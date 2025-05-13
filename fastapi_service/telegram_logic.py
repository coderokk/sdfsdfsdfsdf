import asyncio
import datetime
import random
import time
from typing import Dict, Optional, Tuple, List, Any

import pytz
from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession
from telethon.tl.types import User

from . import config, models
from .utils import get_logger, load_json_data, save_json_data, extract_url_from_text, get_utc_now, get_utc_today_str

# --- Global State and Locks (specific to telegram_logic, but managed by main app) ---
# Эти словари будут инициализированы и управляться из main_fastapi_app.py
# Здесь мы их просто объявляем для ясности использования в этом модуле.

# clients: Dict[str, TelegramClient] = {} -> populated by main app
# client_status: Dict[str, str] = {} -> populated by main app
# client_locks: Dict[str, asyncio.Lock] = {} -> populated by main app
# client_details: Dict[str, models.TelegramClientDetails] = {} -> populated by main app
# client_cooldown_end_times: Dict[str, float] = {} -> populated by main app
# app_state: Dict[str, Any] = {} -> populated by main app (e.g., "clients_initialized", "current_stats", "session_to_phone_map")

# Locks for file access, also managed by main app
# sessions_file_lock_fastapi: asyncio.Lock
# stats_file_lock_fastapi: asyncio.Lock
# select_client_lock: asyncio.Lock


logger = get_logger(__name__)

# --- Statistics Update Functions (moved here as they are closely tied to client status changes) ---

async def update_stats_on_request(
    phone_number: str,
    account_name: Optional[str],
    request_id: str,
    stats_file_path: str, # Pass as arg
    stats_file_lock: asyncio.Lock, # Pass as arg
    app_state: Dict[str, Any] # Pass as arg
) -> None:
    """
    Updates usage statistics for a given phone number when a request is processed.
    """
    task_logger = get_logger("stats_updater", request_id)
    async with stats_file_lock:
        # current_stats = await load_json_data(stats_file_path, stats_file_lock) # Lock already acquired
        # Вместо повторной загрузки файла, используем кэш из app_state
        current_stats = app_state.get("current_stats", {})

        today_utc_str = get_utc_today_str()

        if phone_number not in current_stats:
            current_stats[phone_number] = models.StatsAccountDetail(
                name=account_name,
                session_string_ref=app_state.get("phone_to_session_map", {}).get(phone_number) # Попытка найти сессию
            ).model_dump(exclude_none=True)
            task_logger.info(f"Initialized new stats entry for phone: {phone_number}")

        entry = current_stats[phone_number]
        entry["total_uses"] = entry.get("total_uses", 0) + 1
        entry["last_active"] = get_utc_now().isoformat()
        if account_name and not entry.get("name"): # Обновляем имя, если оно не было установлено или предоставлено новое
            entry["name"] = account_name
        
        if "daily_usage" not in entry:
            entry["daily_usage"] = {}
        entry["daily_usage"][today_utc_str] = entry["daily_usage"].get(today_utc_str, 0) + 1
        
        # Обновляем кэш в app_state
        app_state["current_stats"] = current_stats
        
        if not await save_json_data(Path(stats_file_path), current_stats, stats_file_lock): # Lock already acquired by caller context
            task_logger.error(f"Failed to save updated stats to {stats_file_path} for {phone_number}")
        else:
            task_logger.info(f"Stats updated for {phone_number} ({account_name}). Total: {entry['total_uses']}, Today ({today_utc_str}): {entry['daily_usage'][today_utc_str]}")


async def update_session_worker_status_in_stats(
    phone_number: str,
    session_string: str,
    new_status: str,
    account_name_from_worker: Optional[str],
    request_id: str,
    stats_file_path: str, # Pass as arg
    stats_file_lock: asyncio.Lock, # Pass as arg
    app_state: Dict[str, Any] # Pass as arg
) -> None:
    """
    Updates the status of a session worker in the statistics file.
    """
    task_logger = get_logger("stats_status_updater", request_id)
    async with stats_file_lock:
        # current_stats = await load_json_data(stats_file_path, stats_file_lock) # Lock already acquired
        current_stats = app_state.get("current_stats", {})

        if phone_number not in current_stats:
            current_stats[phone_number] = models.StatsAccountDetail(
                name=account_name_from_worker,
                session_string_ref=session_string,
                status_from_worker=new_status
            ).model_dump(exclude_none=True)
            task_logger.info(f"Initialized new stats entry for phone {phone_number} with status {new_status}")
        else:
            entry = current_stats[phone_number]
            entry["status_from_worker"] = new_status
            if account_name_from_worker: # Обновляем имя, если предоставлено
                entry["name"] = account_name_from_worker
            if "session_string_ref" not in entry or entry["session_string_ref"] != session_string : # Обновляем ссылку на сессию
                 entry["session_string_ref"] = session_string
            
            # Инициализация полей, если отсутствуют (для старых записей)
            if "last_active" not in entry: entry["last_active"] = get_utc_now().isoformat()
            if "total_uses" not in entry: entry["total_uses"] = 0
            if "daily_usage" not in entry: entry["daily_usage"] = {}
            if "notified_daily_limit_today" not in entry: entry["notified_daily_limit_today"] = False # Имя изменено
            if "notified_error" not in entry: entry["notified_error"] = False


            # Сброс флага notified_error, если статус стал "ok"
            if new_status == "ok" and entry.get("notified_error") is True:
                entry["notified_error"] = False
                task_logger.info(f"Reset 'notified_error' flag for {phone_number} as status is 'ok'.")
        
        app_state["current_stats"] = current_stats # Update cache

        if not await save_json_data(Path(stats_file_path), current_stats, stats_file_lock): # Lock already acquired
            task_logger.error(f"Failed to save updated worker status to {stats_file_path} for {phone_number}")
        else:
            task_logger.info(f"Worker status for {phone_number} ({account_name_from_worker}) updated to '{new_status}' in stats.")


# --- Telegram Client Management ---

async def connect_single_client(
    session_str: str,
    phone_hint: str,
    request_id: str,
    clients: Dict[str, TelegramClient], # Pass as arg
    client_status: Dict[str, str], # Pass as arg
    client_details: Dict[str, models.TelegramClientDetails], # Pass as arg
    stats_file_path: str, # Pass as arg
    stats_file_lock: asyncio.Lock, # Pass as arg
    app_state: Dict[str, Any] # Pass as arg
) -> None:
    """
    Connects a single Telegram client using a session string.
    Updates client_status and client_details.
    """
    conn_logger = get_logger(f"client_connector.{phone_hint}", request_id)
    
    if session_str in clients and clients[session_str].is_connected():
        conn_logger.info(f"Client for {phone_hint} (session hint: ...{session_str[-6:]}) already connected and authorized.")
        # Ensure status and details are consistent
        if client_status.get(session_str) != "ok":
            client_status[session_str] = "ok"
            # Get existing name if possible, otherwise it will be updated if stats are loaded
            name_from_details = client_details.get(session_str, {}).get("name", "N/A")
            await update_session_worker_status_in_stats(
                phone_hint, session_str, "ok", name_from_details, request_id,
                stats_file_path, stats_file_lock, app_state
            )
        if session_str not in client_details: # Should not happen if connected
             me = await clients[session_str].get_me()
             name_from_tg = getattr(me, 'first_name', '') + (' ' + getattr(me, 'last_name', '') if getattr(me, 'last_name', '') else '') or getattr(me, 'username', '') or f"ID:{me.id}"
             client_details[session_str] = models.TelegramClientDetails(phone=phone_hint, name=name_from_tg.strip(), original_phone_hint=phone_hint)
        return

    client = TelegramClient(
        StringSession(session_str),
        config.API_ID,
        config.API_HASH,
        request_retries=3,      # Retries for individual requests
        connection_retries=3,   # Retries for initial connection
        retry_delay=5,          # Delay between retries
        auto_reconnect=True,
        lang_code='en',
        system_lang_code='en'
    )
    conn_logger.info(f"Attempting to connect client for {phone_hint} (session: ...{session_str[-6:]})")
    
    new_status = "error" # Default status if connection fails before specific error
    name_from_tg = "N/A"

    try:
        await asyncio.wait_for(client.connect(), timeout=config.TELEGRAM_CONNECT_TIMEOUT)
        
        if client.is_connected():
            conn_logger.info(f"Client for {phone_hint} connected. Checking authorization...")
            if not await client.is_user_authorized():
                new_status = "auth_error"
                conn_logger.error(f"Client for {phone_hint} connected but NOT AUTHORIZED.")
                await client.disconnect()
            else:
                me: User = await client.get_me()
                if not me: # Should not happen if authorized
                    new_status = "error"
                    conn_logger.error(f"Client for {phone_hint} authorized but get_me() returned None.")
                    await client.disconnect()
                else:
                    name_from_tg = getattr(me, 'first_name', '') + \
                                   (' ' + getattr(me, 'last_name', '') if getattr(me, 'last_name', '') else '') or \
                                   getattr(me, 'username', '') or \
                                   f"ID:{me.id}"
                    name_from_tg = name_from_tg.strip()
                    
                    clients[session_str] = client
                    new_status = "ok"
                    client_details[session_str] = models.TelegramClientDetails(
                        phone=phone_hint, # This might be just a hint
                        name=name_from_tg,
                        original_phone_hint=phone_hint
                    )
                    conn_logger.info(f"Client for {phone_hint} ({name_from_tg}) connected and authorized successfully.")
        else: # Should not happen if connect() didn't raise TimeoutError
            new_status = "error"
            conn_logger.error(f"Client for {phone_hint} .connect() completed but client.is_connected() is false.")

    except asyncio.TimeoutError:
        new_status = "timeout_connect" # More specific status
        conn_logger.error(f"Timeout connecting client for {phone_hint} after {config.TELEGRAM_CONNECT_TIMEOUT}s.")
    except errors.AuthKeyError:
        new_status = "auth_key_error" # Session is invalid / revoked by Telegram
        conn_logger.error(f"Authentication key error for {phone_hint}. Session likely invalid or revoked.")
    except errors.SessionPasswordNeededError: # Should not happen with StringSession if 2FA was handled during creation
        new_status = "auth_2fa_error"
        conn_logger.error(f"2FA password needed for {phone_hint}, but using StringSession. Session might be corrupted or improperly generated.")
    except errors.UserDeactivatedError:
        new_status = "deactivated"
        conn_logger.error(f"User account for {phone_hint} is deactivated.")
    except errors.SessionExpiredError:
        new_status = "expired"
        conn_logger.error(f"Session for {phone_hint} has expired.")
    except errors.PhoneNumberInvalidError:
        new_status = "phone_invalid_error" # Should not happen with string sessions
        conn_logger.error(f"Phone number invalid for {phone_hint} (Telethon error).")
    except errors.FloodWaitError as e: # Should not happen on connect, but good to catch
        flood_end_time = int(time.time() + e.seconds + 5)
        new_status = f"flood_wait_{flood_end_time}"
        conn_logger.warning(f"Flood wait encountered for {phone_hint} during connection: {e.seconds}s. Status set to {new_status}.")
    except Exception as e:
        new_status = "error_connect_generic" # More specific status
        conn_logger.error(f"Generic error connecting client for {phone_hint}: {e}", exc_info=True)
    finally:
        client_status[session_str] = new_status
        await update_session_worker_status_in_stats(
            phone_hint, session_str, new_status, name_from_tg if new_status == "ok" else None, request_id,
            stats_file_path, stats_file_lock, app_state
        )
        if new_status != "ok":
            if client and client.is_connected():
                try:
                    await client.disconnect()
                    conn_logger.info(f"Disconnected client for {phone_hint} due to status: {new_status}")
                except Exception as e_disc:
                    conn_logger.error(f"Error disconnecting client for {phone_hint} after error: {e_disc}")
            if session_str in clients: # Remove from active clients if connection failed
                del clients[session_str]
            if session_str in client_details: # Remove details if connection failed
                 del client_details[session_str]


async def select_client_with_lock(
    select_client_lock: asyncio.Lock, # Pass as arg
    clients: Dict[str, TelegramClient], # Pass as arg
    client_status: Dict[str, str], # Pass as arg
    client_locks: Dict[str, asyncio.Lock], # Pass as arg
    client_details: Dict[str, models.TelegramClientDetails], # Pass as arg
    client_cooldown_end_times: Dict[str, float], # Pass as arg
    app_state: Dict[str, Any], # Pass as arg
    stats_file_path: str, # Pass as arg
    stats_file_lock: asyncio.Lock # Pass as arg
) -> Optional[Tuple[str, TelegramClient, asyncio.Lock, str, str]]: # session_str, client, lock, account_name, phone_number
    """
    Selects an available Telegram client, prioritizing those with fewer daily uses.
    Manages client status transitions (e.g., from flood_wait or daily_limit_reached).
    Returns a tuple (session_string, client_instance, client_specific_lock, account_name, phone_number) or None.
    """
    sel_logger = get_logger("client_selector")
    async with select_client_lock:
        sel_logger.debug("Acquired select_client_lock.")
        now_ts = time.time()
        today_utc_str = get_utc_today_str()
        
        potentially_available_sessions: List[str] = []
        
        # Create a copy of items to iterate over, as we might modify client_status
        status_items = list(client_status.items())

        for s_str, status in status_items:
            phone_num_for_s_str = app_state.get("session_to_phone_map", {}).get(s_str)
            if not phone_num_for_s_str:
                if status == "ok": # Only log as error if it was supposed to be OK
                    sel_logger.error(f"CRITICAL: Session string {s_str[-6:]} has status '{status}' but no phone_number in session_to_phone_map. Marking as error.")
                    client_status[s_str] = "error_mapping" # Specific error
                    # No need to call update_session_worker_status_in_stats here, will be handled by health checks or next run
                else:
                    sel_logger.warning(f"Session string {s_str[-6:]} (status: {status}) not found in session_to_phone_map. Skipping.")
                continue

            # 1. Check Cooldown
            if client_cooldown_end_times.get(s_str, 0) > now_ts:
                sel_logger.debug(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) is in cooldown. Ends in {client_cooldown_end_times.get(s_str, 0) - now_ts:.0f}s.")
                continue

            # 2. Handle 'ok' status
            if status == "ok":
                if s_str not in clients or not clients[s_str].is_connected():
                    sel_logger.warning(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) status is 'ok' but client not in 'clients' dict or not connected. Marking as 'error_disconnected'.")
                    client_status[s_str] = "error_disconnected"
                    await update_session_worker_status_in_stats(
                        phone_num_for_s_str, s_str, "error_disconnected", 
                        client_details.get(s_str, {}).get("name"), "select_client",
                        stats_file_path, stats_file_lock, app_state
                    )
                    continue
                potentially_available_sessions.append(s_str)
                continue # Move to next session string

            # 3. Handle 'flood_wait_TIMESTAMP'
            if status.startswith("flood_wait_"):
                try:
                    flood_end_time = int(status.split("_")[-1])
                    if now_ts >= flood_end_time:
                        sel_logger.info(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) flood wait ended. Changing status to 'ok'.")
                        client_status[s_str] = "ok" # Tentatively OK
                        await update_session_worker_status_in_stats(
                            phone_num_for_s_str, s_str, "ok", 
                            client_details.get(s_str, {}).get("name"), "select_client_flood_end",
                            stats_file_path, stats_file_lock, app_state
                        )
                        # Now re-check connection for this newly 'ok' client
                        if s_str in clients and clients[s_str].is_connected():
                            potentially_available_sessions.append(s_str)
                        else:
                            sel_logger.warning(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) became 'ok' after flood, but not connected. Marking 'error_disconnected_post_flood'.")
                            client_status[s_str] = "error_disconnected_post_flood"
                            await update_session_worker_status_in_stats(
                                phone_num_for_s_str, s_str, "error_disconnected_post_flood", 
                                client_details.get(s_str, {}).get("name"), "select_client_flood_end_fail",
                                stats_file_path, stats_file_lock, app_state
                            )
                    else:
                        sel_logger.debug(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) still in flood wait. Ends in {flood_end_time - now_ts:.0f}s.")
                except ValueError:
                    sel_logger.error(f"Invalid flood_wait timestamp for {phone_num_for_s_str} (...{s_str[-6:]}): {status}. Marking as 'error_status_parse'.")
                    client_status[s_str] = "error_status_parse"
                continue

            # 4. Handle 'daily_limit_reached_YYYY-MM-DD'
            if status.startswith("daily_limit_reached_"):
                try:
                    limit_date_str = status.split("_")[-1]
                    if limit_date_str != today_utc_str:
                        sel_logger.info(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) daily limit for {limit_date_str} passed. Today is {today_utc_str}. Changing status to 'ok'.")
                        client_status[s_str] = "ok" # Tentatively OK
                        await update_session_worker_status_in_stats(
                            phone_num_for_s_str, s_str, "ok", 
                            client_details.get(s_str, {}).get("name"), "select_client_limit_reset",
                            stats_file_path, stats_file_lock, app_state
                        )
                        # Reset notified flag in stats
                        async with stats_file_lock: # Ensure exclusive access to app_state["current_stats"]
                            stats_data = app_state.get("current_stats", {})
                            if phone_num_for_s_str in stats_data and "notified_daily_limit_today" in stats_data[phone_num_for_s_str]:
                                stats_data[phone_num_for_s_str]["notified_daily_limit_today"] = False # Reset for new day
                                app_state["current_stats"] = stats_data # Update cache
                                # Save will happen via update_session_worker_status_in_stats or next stats save

                        if s_str in clients and clients[s_str].is_connected():
                            potentially_available_sessions.append(s_str)
                        else:
                            sel_logger.warning(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) became 'ok' after daily limit, but not connected. Marking 'error_disconnected_post_limit'.")
                            client_status[s_str] = "error_disconnected_post_limit"
                            await update_session_worker_status_in_stats(
                                phone_num_for_s_str, s_str, "error_disconnected_post_limit", 
                                client_details.get(s_str, {}).get("name"), "select_client_limit_reset_fail",
                                stats_file_path, stats_file_lock, app_state
                            )
                    else:
                        sel_logger.debug(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) daily limit still active for today ({today_utc_str}).")
                except IndexError:
                    sel_logger.error(f"Invalid daily_limit_reached format for {phone_num_for_s_str} (...{s_str[-6:]}): {status}. Marking as 'error_status_parse'.")
                    client_status[s_str] = "error_status_parse"
                continue
            
            # 5. Skip other error statuses (error, auth_error, etc.)
            # sel_logger.debug(f"Session {phone_num_for_s_str} (...{s_str[-6:]}) has status '{status}', skipping.")


        if not potentially_available_sessions:
            sel_logger.warning("No potentially available sessions found after checking status, cooldowns, flood waits, and daily limits.")
            # select_client_lock is released by 'async with'
            return None

        # Sort available sessions by daily usage
        client_usage_tuples: List[Tuple[str, int]] = []
        current_stats_cache = app_state.get("current_stats", {})

        for s_str_avail in potentially_available_sessions:
            phone_num_for_sort = app_state.get("session_to_phone_map", {}).get(s_str_avail)
            if not phone_num_for_sort: # Should not happen due to earlier check
                sel_logger.error(f"CRITICAL: Available session {s_str_avail[-6:]} missing from session_to_phone_map during sorting. Skipping.")
                continue

            daily_uses = 0
            if phone_num_for_sort in current_stats_cache:
                daily_uses = current_stats_cache[phone_num_for_sort].get("daily_usage", {}).get(today_utc_str, 0)
            
            # Check against daily limit again, as stats might have updated
            if daily_uses >= config.DAILY_REQUEST_LIMIT_PER_SESSION:
                sel_logger.warning(f"Session {phone_num_for_sort} (...{s_str_avail[-6:]}) reached daily limit ({daily_uses}/{config.DAILY_REQUEST_LIMIT_PER_SESSION}) during selection sort. Marking and skipping.")
                new_status_limit = f"daily_limit_reached_{today_utc_str}"
                client_status[s_str_avail] = new_status_limit
                await update_session_worker_status_in_stats(
                    phone_num_for_sort, s_str_avail, new_status_limit,
                    client_details.get(s_str_avail, {}).get("name"), "select_client_limit_recheck",
                    stats_file_path, stats_file_lock, app_state
                )
                continue # Skip this one

            client_usage_tuples.append((s_str_avail, daily_uses))

        if not client_usage_tuples:
            sel_logger.warning("No sessions available after re-checking daily limits during sort.")
            return None

        # Sort by daily_uses (ascending), then by s_str_avail (ascending, for tie-breaking consistency)
        client_usage_tuples.sort(key=lambda x: (x[1], x[0]))
        
        sel_logger.info(f"Sorted available sessions by usage: {[(app_state.get('session_to_phone_map',{}).get(s,'?'), u) for s,u in client_usage_tuples]}")

        for s_str_candidate, daily_uses_candidate in client_usage_tuples:
            # Re-verify client exists and is connected (it might have been disconnected by another task)
            candidate_client = clients.get(s_str_candidate)
            if not candidate_client or not candidate_client.is_connected():
                sel_logger.warning(f"Candidate session {app_state.get('session_to_phone_map',{}).get(s_str_candidate)} (...{s_str_candidate[-6:]}) was disconnected or removed before final selection. Marking 'error_disconnected_final'.")
                client_status[s_str_candidate] = "error_disconnected_final"
                await update_session_worker_status_in_stats(
                    app_state.get('session_to_phone_map',{}).get(s_str_candidate), s_str_candidate, "error_disconnected_final",
                    client_details.get(s_str_candidate, {}).get("name"), "select_client_final_check",
                    stats_file_path, stats_file_lock, app_state
                )
                continue

            candidate_details_model = client_details.get(s_str_candidate)
            if not candidate_details_model:
                sel_logger.critical(f"CRITICAL: Client details not found for selected candidate session {s_str_candidate[-6:]}. Marking as 'error_details_missing'.")
                client_status[s_str_candidate] = "error_details_missing"
                # No update_session_worker_status_in_stats here, as phone_number might be unknown
                continue
            
            candidate_name = candidate_details_model.name
            candidate_phone = candidate_details_model.phone # This is the original_phone_hint

            candidate_lock = client_locks.get(s_str_candidate)
            if not candidate_lock: # Should have been created during initialization
                sel_logger.critical(f"CRITICAL: Lock not found for selected candidate session {s_str_candidate[-6:]}. Creating one now. This indicates an issue in client_locks initialization.")
                candidate_lock = asyncio.Lock()
                client_locks[s_str_candidate] = candidate_lock
                # No status change needed here, just a recovery action

            sel_logger.info(f"Selected client: {candidate_name} ({candidate_phone}, ...{s_str_candidate[-6:]}) with {daily_uses_candidate} daily uses.")
            # select_client_lock is released by 'async with'
            return s_str_candidate, candidate_client, candidate_lock, candidate_name, candidate_phone

        sel_logger.warning("Loop finished without selecting any client from sorted list (all re-verified candidates failed).")
        # select_client_lock is released by 'async with'
        return None


# --- Core Telegram Interaction Logic ---

async def process_link_with_telegram(
    client: TelegramClient,
    url_to_process: str,
    account_name: str, # Name of the TG account being used
    phone_number: str, # Phone number hint of the TG account
    request_id: str,
    client_status_dict: Dict[str, str], # Pass client_status directly
    stats_file_path_str: str, # Pass as arg
    stats_file_lock: asyncio.Lock, # Pass as arg
    app_state: Dict[str, Any] # Pass as arg
) -> Dict[str, Optional[str]]:
    """
    Processes a URL through the target Telegram bot using the provided client.
    Handles conversation flow, button clicking, and message parsing.
    Updates client_status on certain errors.
    """
    proc_logger = get_logger(f"tg_processor.{account_name}", request_id)
    result: Dict[str, Optional[str]] = {'main_url': None, 'license_url': None, 'error': None, 'telegram_error_type': None}
    
    session_str_saved = client.session.save() # Get session string for status updates

    try:
        proc_logger.info(f"Getting entity for bot: {config.TARGET_BOT_USERNAME}")
        entity = await client.get_entity(config.TARGET_BOT_USERNAME)

        # Total conversation timeout should be generous enough for all steps
        # Button timeout + intermediate timeout + (2 * response_timeout for main/license)
        # Adding a small buffer
        total_conversation_timeout = (
            config.TELEGRAM_BUTTON_RESPONSE_TIMEOUT + 
            config.TELEGRAM_INTERMEDIATE_MESSAGE_TIMEOUT + 
            (2 * config.TELEGRAM_RESPONSE_TIMEOUT) + 
            120 # Buffer
        )
        
        # We use a slightly shorter timeout for the `with client.conversation` block itself,
        # and manage individual `conv.get_response` timeouts within the loop.
        # The main purpose of the outer timeout is to prevent indefinite blocking if the bot hangs completely.
        # Let's make it sum of individual steps + buffer
        conversation_context_timeout = config.TELEGRAM_BUTTON_RESPONSE_TIMEOUT + \
                                       config.TELEGRAM_INTERMEDIATE_MESSAGE_TIMEOUT + \
                                       config.TELEGRAM_RESPONSE_TIMEOUT * 2 + 60 # Buffer for processing

        proc_logger.info(f"Starting conversation with {config.TARGET_BOT_USERNAME}. Total context timeout: {conversation_context_timeout}s")

        async with client.conversation(entity, timeout=conversation_context_timeout, exclusive=True) as conv:
            # 1. Send URL
            proc_logger.info(f"Sending URL to bot: {url_to_process}")
            await conv.send_message(url_to_process)

            # 2. Wait for button response
            proc_logger.info(f"Waiting for button response (timeout: {config.TELEGRAM_BUTTON_RESPONSE_TIMEOUT}s)...")
            response_buttons_msg = await conv.get_response(timeout=config.TELEGRAM_BUTTON_RESPONSE_TIMEOUT)
            
            button_found_and_clicked = False
            if response_buttons_msg and response_buttons_msg.buttons:
                proc_logger.debug(f"Received message with buttons: {response_buttons_msg.text[:100]}")
                for row in response_buttons_msg.buttons:
                    for btn in row:
                        if btn.text and btn.text.strip().lower() == config.TARGET_BUTTON_TEXT.lower():
                            proc_logger.info(f"Target button '{config.TARGET_BUTTON_TEXT}' found. Clicking...")
                            await asyncio.sleep(random.uniform(0.5, 1.5)) # Small delay before click
                            # await response_buttons_msg.click(text=btn.text) # Prefer clicking by index if possible
                            await btn.click() # Click the button object directly
                            button_found_and_clicked = True
                            proc_logger.info(f"Button '{btn.text}' clicked.")
                            break
                    if button_found_and_clicked:
                        break
            
            if not button_found_and_clicked:
                err_msg = f"Button '{config.TARGET_BUTTON_TEXT}' not found in bot's response."
                proc_logger.error(err_msg + f" Response text: {response_buttons_msg.text[:200] if response_buttons_msg else 'No response'}")
                result['telegram_error_type'] = "ButtonNotFoundError"
                raise ValueError(err_msg)

            # 3. Optional: Wait for an intermediate message (e.g., "Processing your request...")
            if config.TELEGRAM_INTERMEDIATE_MESSAGE_TIMEOUT > 0:
                try:
                    proc_logger.info(f"Waiting for intermediate message (timeout: {config.TELEGRAM_INTERMEDIATE_MESSAGE_TIMEOUT}s)...")
                    intermediate_response = await conv.get_response(timeout=config.TELEGRAM_INTERMEDIATE_MESSAGE_TIMEOUT)
                    if intermediate_response and intermediate_response.text:
                        proc_logger.info(f"Intermediate bot message: {intermediate_response.text[:150]}")
                except asyncio.TimeoutError:
                    proc_logger.warning("Timeout waiting for optional intermediate message. Proceeding...")
                except errors.AlreadyRepliedError: # Can happen if bot sends multiple messages quickly
                    proc_logger.warning("AlreadyRepliedError caught for intermediate message, likely bot sent files fast.")

            # 4. Wait for file/link messages
            proc_logger.info(f"Waiting for file/link messages (individual timeout: {config.TELEGRAM_RESPONSE_TIMEOUT}s per message)...")
            
            # Flags to track if we've seen messages that *should* contain the links,
            # even if URL extraction failed. This helps break if bot sends text but no valid URLs.
            main_file_message_signature_seen = False
            license_file_message_signature_seen = False
            
            # Overall timeout for this loop part
            # We expect two messages, each with TELEGRAM_RESPONSE_TIMEOUT
            # Add a buffer for processing time between messages
            link_wait_loop_start_time = time.monotonic()
            max_link_wait_duration = config.TELEGRAM_RESPONSE_TIMEOUT * 2 + 30 # Max time for this loop

            while not (result.get('main_url') and result.get('license_url')):
                current_loop_time = time.monotonic()
                if current_loop_time - link_wait_loop_start_time > max_link_wait_duration:
                    proc_logger.error(f"Max duration {max_link_wait_duration}s exceeded for link waiting loop.")
                    if not result.get('main_url'):
                        result['telegram_error_type'] = "MainFileTimeoutErrorOverall"
                        raise TimeoutError("Timeout waiting for main file/link message (overall loop).")
                    else: # Main URL found, but license timed out
                        proc_logger.warning("Main URL found, but timed out waiting for license URL (overall loop).")
                        break # Proceed with what we have

                # If we've seen signatures for both main and license messages but still no URLs,
                # it's unlikely more messages will help.
                if main_file_message_signature_seen and license_file_message_signature_seen and \
                   not result.get('main_url') and not result.get('license_url'):
                    proc_logger.warning("Saw signatures for main and license messages, but no URLs extracted. Breaking loop.")
                    if not result.get('main_url'): # If main is still missing, it's an error
                         result['telegram_error_type'] = "MainUrlMissingAfterSignatures"
                         raise ValueError("Main file URL not obtained despite seeing relevant message signature.")
                    break


                # Calculate remaining time for this specific get_response call
                # This is tricky because conv.get_response has its own timeout.
                # We rely on TELEGRAM_RESPONSE_TIMEOUT for individual messages.
                # The outer loop timeout (max_link_wait_duration) is a safeguard.
                
                try:
                    current_resp = await conv.get_response(timeout=config.TELEGRAM_RESPONSE_TIMEOUT)
                except asyncio.TimeoutError:
                    # This timeout is for a single expected message (main or license)
                    if not result.get('main_url'):
                        err_msg = "Timeout waiting for a response message containing the main file/link."
                        proc_logger.error(err_msg)
                        result['telegram_error_type'] = "MainFileTimeoutError"
                        raise TimeoutError(err_msg)
                    else: # Main URL found, but license timed out
                        proc_logger.warning("Timeout waiting for a message potentially containing the license file/link. Proceeding with main file only.")
                        break # Exit loop, process with what we have

                current_text_lower = (current_resp.text or "").lower()
                full_message_text_for_url_extraction = current_resp.text or ""
                proc_logger.debug(f"Received message from bot: {full_message_text_for_url_extraction[:150]}")

                # Check for "Oops!" error from bot
                if config.OOPS_BOT_ERROR_KEYWORD and config.OOPS_BOT_ERROR_KEYWORD in current_text_lower:
                    err_msg = f"Bot reported '{config.OOPS_BOT_ERROR_KEYWORD}': {current_resp.text[:100]}"
                    proc_logger.warning(err_msg) # Log as warning, but treat as error for this request
                    result['telegram_error_type'] = "BotReportedOopsError"
                    # This error should not mark the session as faulty.
                    raise ValueError(err_msg) # Raise to be caught by the outer handler

                # Check for other generic bot errors
                # Keywords like "ошибка", "не найден", "лимит" (and English equivalents if applicable)
                # This is a basic check, might need refinement based on bot's actual error messages
                bot_error_keywords = ["ошибка", "не найден", "лимит", "error", "not found", "limit reached"]
                if any(keyword in current_text_lower for keyword in bot_error_keywords):
                    # More specific check: avoid false positives if these words are part of normal messages
                    # For example, if "ссылка на файл не найдена" is a specific error.
                    # This needs to be tuned based on the bot's actual responses.
                    # For now, a simple check:
                    if "ссылк" not in current_text_lower: # If "ссылка" (link) is not mentioned, it's more likely an error
                        err_msg = f"Bot reported a potential error: {current_resp.text[:100]}"
                        proc_logger.warning(err_msg)
                        result['telegram_error_type'] = "BotReportedError"
                        raise ValueError(err_msg) # Raise to be caught

                # Try to extract main file URL
                if not result.get('main_url') and \
                   config.MAIN_FILE_KEYWORD in current_text_lower and \
                   config.LINK_KEYWORD in current_text_lower:
                    main_file_message_signature_seen = True
                    proc_logger.info(f"Main file keyword '{config.MAIN_FILE_KEYWORD}' and link keyword '{config.LINK_KEYWORD}' found. Attempting to extract URL.")
                    extracted_main_url = extract_url_from_text(full_message_text_for_url_extraction, request_id)
                    if extracted_main_url:
                        result['main_url'] = extracted_main_url
                        proc_logger.info(f"Extracted main file URL: {extracted_main_url}")
                    else:
                        proc_logger.warning(f"Main file keywords found, but no URL extracted from: {full_message_text_for_url_extraction[:100]}")
                
                # Try to extract license file URL
                elif not result.get('license_url') and \
                     config.LICENSE_KEYWORD in current_text_lower and \
                     config.LINK_KEYWORD in current_text_lower:
                    license_file_message_signature_seen = True
                    proc_logger.info(f"License file keyword '{config.LICENSE_KEYWORD}' and link keyword '{config.LINK_KEYWORD}' found. Attempting to extract URL.")
                    extracted_license_url = extract_url_from_text(full_message_text_for_url_extraction, request_id)
                    if extracted_license_url:
                        result['license_url'] = extracted_license_url
                        proc_logger.info(f"Extracted license file URL: {extracted_license_url}")
                    else:
                        proc_logger.warning(f"License file keywords found, but no URL extracted from: {full_message_text_for_url_extraction[:100]}")
            
            # After loop, check if main URL was obtained
            if not result.get('main_url'):
                err_msg = "Main file URL not obtained after conversation loop."
                proc_logger.error(err_msg)
                result['telegram_error_type'] = "MainUrlMissingAfterLoop"
                raise ValueError(err_msg)

            proc_logger.info("Conversation finished successfully. Main URL obtained.")
            # Mark conversation as read (optional, helps in some cases)
            # await conv.mark_read() # This might throw if conversation ended abruptly
            return result

    except errors.FloodWaitError as e:
        err_type_name = type(e).__name__
        err_msg = f"FloodWaitError for {account_name} ({phone_number}): {e.seconds} seconds. {str(e)}"
        proc_logger.error(err_msg)
        result['error'] = err_msg
        result['telegram_error_type'] = err_type_name
        
        if session_str_saved and phone_number:
            flood_end_timestamp = int(time.time() + e.seconds + 5) # Add a small buffer
            new_status_flood = f"flood_wait_{flood_end_timestamp}"
            client_status_dict[session_str_saved] = new_status_flood
            proc_logger.info(f"Set status for {phone_number} to {new_status_flood}")
            # Stats update will be handled by select_client or health checks
            await update_session_worker_status_in_stats(
                phone_number, session_str_saved, new_status_flood, account_name, request_id,
                stats_file_path_str, stats_file_lock, app_state
            )
        return result

    except (asyncio.TimeoutError, errors.TimeoutError) as e: # Catch Telethon's TimeoutError too
        err_type_name = type(e).__name__
        # Distinguish between conversation timeout and specific step timeouts
        if result.get('telegram_error_type'): # Specific timeout already set (e.g., ButtonNotFound, MainFileTimeout)
            err_msg = result.get('error') or f"Timeout during Telegram interaction: {str(e)}"
        else: # General conversation timeout
            err_msg = f"Overall conversation timeout for {account_name} ({phone_number}): {str(e)}"
            result['telegram_error_type'] = "ConversationTimeoutError"
        
        proc_logger.error(err_msg)
        result['error'] = err_msg
        # For general conversation timeouts, we usually don't penalize the session status
        # unless it happens repeatedly (which health checks might catch).
        # If a specific step timed out (e.g. button not found), that's handled by ValueError below.
        return result

    except ValueError as e: # Handles ButtonNotFoundError, BotReportedOopsError, MainUrlMissing, etc.
        err_type_name = type(e).__name__
        err_msg = f"ValueError for {account_name} ({phone_number}): {str(e)}"
        proc_logger.error(err_msg)
        result['error'] = err_msg
        if not result.get('telegram_error_type'): # Ensure type is set
            result['telegram_error_type'] = err_type_name

        if session_str_saved and phone_number:
            current_error_type = result.get('telegram_error_type')
            if current_error_type == "BotReportedOopsError":
                proc_logger.info(f"Bot reported 'Oops' for {phone_number}. Session status will NOT be changed.")
                # No status change, no stats update for this specific error
            elif current_error_type == "ButtonNotFoundError" or \
                 current_error_type == "MainUrlMissingAfterLoop" or \
                 current_error_type == "MainUrlMissingAfterSignatures" or \
                 current_error_type == "BotReportedError":
                # These errors might indicate a problem with the bot or the session's ability to interact.
                # Mark session as 'error' for review.
                client_status_dict[session_str_saved] = "error_interaction"
                proc_logger.warning(f"Marking session {phone_number} as 'error_interaction' due to {current_error_type}.")
                await update_session_worker_status_in_stats(
                    phone_number, session_str_saved, "error_interaction", account_name, request_id,
                    stats_file_path_str, stats_file_lock, app_state
                )
            # Other ValueErrors might not require immediate status change, depends on their nature.
        return result

    except (errors.UserDeactivatedError, errors.AuthKeyError, errors.SessionExpiredError,
            errors.PhoneNumberInvalidError, errors.ChatWriteForbiddenError, errors.UserIsBlockedError) as e:
        err_type_name = type(e).__name__
        err_msg = f"Critical Telegram error for {account_name} ({phone_number}): {err_type_name} - {str(e)}"
        proc_logger.error(err_msg)
        result['error'] = err_msg
        result['telegram_error_type'] = err_type_name

        if session_str_saved and phone_number:
            worker_new_status = "error" # Default
            if isinstance(e, errors.UserDeactivatedError): worker_new_status = "deactivated"
            elif isinstance(e, errors.AuthKeyError): worker_new_status = "auth_key_error"
            elif isinstance(e, errors.SessionExpiredError): worker_new_status = "expired"
            elif isinstance(e, errors.UserIsBlockedError): worker_new_status = "blocked_by_bot" # Specific
            elif isinstance(e, errors.ChatWriteForbiddenError): worker_new_status = "chat_write_forbidden" # Specific

            client_status_dict[session_str_saved] = worker_new_status
            proc_logger.info(f"Set status for {phone_number} to {worker_new_status} due to {err_type_name}")
            await update_session_worker_status_in_stats(
                phone_number, session_str_saved, worker_new_status, account_name, request_id,
                stats_file_path_str, stats_file_lock, app_state
            )
        return result
    
    except errors.RPCError as e: # Catch other Telethon RPC errors
        err_type_name = type(e).__name__
        err_msg = f"Telegram RPCError for {account_name} ({phone_number}): {err_type_name} - {str(e)}"
        proc_logger.error(err_msg, exc_info=True) # Log with traceback for unexpected RPC errors
        result['error'] = err_msg
        result['telegram_error_type'] = f"RPCError_{err_type_name}"

        if session_str_saved and phone_number:
            # For generic RPC errors, mark as 'error' for investigation
            client_status_dict[session_str_saved] = "error_rpc"
            proc_logger.warning(f"Marking session {phone_number} as 'error_rpc' due to {err_type_name}.")
            await update_session_worker_status_in_stats(
                phone_number, session_str_saved, "error_rpc", account_name, request_id,
                stats_file_path_str, stats_file_lock, app_state
            )
        return result

    except Exception as e:
        err_type_name = type(e).__name__
        proc_logger.critical(f"Unhandled exception during Telegram processing for {account_name} ({phone_number}): {err_type_name} - {str(e)}", exc_info=True)
        result['error'] = f"Critical unhandled error: {str(e)}"
        result['telegram_error_type'] = f"UnhandledException_{err_type_name}"
        
        if session_str_saved and phone_number:
            client_status_dict[session_str_saved] = "error_unhandled"
            proc_logger.warning(f"Marking session {phone_number} as 'error_unhandled' due to {err_type_name}.")
            await update_session_worker_status_in_stats(
                phone_number, session_str_saved, "error_unhandled", account_name, request_id,
                stats_file_path_str, stats_file_lock, app_state
            )
        return result
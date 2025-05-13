# telegram_management_bot/handlers/session_management_handlers.py
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, List

from aiogram import Router, F, Bot
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import aiofiles
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from .. import bot_config
from ..keyboards.inline_keyboards import (
    get_confirmation_keyboard,
    get_manage_sessions_keyboard,
    get_back_to_menu_keyboard,
    get_item_selection_keyboard,
    get_pagination_keyboard
)
from ..utils.bot_utils import AdminFilter, get_user_info, CONFIRM_YES, CONFIRM_NO, CANCEL_ACTION, ActionWithIdCallback, PaginatorCallback
from ..utils.fastapi_interaction import get_fastapi_health # Для получения статусов сессий

logger = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(AdminFilter(bot_config.ADMIN_IDS))
router.message.filter(AdminFilter(bot_config.ADMIN_IDS))

# --- FSM для добавления новой сессии ---
class AddSessionStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_api_id = State() # Если не используются глобальные
    waiting_for_api_hash = State() # Если не используются глобальные
    waiting_for_code = State()
    waiting_for_password = State() # Для 2FA

# --- Утилиты для работы с sessions.json и stats.json ---
# Эти функции дублируют логику из FastAPI utils, но для бота они могут быть немного другими
# (например, синхронные или с другими путями). Для простоты, здесь будут свои реализации.

async def load_json_bot(filepath: Path) -> Dict:
    if not filepath.exists():
        return {}
    try:
        async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
            content = await f.read()
            return json.loads(content) if content else {}
    except Exception as e:
        logger.error(f"Error loading JSON from {filepath} in bot: {e}")
        return {}

async def save_json_bot(filepath: Path, data: Dict) -> bool:
    try:
        # Атомарная запись
        temp_filepath = filepath.with_suffix(f"{filepath.suffix}.tmp_bot")
        async with aiofiles.open(temp_filepath, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, indent=4, ensure_ascii=False))
        os.replace(temp_filepath, filepath) # Синхронно, но быстро
        return True
    except Exception as e:
        logger.error(f"Error saving JSON to {filepath} in bot: {e}")
        if temp_filepath.exists():
            try: os.remove(temp_filepath)
            except: pass
        return False

# --- Добавление новой сессии ---
CALLBACK_PREFIX_ADD_SESSION = "session_add" # Не используется для FSM, но для общей логики
@router.callback_query(F.data == "session_add_new")
async def cq_add_session_start(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} initiated adding new session.")
    await state.set_state(AddSessionStates.waiting_for_phone)
    
    message_text = (
        "📱 **Add New Telegram Session**\n\n"
        "Please enter the phone number for the new session in international format (e.g., `+1234567890`)."
    )
    await callback_query.message.edit_text(
        message_text,
        parse_mode="Markdown",
        reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
    )
    await callback_query.answer()

@router.message(StateFilter(AddSessionStates.waiting_for_phone))
async def process_phone_for_session(message: Message, state: FSMContext):
    phone_number = message.text.strip()
    # Простая валидация формата номера телефона
    if not re.match(r"^\+\d{10,15}$", phone_number):
        await message.reply(
            "❌ Invalid phone number format. Please use international format (e.g., `+1234567890`).",
            parse_mode="Markdown"
        )
        return
    
    await state.update_data(phone_number=phone_number)
    logger.info(f"Received phone number for new session: {phone_number}")

    # Проверяем, есть ли глобальные API_ID и API_HASH
    if bot_config.TELETHON_API_ID and bot_config.TELETHON_API_HASH:
        await state.update_data(api_id=bot_config.TELETHON_API_ID, api_hash=bot_config.TELETHON_API_HASH)
        await message.answer(
            f"Using global API ID and Hash. Sending code to `{phone_number}`...",
            parse_mode="Markdown"
        )
        # Сразу переходим к отправке кода
        await _send_code_request_for_session(message, state)
    else:
        await state.set_state(AddSessionStates.waiting_for_api_id)
        await message.answer(
            "Next, please enter your Telegram Application **API ID**.\n"
            "You can get this from [my.telegram.org](https://my.telegram.org/apps).",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
        )

@router.message(StateFilter(AddSessionStates.waiting_for_api_id))
async def process_api_id_for_session(message: Message, state: FSMContext):
    try:
        api_id = int(message.text.strip())
        await state.update_data(api_id=api_id)
        await state.set_state(AddSessionStates.waiting_for_api_hash)
        logger.info(f"Received API ID: {api_id}")
        await message.answer(
            "Great. Now enter your Telegram Application **API Hash**.",
            reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
        )
    except ValueError:
        await message.reply("❌ API ID must be an integer. Please try again.")

@router.message(StateFilter(AddSessionStates.waiting_for_api_hash))
async def process_api_hash_for_session(message: Message, state: FSMContext):
    api_hash = message.text.strip()
    if not api_hash: # Простая проверка
        await message.reply("❌ API Hash cannot be empty. Please try again.")
        return
        
    await state.update_data(api_hash=api_hash)
    logger.info(f"Received API Hash (masked): {api_hash[:5]}...")
    
    data = await state.get_data()
    phone_number = data.get("phone_number")
    await message.answer(
        f"API ID and Hash received. Sending code to `{phone_number}`...",
        parse_mode="Markdown"
    )
    await _send_code_request_for_session(message, state)


async def _send_code_request_for_session(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone_number")
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")

    # Создаем временный клиент Telethon для логина
    # Используем сессию в памяти, т.к. StringSession будет получен только после логина
    client = TelegramClient(StringSession(), api_id, api_hash, lang_code='en', system_lang_code='en')
    
    try:
        await client.connect()
        logger.info(f"Telethon client connected for {phone} to send code.")
        sent_code = await client.send_code_request(phone)
        await state.update_data(phone_code_hash=sent_code.phone_code_hash, temp_client=client) # Сохраняем клиент в state
        await state.set_state(AddSessionStates.waiting_for_code)
        logger.info(f"Code sent to {phone}. Phone code hash stored.")
        await message.answer(
            "A code has been sent to your Telegram account (or via SMS).\n"
            "Please enter the code you received.",
            reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
        )
    except errors.PhoneNumberInvalidError:
        logger.error(f"Phone number {phone} is invalid.")
        await message.answer("❌ The phone number you entered is invalid. Please start over.",
                             reply_markup=get_back_to_menu_keyboard("manage_sessions", "⬅️ Back"))
        await state.clear()
    except errors.ApiIdInvalidError:
        logger.error(f"API ID/Hash is invalid.")
        await message.answer("❌ The API ID or API Hash is invalid. Please check them and start over.",
                             reply_markup=get_back_to_menu_keyboard("manage_sessions", "⬅️ Back"))
        await state.clear()
    except Exception as e:
        logger.error(f"Error sending code for {phone}: {e}", exc_info=True)
        await message.answer(f"❌ An error occurred while sending the code: {e}\n"
                             "Please try again or check your details.",
                             reply_markup=get_back_to_menu_keyboard("manage_sessions", "⬅️ Back"))
        if client.is_connected(): await client.disconnect()
        await state.clear()


@router.message(StateFilter(AddSessionStates.waiting_for_code))
async def process_code_for_session(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get("phone_number")
    phone_code_hash = data.get("phone_code_hash")
    client: TelegramClient = data.get("temp_client") # Получаем клиент из state

    if not client:
        logger.error("Temporary Telethon client not found in state for code processing.")
        await message.answer("Internal error: client session lost. Please start over.",
                             reply_markup=get_back_to_menu_keyboard("manage_sessions", "⬅️ Back"))
        await state.clear()
        return

    try:
        logger.info(f"Attempting to sign in {phone} with code.")
        signed_in_user = await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        
        session_string = client.session.save()
        logger.info(f"Successfully signed in user: {signed_in_user.username if signed_in_user.username else signed_in_user.id}. Session string obtained.")
        
        # Сохраняем сессию
        sessions_data = await load_json_bot(bot_config.SESSIONS_JSON_PATH)
        sessions_data[phone] = session_string
        if await save_json_bot(bot_config.SESSIONS_JSON_PATH, sessions_data):
            logger.info(f"Session for {phone} saved to {bot_config.SESSIONS_JSON_PATH.name}")
            
            # Добавляем базовую запись в stats.json
            stats_data = await load_json_bot(bot_config.STATS_JSON_PATH)
            if phone not in stats_data:
                user_name = getattr(signed_in_user, 'first_name', '') + \
                            (' ' + getattr(signed_in_user, 'last_name', '') if getattr(signed_in_user, 'last_name', '') else '') or \
                            getattr(signed_in_user, 'username', '') or f"ID:{signed_in_user.id}"
                stats_data[phone] = {
                    "name": user_name.strip(),
                    "total_uses": 0,
                    "last_active": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status_from_worker": "ok_new", # Начальный статус
                    "session_string_ref": session_string, # Ссылка на сессию
                    "daily_usage": {}
                }
                if await save_json_bot(bot_config.STATS_JSON_PATH, stats_data):
                    logger.info(f"Initial stats entry for {phone} created in {bot_config.STATS_JSON_PATH.name}")
                else:
                    logger.error(f"Failed to save initial stats for {phone}.")
            
            await message.answer(
                f"✅ Session for `{phone}` added successfully!\n"
                "The FastAPI service might need a restart or a session reload command "
                "to pick up the new session.",
                parse_mode="Markdown",
                reply_markup=get_manage_sessions_keyboard()
            )
        else:
            await message.answer(
                "❌ Failed to save the session string to file. Please check bot logs.",
                reply_markup=get_manage_sessions_keyboard()
            )
            
        await state.clear()

    except errors.SessionPasswordNeededError:
        logger.info(f"2FA password needed for {phone}.")
        await state.set_state(AddSessionStates.waiting_for_password)
        await message.answer(
            "This account has Two-Factor Authentication (2FA) enabled.\n"
            "Please enter your 2FA password.",
            reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
        )
    except errors.PhoneCodeInvalidError:
        logger.warning(f"Invalid code entered for {phone}.")
        await message.reply("❌ The code you entered is invalid. Please try again or request a new code (cancel and start over).")
        # Остаемся в состоянии waiting_for_code
    except errors.PhoneCodeExpiredError:
        logger.warning(f"Code expired for {phone}.")
        await message.reply("❌ The code has expired. Please cancel and start the process again to get a new code.",
                            reply_markup=get_back_to_menu_keyboard("manage_sessions", "⬅️ Back"))
        await state.clear() # Сбрасываем, т.к. нужен новый phone_code_hash
    except Exception as e:
        logger.error(f"Error signing in {phone}: {e}", exc_info=True)
        await message.answer(f"❌ An error occurred during sign-in: {e}\n"
                             "Please try again or check your details.",
                             reply_markup=get_manage_sessions_keyboard())
        await state.clear()
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info(f"Disconnected temporary Telethon client for {phone}.")
        # Не очищаем temp_client из state, если перешли на waiting_for_password

@router.message(StateFilter(AddSessionStates.waiting_for_password))
async def process_password_for_session(message: Message, state: FSMContext):
    password = message.text # Не strip(), пароль может содержать пробелы
    data = await state.get_data()
    phone = data.get("phone_number")
    client: TelegramClient = data.get("temp_client")

    if not client: # Должен быть здесь из предыдущего шага
        logger.error("Temporary Telethon client not found in state for password processing.")
        await message.answer("Internal error: client session lost. Please start over.",
                             reply_markup=get_back_to_menu_keyboard("manage_sessions", "⬅️ Back"))
        await state.clear()
        return

    try:
        logger.info(f"Attempting to sign in {phone} with 2FA password.")
        # Клиент уже должен быть подключен и иметь phone_code_hash
        signed_in_user = await client.sign_in(password=password)
        
        session_string = client.session.save()
        logger.info(f"Successfully signed in user (2FA): {signed_in_user.username if signed_in_user.username else signed_in_user.id}. Session string obtained.")

        # Сохраняем сессию (дублирование логики из обычного sign_in, можно вынести в функцию)
        sessions_data = await load_json_bot(bot_config.SESSIONS_JSON_PATH)
        sessions_data[phone] = session_string
        if await save_json_bot(bot_config.SESSIONS_JSON_PATH, sessions_data):
            logger.info(f"Session for {phone} (2FA) saved to {bot_config.SESSIONS_JSON_PATH.name}")
            
            stats_data = await load_json_bot(bot_config.STATS_JSON_PATH)
            if phone not in stats_data:
                user_name = getattr(signed_in_user, 'first_name', '') + \
                            (' ' + getattr(signed_in_user, 'last_name', '') if getattr(signed_in_user, 'last_name', '') else '') or \
                            getattr(signed_in_user, 'username', '') or f"ID:{signed_in_user.id}"
                stats_data[phone] = {
                    "name": user_name.strip(), "total_uses": 0, 
                    "last_active": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status_from_worker": "ok_new", "session_string_ref": session_string, "daily_usage": {}
                }
                if await save_json_bot(bot_config.STATS_JSON_PATH, stats_data):
                    logger.info(f"Initial stats entry for {phone} (2FA) created.")
                else: logger.error(f"Failed to save initial stats for {phone} (2FA).")

            await message.answer(
                f"✅ Session for `{phone}` (2FA) added successfully!\n"
                "FastAPI service might need a restart or session reload.",
                parse_mode="Markdown",
                reply_markup=get_manage_sessions_keyboard()
            )
        else:
            await message.answer(
                "❌ Failed to save the session string to file (2FA). Check bot logs.",
                reply_markup=get_manage_sessions_keyboard()
            )
        await state.clear()

    except errors.PasswordHashInvalidError: # Неправильный пароль 2FA
        logger.warning(f"Invalid 2FA password for {phone}.")
        await message.reply("❌ Incorrect 2FA password. Please try again or cancel.",
                            reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel"))
        # Остаемся в состоянии waiting_for_password
    except Exception as e:
        logger.error(f"Error signing in {phone} with 2FA password: {e}", exc_info=True)
        await message.answer(f"❌ An error occurred during 2FA sign-in: {e}\nPlease try again.",
                             reply_markup=get_manage_sessions_keyboard())
        await state.clear()
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info(f"Disconnected temporary Telethon client for {phone} after 2FA attempt.")


# --- Листинг сессий ---
PAGE_SIZE_SESSIONS = 5
CALLBACK_PREFIX_LIST_SESSIONS = "session_list_all" # Для пагинации
@router.callback_query(F.data.startswith(CALLBACK_PREFIX_LIST_SESSIONS))
async def cq_list_sessions(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} requested to list sessions.")
    await callback_query.answer("Fetching session list...")

    try:
        page = int(callback_query.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    
    sessions_data = await load_json_bot(bot_config.SESSIONS_JSON_PATH)
    stats_data = await load_json_bot(bot_config.STATS_JSON_PATH)
    fastapi_health_data = await get_fastapi_health() # Получаем статусы от FastAPI

    if not sessions_data:
        await callback_query.message.edit_text(
            "📱 **Telegram Sessions**\n\nNo sessions configured yet.",
            reply_markup=get_back_to_menu_keyboard("manage_sessions", "⬅️ Back")
        )
        return

    session_items_for_display = []
    for phone, session_str in sessions_data.items():
        stat_entry = stats_data.get(phone, {})
        name = stat_entry.get("name", "N/A")
        total_uses = stat_entry.get("total_uses", 0)
        
        # Получаем статус из FastAPI /health
        # FastAPI health_check возвращает детальные статусы в client_statuses_detailed
        # Ключ там: "Account Name (phone, SID)"
        # Нам нужно найти соответствующий SID (session_id)
        # SID - это последние 6 символов session_string
        session_id_short = session_str[-6:]
        current_status_from_fastapi = "Unknown (FastAPI unreachable or session not in health)"
        daily_usage_str = "N/A"

        if fastapi_health_data and not fastapi_health_data.get("error"):
            detailed_statuses = fastapi_health_data.get("clients_statuses_detailed", {})
            found_in_health = False
            for display_key, status_text in detailed_statuses.items():
                if f"...{session_id_short})" in display_key and phone in display_key: # Ищем по SID и телефону
                    current_status_from_fastapi = status_text # Статус уже включает дневное использование
                    found_in_health = True
                    # Извлекаем daily usage из status_text если возможно (например, "ok (today: 15/100)")
                    match_usage = re.search(r"\(today: (\d+/\d+)\)", status_text)
                    if match_usage:
                        daily_usage_str = match_usage.group(1)
                    break
            if not found_in_health:
                 current_status_from_fastapi = stat_entry.get("status_from_worker", "Not in FastAPI /health")
                 # Если не нашли в health, берем из локальной статистики
                 today_utc = time.strftime("%Y-%m-%d", time.gmtime())
                 daily_uses_today = stat_entry.get("daily_usage", {}).get(today_utc, 0)
                 limit_per_session = fastapi_health_data.get("daily_request_limit_per_session", 100) if fastapi_health_data else 100
                 daily_usage_str = f"{daily_uses_today}/{limit_per_session}"


        session_items_for_display.append({
            "id": phone, # Используем телефон как ID для выбора
            "text": f"📞 {phone} ({name})\n"
                    f" статуса: {current_status_from_fastapi}\n"
                    f"📊 Uses (Today/Total): {daily_usage_str} / {total_uses}"
        })
    
    # Сортировка по номеру телефона
    session_items_for_display.sort(key=lambda x: x["id"])
    
    total_pages = (len(session_items_for_display) + PAGE_SIZE_SESSIONS - 1) // PAGE_SIZE_SESSIONS
    
    # Формируем текст для текущей страницы
    start_index = page * PAGE_SIZE_SESSIONS
    end_index = start_index + PAGE_SIZE_SESSIONS
    current_page_items_text = [item['text'] for item in session_items_for_display[start_index:end_index]]
    
    message_text = "📱 **Configured Telegram Sessions** (Page {}/{})\n\n".format(page + 1, total_pages)
    if current_page_items_text:
        message_text += "\n\n---\n\n".join(current_page_items_text)
    else:
        message_text += "No sessions on this page."

    reply_markup = get_pagination_keyboard(
        action_prefix=CALLBACK_PREFIX_LIST_SESSIONS,
        current_page=page,
        total_pages=total_pages,
        back_menu_callback="manage_sessions"
    )
    
    try:
        await callback_query.message.edit_text(message_text, reply_markup=reply_markup, parse_mode=None) # Без Markdown для простоты
    except Exception as e:
        logger.error(f"Error editing message for session list: {e}")
        # Если ошибка, например, из-за длины, отправляем новым сообщением
        await callback_query.message.answer(message_text, reply_markup=reply_markup, parse_mode=None)


# --- Удаление сессии ---
CALLBACK_PREFIX_DELETE_SESSION_SELECT = "session_del_sel"
CALLBACK_PREFIX_DELETE_SESSION_PAGE = CALLBACK_PREFIX_DELETE_SESSION_SELECT + "_page" # Для пагинации выбора
CALLBACK_PREFIX_DELETE_SESSION_CONFIRM = "session_del_conf"

@router.callback_query(F.data == "session_delete_select")
@router.callback_query(F.data.startswith(CALLBACK_PREFIX_DELETE_SESSION_PAGE)) # Для пагинации
async def cq_delete_session_select(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} initiated session deletion - selection step.")
    
    page = 0
    if callback_query.data.startswith(CALLBACK_PREFIX_DELETE_SESSION_PAGE):
        try:
            page = int(callback_query.data.split(":")[-1])
        except: pass

    sessions_data = await load_json_bot(bot_config.SESSIONS_JSON_PATH)
    if not sessions_data:
        await callback_query.message.edit_text("No sessions to delete.", reply_markup=get_back_to_menu_keyboard("manage_sessions"))
        await callback_query.answer()
        return

    items_for_selection = [{"id": phone, "text": f"📞 {phone}"} for phone in sorted(sessions_data.keys())]
    
    reply_markup = get_item_selection_keyboard(
        items=items_for_selection,
        action_prefix=CALLBACK_PREFIX_DELETE_SESSION_SELECT, # Префикс для выбора конкретной сессии
        page=page,
        page_size=PAGE_SIZE_SESSIONS,
        back_menu_callback="manage_sessions"
    )
    await callback_query.message.edit_text(
        "➖ **Delete Session**\n\nSelect a session to delete:",
        reply_markup=reply_markup
    )
    await callback_query.answer()

@router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX_DELETE_SESSION_SELECT}:id:"))
async def cq_delete_session_confirm_prompt(callback_query: CallbackQuery, state: FSMContext):
    phone_to_delete = callback_query.data.split(":")[-1].replace("_",":") # Восстанавливаем, если были замены
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} selected session {phone_to_delete} for deletion confirmation.")
    
    await state.update_data(phone_to_delete=phone_to_delete) # Сохраняем в FSM для подтверждения
    
    await callback_query.message.edit_text(
        f"⚠️ **Confirm Deletion** ⚠️\n\n"
        f"Are you sure you want to delete the session for phone number `{phone_to_delete}`?\n"
        "This will remove it from `sessions.json` and `usage_stats.json`.",
        parse_mode="Markdown",
        reply_markup=get_confirmation_keyboard(CALLBACK_PREFIX_DELETE_SESSION_CONFIRM, item_id=phone_to_delete)
    )
    await callback_query.answer()

@router.callback_query(F.data.startswith(CALLBACK_PREFIX_DELETE_SESSION_CONFIRM))
async def cq_delete_session_action(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    parts = callback_query.data.split(":")
    action = parts[2]
    phone_to_delete = parts[4] if len(parts) > 4 else (await state.get_data()).get("phone_to_delete")

    if not phone_to_delete:
        logger.error(f"Phone to delete not found in callback or state for admin {user_info}.")
        await callback_query.message.edit_text("Error: Could not determine which session to delete. Please try again.",
                                               reply_markup=get_manage_sessions_keyboard())
        await state.clear()
        await callback_query.answer("Error.", show_alert=True)
        return

    if action == CONFIRM_YES:
        logger.info(f"Admin {user_info} confirmed deletion of session {phone_to_delete}.")
        
        sessions_data = await load_json_bot(bot_config.SESSIONS_JSON_PATH)
        stats_data = await load_json_bot(bot_config.STATS_JSON_PATH)
        
        session_deleted = False
        if phone_to_delete in sessions_data:
            del sessions_data[phone_to_delete]
            if await save_json_bot(bot_config.SESSIONS_JSON_PATH, sessions_data):
                logger.info(f"Session {phone_to_delete} removed from sessions.json.")
                session_deleted = True
            else:
                logger.error(f"Failed to save sessions.json after deleting {phone_to_delete}.")
        
        stats_deleted = False
        if phone_to_delete in stats_data:
            del stats_data[phone_to_delete]
            if await save_json_bot(bot_config.STATS_JSON_PATH, stats_data):
                logger.info(f"Stats for {phone_to_delete} removed from usage_stats.json.")
                stats_deleted = True
            else:
                logger.error(f"Failed to save usage_stats.json after deleting stats for {phone_to_delete}.")

        if session_deleted or stats_deleted:
            await callback_query.message.edit_text(
                f"✅ Session and/or stats for `{phone_to_delete}` have been deleted.\n"
                "FastAPI service might need a restart or session reload.",
                parse_mode="Markdown",
                reply_markup=get_manage_sessions_keyboard()
            )
        else:
            await callback_query.message.edit_text(
                f"❌ Failed to delete session or stats for `{phone_to_delete}` (or already deleted). Check logs.",
                parse_mode="Markdown",
                reply_markup=get_manage_sessions_keyboard()
            )
    elif action == CONFIRM_NO:
        logger.info(f"Admin {user_info} canceled deletion of session {phone_to_delete}.")
        await callback_query.message.edit_text(
            "🚫 Session deletion canceled.",
            reply_markup=get_manage_sessions_keyboard() # Возврат в меню сессий
        )
    
    await state.clear()
    await callback_query.answer()


# --- Заморозка/Разморозка сессий (TODO) ---
# Требует эндпоинтов в FastAPI или прямого изменения файла статусов (не рекомендуется)
# Для примера, если бы был эндпоинт в FastAPI:
# @router.callback_query(F.data == "session_freeze_select")
# async def cq_freeze_session_select(callback_query: CallbackQuery, state: FSMContext):
#     await callback_query.message.edit_text("❄️ Session Freeze: This feature is not yet implemented via API.\n"
#                                            "You would select a session here, then confirm.",
#                                            reply_markup=get_back_to_menu_keyboard("manage_sessions"))
#     await callback_query.answer()

# @router.callback_query(F.data == "session_unfreeze_select")
# async def cq_unfreeze_session_select(callback_query: CallbackQuery, state: FSMContext):
#     await callback_query.message.edit_text("☀️ Session Unfreeze: This feature is not yet implemented via API.",
#                                            reply_markup=get_back_to_menu_keyboard("manage_sessions"))
#     await callback_query.answer()
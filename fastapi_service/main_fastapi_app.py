import asyncio
import datetime
import functools
import json
import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Union

import pytz
from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks, Header
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.security.api_key import APIKeyHeader, APIKey
from pydantic import HttpUrl, ValidationError
from telethon import TelegramClient, errors

# Импорт наших модулей
from . import config, models
from .file_utils import download_file, cleanup_temp_file, cleanup_temp_directory, parse_content_disposition
from .s3_logic import upload_file_to_s3, s3_client, construct_s3_public_url
from .telegram_logic import (
    connect_single_client,
    select_client_with_lock,
    process_link_with_telegram,
    update_stats_on_request,
    update_session_worker_status_in_stats,
)
from .utils import (
    setup_logging,
    get_logger,
    load_json_data,
    save_json_data,
    get_request_id,
    get_utc_now,
    get_utc_today_str,
)

# --- Глобальное состояние приложения ---
# Эти переменные будут инициализированы в lifespan context manager (on_startup)

# Словарь для хранения активных клиентов Telethon: {session_string: TelegramClient}
clients: Dict[str, TelegramClient] = {}

# Словарь для статусов клиентов: {session_string: "ok" | "error" | "auth_error" | "deactivated" | "expired" | "timeout_connect" | "flood_wait_TIMESTAMP" | "daily_limit_reached_YYYY-MM-DD" | ...}
client_status: Dict[str, str] = {}

# Словарь для блокировок по каждому клиенту: {session_string: asyncio.Lock}
client_locks: Dict[str, asyncio.Lock] = {}

# Словарь для деталей клиентов: {session_string: models.TelegramClientDetails}
client_details: Dict[str, models.TelegramClientDetails] = {}

# Словарь для времени окончания кулдауна сессии: {session_string: float_timestamp}
client_cooldown_end_times: Dict[str, float] = {}

# Общее состояние приложения
app_state: Dict[str, Any] = {
    "clients_initialized": False,
    "current_stats": {},  # Кэш файла usage_stats.json
    "session_to_phone_map": {}, # {session_string: phone_number_hint}
    "phone_to_session_map": {}, # {phone_number_hint: session_string} - для удобства в некоторых местах
    "webhook_tasks_db": {}, # Кэш файла webhook_tasks.json
    "active_async_tasks": set(), # Множество ID активных асинхронных задач
}

# Блокировки для доступа к файлам и критическим секциям
sessions_file_lock_fastapi = asyncio.Lock()
stats_file_lock_fastapi = asyncio.Lock()
webhook_db_lock_fastapi = asyncio.Lock()
select_client_lock = asyncio.Lock() # Глобальная блокировка для выбора клиента

# Настройка логирования (вызывается один раз при старте)
# setup_logging() будет вызвано в on_startup после того, как config будет полностью загружен
logger = get_logger(__name__) # Базовый логгер для этого модуля

# --- API Key Security ---
api_key_header_auth = APIKeyHeader(name=config.API_KEY_NAME_HEADER, auto_error=False)

async def verify_api_key(api_key_header: Optional[str] = Depends(api_key_header_auth)):
    if not config.FASTAPI_CLIENT_API_KEY: # Если ключ не установлен, доступ публичный
        return True
    if not api_key_header:
        logger.warning(f"Missing API Key. Header: {config.API_KEY_NAME_HEADER}")
        raise HTTPException(status_code=401, detail="Not authenticated: API Key is missing.")
    if api_key_header == config.FASTAPI_CLIENT_API_KEY:
        return True
    logger.warning(f"Invalid API Key provided: '{api_key_header[:10]}...'")
    raise HTTPException(status_code=403, detail="Could not validate credentials: Invalid API Key.")


# --- FastAPI Lifespan Events ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Действия при запуске приложения (startup)
    main_startup_logger = get_logger("app_startup")
    main_startup_logger.info(f"Application startup. Version: {config.APP_VERSION}")
    
    setup_logging(log_level=config.LOG_LEVEL, log_file=config.LOG_FILE_PATH_FOR_DOWNLOAD) # Настройка логирования
    main_startup_logger.info("Logging configured.")

    config.TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    main_startup_logger.info(f"Temporary download directory ensured: {config.TEMP_DOWNLOAD_DIR}")

    # Инициализация клиентов Telegram
    await initialize_telegram_clients()

    # Загрузка базы данных задач webhook и возобновление незавершенных задач
    await load_and_resume_webhook_tasks()

    main_startup_logger.info("Application startup complete.")
    yield
    # Действия при остановке приложения (shutdown)
    main_shutdown_logger = get_logger("app_shutdown")
    main_shutdown_logger.info("Application shutdown sequence started.")

    await cleanup_telegram_clients()
    
    # Очистка временных файлов (только те, что созданы этим приложением, если есть механизм)
    # Пока что просто очищаем все старше определенного времени или всю директорию
    # Рекомендуется более гранулярная очистка, если в этой папке могут быть другие файлы
    await cleanup_temp_directory(config.TEMP_DOWNLOAD_DIR, older_than_hours=24) # Удаляем файлы старше 24 часов
    # или полная очистка: await cleanup_temp_directory(config.TEMP_DOWNLOAD_DIR)

    # Дождаться завершения активных фоновых задач (если это необходимо и возможно)
    # Это сложная задача, так как задачи могут быть длительными.
    # Можно использовать asyncio.gather с таймаутом.
    if app_state["active_async_tasks"]:
        main_shutdown_logger.info(f"Waiting for {len(app_state['active_async_tasks'])} active async tasks to complete (max 30s)...")
        # Это не остановит задачи, а лишь подождет их некоторое время.
        # Для реальной отмены нужен более сложный механизм с asyncio.Task.cancel()
        # и обработкой CancelledError внутри задач.
        # await asyncio.wait(list(app_state["active_async_tasks"]), timeout=30) # Это неверно, т.к. храним ID, а не Task объекты
        # Вместо этого, просто логируем, что задачи могут продолжать выполняться.
        main_shutdown_logger.warning(f"{len(app_state['active_async_tasks'])} tasks were active. They might continue if not completed.")


    main_shutdown_logger.info("Application shutdown complete.")

# --- FastAPI App Instance ---
app = FastAPI(
    title="Telegram File Processor and S3 Uploader",
    version=config.APP_VERSION,
    description="An advanced FastAPI service to process URLs via Telegram and upload files to S3.",
    lifespan=lifespan,
    # dependencies=[Depends(verify_api_key)] # Можно установить глобальную зависимость
)

# --- Helper Functions for Lifespan ---
async def initialize_telegram_clients():
    init_logger = get_logger("tg_client_init")
    init_logger.info("Initializing Telegram clients...")
    app_state["clients_initialized"] = False

    sessions_data = await load_json_data(config.SESSIONS_FILE_PATH, sessions_file_lock_fastapi)
    if not sessions_data:
        init_logger.warning(f"No session data found in {config.SESSIONS_FILE_PATH}. No clients will be initialized.")
        app_state["clients_initialized"] = True # Считаем инициализацию завершенной, хоть и без клиентов
        return

    # Загрузка статистики для использования в select_client и health_check
    app_state["current_stats"] = await load_json_data(config.STATS_FILE_PATH, stats_file_lock_fastapi)
    init_logger.info(f"Loaded {len(app_state['current_stats'])} entries from stats file.")

    # Populate session_to_phone_map and phone_to_session_map
    for phone_str, session_str_val in sessions_data.items():
        if not isinstance(session_str_val, str) or not session_str_val.strip():
            init_logger.warning(f"Invalid session string for phone {phone_str} in {config.SESSIONS_FILE_PATH}. Skipping.")
            continue
        app_state["session_to_phone_map"][session_str_val] = phone_str
        app_state["phone_to_session_map"][phone_str] = session_str_val
        if session_str_val not in client_locks: # Создаем блокировку для каждой сессии
            client_locks[session_str_val] = asyncio.Lock()

    init_logger.info(f"Found {len(sessions_data)} sessions in {config.SESSIONS_FILE_PATH}.")
    
    connect_tasks = []
    for phone_number_hint, session_string in sessions_data.items():
        if not isinstance(session_string, str) or not session_string.strip():
            continue # Уже проверено выше, но на всякий случай
        
        req_id_connect = f"init_conn_{phone_number_hint.replace('+', '')[-4:]}"
        # Передаем все необходимые зависимости в connect_single_client
        task = asyncio.create_task(connect_single_client(
            session_str=session_string,
            phone_hint=phone_number_hint,
            request_id=req_id_connect,
            clients=clients,
            client_status=client_status,
            client_details=client_details,
            stats_file_path=str(config.STATS_FILE_PATH),
            stats_file_lock=stats_file_lock_fastapi,
            app_state=app_state
        ))
        connect_tasks.append(task)

    if connect_tasks:
        await asyncio.gather(*connect_tasks, return_exceptions=True) # Собираем результаты, чтобы не упасть на первой ошибке

    # Логирование итогов инициализации
    active_count = sum(1 for s, status in client_status.items() if status == "ok" and s in clients and clients[s].is_connected())
    error_count = sum(1 for status in client_status.values() if "error" in status or status in ["auth_key_error", "deactivated", "expired", "timeout_connect"])
    flood_wait_count = sum(1 for status in client_status.values() if status.startswith("flood_wait_"))
    
    init_logger.info(
        f"Telegram client initialization complete. Total configured: {len(sessions_data)}. "
        f"Active (connected & authorized): {active_count}. Errors: {error_count}. Flood Wait: {flood_wait_count}."
    )
    app_state["clients_initialized"] = True


async def cleanup_telegram_clients():
    cleanup_logger = get_logger("tg_client_cleanup")
    cleanup_logger.info("Cleaning up Telegram clients...")
    active_clients_list = list(clients.values()) # Копируем, так как будем изменять словарь clients
    
    disconnect_tasks = []
    for client_instance in active_clients_list:
        if client_instance and client_instance.is_connected():
            cleanup_logger.info(f"Disconnecting client: {client_instance.session.save()[:10]}...")
            disconnect_tasks.append(client_instance.disconnect())
    
    if disconnect_tasks:
        results = await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                cleanup_logger.error(f"Error disconnecting client {active_clients_list[i].session.save()[:10]}: {res}")
    
    clients.clear()
    client_status.clear()
    client_cooldown_end_times.clear()
    client_details.clear()
    client_locks.clear() # Блокировки тоже очищаем
    app_state["session_to_phone_map"].clear()
    app_state["phone_to_session_map"].clear()
    cleanup_logger.info("Telegram clients cleaned up.")

# --- Webhook Task Processing Logic ---

async def _send_intermediate_webhook(task_id: str, webhook_url: HttpUrl, payload: Dict, task_logger: logging.LoggerAdapter):
    """Helper to send intermediate webhook updates."""
    if not webhook_url:
        return

    task_logger.info(f"Sending intermediate webhook for task {task_id}, status: {payload.get('status')}, to {webhook_url}")
    try:
        async with httpx.AsyncClient(timeout=config.WEBHOOK_SEND_TIMEOUT, headers={"User-Agent": config.DEFAULT_USER_AGENT}) as client:
            response = await client.post(str(webhook_url), json=payload)
            response.raise_for_status()
            task_logger.info(f"Intermediate webhook for task {task_id} sent successfully to {webhook_url}. Status: {response.status_code}")
    except httpx.HTTPStatusError as e:
        task_logger.error(f"Intermediate webhook for task {task_id} to {webhook_url} failed with status {e.response.status_code}: {e.response.text[:200]}")
    except httpx.RequestError as e:
        task_logger.error(f"Intermediate webhook for task {task_id} to {webhook_url} failed with request error: {e}")
    except Exception as e:
        task_logger.error(f"Unexpected error sending intermediate webhook for task {task_id} to {webhook_url}: {e}", exc_info=True)


async def process_link_download_upload_task(
    original_url: HttpUrl,
    task_id: str,
    webhook_url_to_send: Optional[HttpUrl], # Может быть None
    client_metadata: Optional[Dict[str, Any]],
    _client_acquire_attempt: int = 1 # Для отслеживания попыток получения клиента
):
    """
    The main background task for asynchronous processing.
    Handles Telegram interaction, file download, S3 upload, and final webhook.
    """
    task_logger = get_logger(f"async_task.{task_id}", task_id)
    app_state["active_async_tasks"].add(task_id) # Регистрируем активную задачу

    # --- Инициализация переменных задачи ---
    s3_main_file_key: Optional[str] = None
    main_file_original_name: Optional[str] = None
    main_file_size_bytes: Optional[int] = None
    s3_license_file_key: Optional[str] = None
    license_file_original_name: Optional[str] = None
    license_file_size_bytes: Optional[int] = None
    
    temp_files_registry_in_task: List[str] = [] # Список путей к временным файлам для очистки
    
    error_message_for_webhook: Optional[str] = None
    current_task_stage_error_short: Optional[str] = None # e.g., NoClient, TGError, DownloadFail, UploadFail
    
    # Для восстановления состояния при перезапуске или если клиент не был получен сразу
    session_str_used_in_task: Optional[str] = None
    lock_for_task: Optional[asyncio.Lock] = None # Блокировка выбранного клиента
    acc_name_in_task: Optional[str] = None
    phone_number_used_in_task: Optional[str] = None

    # --- Загрузка/обновление информации о задаче в БД ---
    async def update_task_in_db(updates: Dict[str, Any]):
        async with webhook_db_lock_fastapi:
            task_data = app_state["webhook_tasks_db"].get(task_id)
            if task_data:
                task_data.update(updates)
                task_data["status_updated_at"] = get_utc_now().isoformat()
                app_state["webhook_tasks_db"][task_id] = task_data # Обновляем кэш
                await save_json_data(config.WEBHOOK_DB_FILE, app_state["webhook_tasks_db"], webhook_db_lock_fastapi) # Сохраняем в файл

    try:
        # --- Начало обработки задачи ---
        if _client_acquire_attempt == 1: # Только при первом запуске задачи
            task_logger.info(f"Starting task: URL={original_url}, Webhook={webhook_url_to_send}, Meta={client_metadata}")
            await update_task_in_db({"status": "processing_started", "metadata": client_metadata or {}})
            if webhook_url_to_send:
                payload = models.WebhookProcessingUpdatePayload(
                    task_id=task_id,
                    original_url=original_url,
                    metadata=client_metadata,
                    status="processing_started",
                    message="Task processing has started."
                ).model_dump(exclude_none=True)
                await _send_intermediate_webhook(task_id, webhook_url_to_send, payload, task_logger)
        
        # --- Этап 1: Получение ссылок из Telegram ---
        task_logger.info("Stage 1: Get links from Telegram.")
        
        # Проверка, не были ли ссылки уже получены (например, при возобновлении задачи)
        current_task_data_from_db = {}
        async with webhook_db_lock_fastapi: # Только для чтения из кэша
            current_task_data_from_db = app_state["webhook_tasks_db"].get(task_id, {})

        main_url_from_bot_restored = current_task_data_from_db.get("main_download_url_from_bot")
        license_url_from_bot_restored = current_task_data_from_db.get("license_download_url_from_bot")
        
        # Статусы, при которых мы можем пропустить получение ссылок
        resumable_link_statuses = ["links_retrieved_pending_s3_upload", "processing_s3_upload"]
        
        already_has_links = False
        if main_url_from_bot_restored and current_task_data_from_db.get("status") in resumable_link_statuses:
            task_logger.info("Links already retrieved for this task (resuming). Skipping Telegram interaction.")
            already_has_links = True
            # Восстанавливаем информацию о клиенте, если она была сохранена
            acc_name_in_task = current_task_data_from_db.get("processed_by_account")
            phone_number_used_in_task = current_task_data_from_db.get("processed_by_phone_number")
            # session_str_used_in_task не хранится в DB напрямую, но нужен для статистики
            if phone_number_used_in_task:
                session_str_used_in_task = app_state.get("phone_to_session_map", {}).get(phone_number_used_in_task)

        if not already_has_links:
            await update_task_in_db({"status": "processing_link_retrieval"})
            task_logger.info("Attempting to select a Telegram client...")
            
            selected_client_info = await select_client_with_lock(
                select_client_lock=select_client_lock,
                clients=clients,
                client_status=client_status,
                client_locks=client_locks,
                client_details=client_details,
                client_cooldown_end_times=client_cooldown_end_times,
                app_state=app_state,
                stats_file_path=str(config.STATS_FILE_PATH),
                stats_file_lock=stats_file_lock_fastapi
            )

            if not selected_client_info:
                task_logger.warning(f"No Telegram client available (attempt {_client_acquire_attempt}/{config.MAX_CLIENT_ACQUIRE_RETRIES}).")
                if _client_acquire_attempt < config.MAX_CLIENT_ACQUIRE_RETRIES:
                    new_status_waiting = f"waiting_for_client_attempt_{_client_acquire_attempt + 1}"
                    await update_task_in_db({"status": new_status_waiting})
                    if webhook_url_to_send:
                        payload_retry = models.WebhookProcessingUpdatePayload(
                            task_id=task_id, original_url=original_url, metadata=client_metadata,
                            status="retrying_no_client",
                            message=f"No client available, will retry (attempt {_client_acquire_attempt + 1}/{config.MAX_CLIENT_ACQUIRE_RETRIES})."
                        ).model_dump(exclude_none=True)
                        await _send_intermediate_webhook(task_id, webhook_url_to_send, payload_retry, task_logger)
                    
                    task_logger.info(f"Scheduling retry for task {task_id} in {config.CLIENT_ACQUIRE_RETRY_DELAY}s.")
                    # Создаем новую задачу для повторной попытки
                    # Важно: не использовать background_tasks.add_task здесь, т.к. мы уже в фоновой задаче
                    # Вместо этого, используем asyncio.create_task, но нужно убедиться, что он не потеряется
                    # Лучше, если основной цикл приложения может подхватывать такие задачи.
                    # Для простоты, здесь мы создаем задачу и надеемся, что она выполнится.
                    # В более сложных системах может потребоваться очередь.
                    async def delayed_retry():
                        await asyncio.sleep(config.CLIENT_ACQUIRE_RETRY_DELAY)
                        # Рекурсивный вызов с увеличенным счетчиком попыток
                        asyncio.create_task(process_link_download_upload_task(
                            original_url, task_id, webhook_url_to_send, client_metadata, _client_acquire_attempt + 1
                        ))
                    asyncio.create_task(delayed_retry())
                    # Текущая задача завершается, т.к. клиент не получен
                    app_state["active_async_tasks"].discard(task_id) # Убираем из активных, т.к. эта инстанция завершается
                    return # Важно выйти, чтобы не продолжать без клиента

                else: # Все попытки исчерпаны
                    error_message_for_webhook = "NoClientAvailableAfterRetries"
                    current_task_stage_error_short = "NoClient"
                    task_logger.error(f"Failed to acquire client for task {task_id} after {config.MAX_CLIENT_ACQUIRE_RETRIES} attempts.")
                    raise Exception(error_message_for_webhook) # Переходим в блок except

            session_str_used_in_task, tg_client, lock_for_task, acc_name_in_task, phone_number_used_in_task = selected_client_info
            task_logger.info(f"Selected client: {acc_name_in_task} ({phone_number_used_in_task}, ...{session_str_used_in_task[-6:]}) for task {task_id}.")
            
            await update_task_in_db({
                "status": "processing_link_retrieval_active", # Более точный статус
                "processed_by_account": acc_name_in_task,
                "processed_by_phone_number": phone_number_used_in_task
            })

            result_from_telegram: Dict[str, Optional[str]] = {}
            async with lock_for_task: # Блокируем выбранного клиента на время использования
                task_logger.debug(f"Acquired lock for client {acc_name_in_task} ({phone_number_used_in_task}).")
                # Повторная проверка статуса клиента, так как он мог измениться между выбором и захватом блокировки
                if client_status.get(session_str_used_in_task) != "ok":
                    error_message_for_webhook = f"ClientBecameUnavailableBeforeUse:{client_status.get(session_str_used_in_task)}"
                    current_task_stage_error_short = "ClientUnavailable"
                    task_logger.error(f"Client {acc_name_in_task} status changed to '{client_status.get(session_str_used_in_task)}' before use. Releasing lock and failing task.")
                    # Не меняем статус клиента здесь, т.к. он уже не 'ok'
                    # Кулдаун не применяем, т.к. клиент не использовался
                    raise Exception(error_message_for_webhook)

                result_from_telegram = await process_link_with_telegram(
                    client=tg_client,
                    url_to_process=str(original_url),
                    account_name=acc_name_in_task,
                    phone_number=phone_number_used_in_task,
                    request_id=task_id,
                    client_status_dict=client_status, # Передаем глобальный client_status
                    stats_file_path_str=str(config.STATS_FILE_PATH),
                    stats_file_lock=stats_file_lock_fastapi,
                    app_state=app_state
                )
                
                # Применяем кулдаун к сессии после использования
                cooldown_duration = random.uniform(config.SESSION_REQUEST_DELAY_MIN, config.SESSION_REQUEST_DELAY_MAX)
                client_cooldown_end_times[session_str_used_in_task] = time.time() + cooldown_duration
                task_logger.info(f"Applied cooldown of {cooldown_duration:.2f}s to session {phone_number_used_in_task} (...{session_str_used_in_task[-6:]}).")

            task_logger.debug(f"Released lock for client {acc_name_in_task} ({phone_number_used_in_task}).")
            lock_for_task = None # Сбрасываем, т.к. он больше не нужен

            if result_from_telegram.get('error'):
                error_message_for_webhook = f"TG Error ({result_from_telegram.get('telegram_error_type', 'UnknownTGError')}): {result_from_telegram['error']}"
                current_task_stage_error_short = result_from_telegram.get('telegram_error_type', 'TGError')
                task_logger.error(f"Error from Telegram processing: {error_message_for_webhook}")
                # Статус клиента уже должен быть обновлен внутри process_link_with_telegram
                raise Exception(error_message_for_webhook)

            main_url_from_bot_restored = result_from_telegram.get('main_url')
            license_url_from_bot_restored = result_from_telegram.get('license_url')

            if not main_url_from_bot_restored:
                error_message_for_webhook = "MainUrlNotReceivedFromBot"
                current_task_stage_error_short = "TGMainUrlMissing"
                task_logger.error("Main URL not received from bot, though process_link_with_telegram reported no error.")
                raise Exception(error_message_for_webhook)
            
            # Обновляем статистику использования сессии (инкремент счетчиков)
            # Это должно происходить ПОСЛЕ успешного получения ссылок из Telegram
            await update_stats_on_request(
                phone_number=phone_number_used_in_task,
                account_name=acc_name_in_task,
                request_id=task_id,
                stats_file_path=str(config.STATS_FILE_PATH),
                stats_file_lock=stats_file_lock_fastapi,
                app_state=app_state
            )
            # Проверка дневного лимита после инкремента
            today_utc_str_stats = get_utc_today_str()
            current_daily_uses = app_state.get("current_stats", {}).get(phone_number_used_in_task, {}).get("daily_usage", {}).get(today_utc_str_stats, 0)
            if current_daily_uses >= config.DAILY_REQUEST_LIMIT_PER_SESSION:
                limit_status_str = f"daily_limit_reached_{today_utc_str_stats}"
                task_logger.warning(f"Session {phone_number_used_in_task} reached daily limit ({current_daily_uses}/{config.DAILY_REQUEST_LIMIT_PER_SESSION}) after this request.")
                client_status[session_str_used_in_task] = limit_status_str
                await update_session_worker_status_in_stats(
                    phone_number_used_in_task, session_str_used_in_task, limit_status_str, acc_name_in_task, task_id,
                    str(config.STATS_FILE_PATH), stats_file_lock_fastapi, app_state
                )


            await update_task_in_db({
                "status": "links_retrieved_pending_s3_upload",
                "main_download_url_from_bot": str(main_url_from_bot_restored) if main_url_from_bot_restored else None,
                "license_download_url_from_bot": str(license_url_from_bot_restored) if license_url_from_bot_restored else None,
            })
            if webhook_url_to_send:
                payload_links = models.WebhookProcessingUpdatePayload(
                    task_id=task_id, original_url=original_url, metadata=client_metadata,
                    status="links_retrieved",
                    message="File links retrieved from Telegram.",
                    processed_by_phone_number=phone_number_used_in_task
                ).model_dump(exclude_none=True)
                # Добавляем сами ссылки в промежуточный вебхук, если это полезно клиенту
                # payload_links["data"] = {"main_link": str(main_url_from_bot_restored), "license_link": str(license_url_from_bot_restored) if license_url_from_bot_restored else None}
                await _send_intermediate_webhook(task_id, webhook_url_to_send, payload_links, task_logger)
        
        # --- Этап 2: Скачивание файлов и загрузка в S3 ---
        task_logger.info("Stage 2: Download files and upload to S3.")
        await update_task_in_db({"status": "processing_s3_upload"})

        if not main_url_from_bot_restored: # Должно быть установлено на предыдущем шаге или при возобновлении
            error_message_for_webhook = "InternalErrorMissingDownloadUrlForS3Stage"
            current_task_stage_error_short = "InternalError"
            task_logger.critical("Main download URL is missing at S3 processing stage. This should not happen.")
            raise Exception(error_message_for_webhook)

        # Скачивание основного файла
        task_logger.info(f"Downloading main file from: {main_url_from_bot_restored}")
        main_dl_result = await download_file(str(main_url_from_bot_restored), task_id, "main")
        if not main_dl_result:
            error_message_for_webhook = "MainFileDownloadFailed"
            current_task_stage_error_short = "DownloadFailMain"
            task_logger.error(f"Failed to download main file from {main_url_from_bot_restored}")
            raise Exception(error_message_for_webhook)
        
        temp_main_file_path, main_s3_filename_part, main_file_original_name_res, main_file_size_bytes_res = main_dl_result
        temp_files_registry_in_task.append(temp_main_file_path)
        main_file_original_name = main_file_original_name_res
        main_file_size_bytes = main_file_size_bytes_res
        task_logger.info(f"Main file downloaded to {temp_main_file_path}, original_name='{main_file_original_name}', size={main_file_size_bytes} bytes.")

        # Загрузка основного файла в S3
        if config.S3_CONFIGURED and s3_client:
            task_logger.info(f"Uploading main file '{main_file_original_name}' to S3.")
            s3_main_file_key = await upload_file_to_s3(
                temp_main_file_path, task_id, main_s3_filename_part, main_file_original_name
            )
            if not s3_main_file_key:
                error_message_for_webhook = "MainFileUploadFailedS3"
                current_task_stage_error_short = "UploadFailS3Main"
                task_logger.error(f"Failed to upload main file {main_file_original_name} to S3.")
                raise Exception(error_message_for_webhook)
            task_logger.info(f"Main file uploaded to S3 with key: {s3_main_file_key}")
            await update_task_in_db({
                "s3_main_file_key": s3_main_file_key,
                "s3_main_file_original_name": main_file_original_name,
                "s3_main_file_size_bytes": main_file_size_bytes
            })
        else:
            task_logger.warning("S3 not configured, skipping upload for main file. Task will complete without S3 URLs.")
            # В этом случае s3_main_file_key останется None

        # Скачивание и загрузка файла лицензии (если есть)
        if license_url_from_bot_restored:
            task_logger.info(f"Downloading license file from: {license_url_from_bot_restored}")
            lic_dl_result = await download_file(str(license_url_from_bot_restored), task_id, "license")
            if lic_dl_result:
                temp_license_file_path, license_s3_filename_part, license_file_original_name_res, license_file_size_bytes_res = lic_dl_result
                temp_files_registry_in_task.append(temp_license_file_path)
                license_file_original_name = license_file_original_name_res
                license_file_size_bytes = license_file_size_bytes_res
                task_logger.info(f"License file downloaded to {temp_license_file_path}, original_name='{license_file_original_name}', size={license_file_size_bytes} bytes.")

                if config.S3_CONFIGURED and s3_client:
                    task_logger.info(f"Uploading license file '{license_file_original_name}' to S3.")
                    s3_license_file_key = await upload_file_to_s3(
                        temp_license_file_path, task_id, license_s3_filename_part, license_file_original_name
                    )
                    if s3_license_file_key:
                        task_logger.info(f"License file uploaded to S3 with key: {s3_license_file_key}")
                        await update_task_in_db({
                            "s3_license_file_key": s3_license_file_key,
                            "s3_license_file_original_name": license_file_original_name,
                            "s3_license_file_size_bytes": license_file_size_bytes
                        })
                    else:
                        task_logger.warning(f"Failed to upload license file {license_file_original_name} to S3. Continuing without it.")
                else:
                    task_logger.warning("S3 not configured, skipping upload for license file.")
            else:
                task_logger.warning(f"Failed to download license file from {license_url_from_bot_restored}. Continuing without it.")
        
        task_logger.info(f"Task {task_id} completed successfully.")
        # error_message_for_webhook останется None, что означает успех

    except Exception as e:
        task_logger.error(f"Exception in task {task_id}: {type(e).__name__} - {str(e)}", exc_info=True)
        if not error_message_for_webhook: # Если ошибка не была установлена ранее
            error_message_for_webhook = str(e)
        if not current_task_stage_error_short:
            current_task_stage_error_short = type(e).__name__

        # Дополнительная логика обработки статуса клиента при ошибке в задаче
        # Только если ошибка не "BotReportedOopsError" и сессия была выбрана и использовалась
        if session_str_used_in_task and \
           current_task_stage_error_short != "BotReportedOopsError" and \
           client_status.get(session_str_used_in_task) == "ok":
            
            # Определяем, виноват ли клиент в ошибке
            # Например, если это не ошибка S3 (типа S3 недоступен) или не ошибка отсутствия клиента
            is_client_fault = True
            if current_task_stage_error_short in ["NoClient", "ClientUnavailable", "InternalError"] or \
               "S3" in current_task_stage_error_short.upper() or \
               "DownloadFail" in current_task_stage_error_short: # Ошибки скачивания могут быть не по вине клиента ТГ
                is_client_fault = False
            
            if is_client_fault:
                task_logger.warning(f"Marking client {phone_number_used_in_task} (...{session_str_used_in_task[-6:]}) as 'error_task_fail' due to task failure: {current_task_stage_error_short}")
                client_status[session_str_used_in_task] = "error_task_fail"
                await update_session_worker_status_in_stats(
                    phone_number_used_in_task, session_str_used_in_task, "error_task_fail",
                    acc_name_in_task, task_id,
                    str(config.STATS_FILE_PATH), stats_file_lock_fastapi, app_state
                )
    finally:
        # --- Очистка временных файлов ---
        task_logger.info(f"Cleaning up temporary files for task {task_id}: {temp_files_registry_in_task}")
        for temp_file in temp_files_registry_in_task:
            await cleanup_temp_file(temp_file, task_id)
        
        # --- Отправка финального Webhook ---
        final_task_status_in_db: str
        webhook_db_updates: Dict[str, Any] = {}

        if webhook_url_to_send:
            task_logger.info(f"Preparing final webhook for task {task_id} to {webhook_url_to_send}")
            payload_to_send: Dict[str, Any]
            
            if error_message_for_webhook:
                payload_to_send_model = models.WebhookErrorPayload(
                    task_id=task_id,
                    original_url=original_url,
                    metadata=client_metadata,
                    processed_by_phone_number=phone_number_used_in_task,
                    error_message=error_message_for_webhook,
                    error_type=current_task_stage_error_short
                )
                payload_to_send = payload_to_send_model.model_dump(exclude_none=True)
                final_task_status_in_db = f"failed_{current_task_stage_error_short[:30].replace(' ', '_')}" # Ограничиваем длину и заменяем пробелы
                webhook_db_updates.update({
                    "status": final_task_status_in_db,
                    "error_details": error_message_for_webhook,
                    "error_type": current_task_stage_error_short,
                    "completed_at": get_utc_now().isoformat() # Задача завершена (с ошибкой)
                })
                task_logger.info(f"Task {task_id} failed. Final status: {final_task_status_in_db}")
            else: # Успех
                payload_to_send_model = models.WebhookSuccessPayload(
                    task_id=task_id,
                    original_url=original_url,
                    metadata=client_metadata,
                    processed_by_phone_number=phone_number_used_in_task,
                    main_file_url=construct_s3_public_url(s3_main_file_key) if s3_main_file_key else str(main_url_from_bot_restored), # Возвращаем URL от бота, если S3 не настроен
                    main_file_original_name=main_file_original_name,
                    main_file_s3_key=s3_main_file_key,
                    main_file_size_bytes=main_file_size_bytes,
                    license_file_url=construct_s3_public_url(s3_license_file_key) if s3_license_file_key else (str(license_url_from_bot_restored) if license_url_from_bot_restored else None),
                    license_file_original_name=license_file_original_name,
                    license_file_s3_key=s3_license_file_key,
                    license_file_size_bytes=license_file_size_bytes
                )
                payload_to_send = payload_to_send_model.model_dump(exclude_none=True)
                final_task_status_in_db = "completed"
                webhook_db_updates.update({
                    "status": final_task_status_in_db,
                    "completed_at": get_utc_now().isoformat(),
                    # s3 ключи и имена уже должны быть обновлены в DB по ходу дела
                })
                task_logger.info(f"Task {task_id} succeeded. Final status: {final_task_status_in_db}")

            # Обновляем статус задачи в БД перед отправкой вебхука
            await update_task_in_db(webhook_db_updates)

            # Попытки отправки вебхука
            webhook_sent_successfully = False
            for attempt in range(config.WEBHOOK_MAX_RETRIES + 1):
                task_logger.info(f"Sending final webhook for task {task_id} (attempt {attempt + 1}/{config.WEBHOOK_MAX_RETRIES + 1})")
                try:
                    async with httpx.AsyncClient(timeout=config.WEBHOOK_SEND_TIMEOUT, headers={"User-Agent": config.DEFAULT_USER_AGENT}) as client:
                        response = await client.post(str(webhook_url_to_send), json=payload_to_send)
                        response.raise_for_status() # Ошибка для 4xx/5xx
                        task_logger.info(f"Final webhook for task {task_id} sent successfully to {webhook_url_to_send}. Status: {response.status_code}")
                        webhook_sent_successfully = True
                        await update_task_in_db({
                            "webhook_status": "sent",
                            "webhook_last_attempt_at": get_utc_now().isoformat(),
                            "webhook_error": None
                        })
                        break # Успех, выходим из цикла ретраев
                except httpx.HTTPStatusError as e_wh_status:
                    wh_err_msg = f"Webhook HTTPStatusError: {e_wh_status.response.status_code} - {e_wh_status.response.text[:100]}"
                    task_logger.error(f"Final webhook for task {task_id} (attempt {attempt+1}) to {webhook_url_to_send} failed: {wh_err_msg}")
                    await update_task_in_db({"webhook_error": wh_err_msg, "webhook_last_attempt_at": get_utc_now().isoformat()})
                except httpx.RequestError as e_wh_req:
                    wh_err_msg = f"Webhook RequestError: {str(e_wh_req)}"
                    task_logger.error(f"Final webhook for task {task_id} (attempt {attempt+1}) to {webhook_url_to_send} failed: {wh_err_msg}")
                    await update_task_in_db({"webhook_error": wh_err_msg, "webhook_last_attempt_at": get_utc_now().isoformat()})
                except Exception as e_wh_generic:
                    wh_err_msg = f"Webhook Generic Error: {str(e_wh_generic)}"
                    task_logger.error(f"Final webhook for task {task_id} (attempt {attempt+1}) to {webhook_url_to_send} failed: {wh_err_msg}", exc_info=True)
                    await update_task_in_db({"webhook_error": wh_err_msg, "webhook_last_attempt_at": get_utc_now().isoformat()})

                if attempt < config.WEBHOOK_MAX_RETRIES:
                    delay = config.WEBHOOK_RETRY_DELAYS_SECONDS[min(attempt, len(config.WEBHOOK_RETRY_DELAYS_SECONDS) - 1)]
                    task_logger.info(f"Will retry sending webhook in {delay} seconds...")
                    await asyncio.sleep(delay)
            
            if not webhook_sent_successfully:
                task_logger.error(f"Failed to send final webhook for task {task_id} after all retries.")
                await update_task_in_db({"webhook_status": "failed_after_retries"})
        else: # No webhook_url provided
            task_logger.info(f"No webhook_url for task {task_id}. Marking as completed/failed without sending webhook.")
            if error_message_for_webhook:
                final_task_status_in_db = f"failed_no_webhook_{current_task_stage_error_short[:20].replace(' ', '_')}"
                webhook_db_updates.update({
                    "status": final_task_status_in_db,
                    "error_details": error_message_for_webhook,
                    "error_type": current_task_stage_error_short,
                    "completed_at": get_utc_now().isoformat(),
                    "webhook_status": "not_configured"
                })
            else:
                final_task_status_in_db = "completed_no_webhook"
                webhook_db_updates.update({
                    "status": final_task_status_in_db,
                    "completed_at": get_utc_now().isoformat(),
                    "webhook_status": "not_configured"
                })
            await update_task_in_db(webhook_db_updates)
            task_logger.info(f"Task {task_id} final status (no webhook): {final_task_status_in_db}")

        app_state["active_async_tasks"].discard(task_id) # Убираем из активных после завершения
        task_logger.info(f"Task {task_id} processing fully finalized.")


async def load_and_resume_webhook_tasks():
    resume_logger = get_logger("task_resumer")
    resume_logger.info("Loading webhook tasks database and checking for tasks to resume...")
    
    # Загружаем базу данных задач в кэш app_state
    app_state["webhook_tasks_db"] = await load_json_data(config.WEBHOOK_DB_FILE, webhook_db_lock_fastapi, default_factory=dict)
    
    if not app_state["webhook_tasks_db"]:
        resume_logger.info("Webhook tasks database is empty or not found. No tasks to resume.")
        return

    tasks_to_resume_count = 0
    resumed_task_identifiers = set() # Для отслеживания (client_request_id, original_url) уже возобновленных

    # Статусы, при которых задача считается "застрявшей" и требует возобновления
    stuck_statuses = [
        "pending_link_retrieval",
        "processing_started", # Если приложение упало в самом начале
        "processing_link_retrieval",
        "processing_link_retrieval_active",
        "links_retrieved_pending_s3_upload",
        "processing_s3_upload"
    ]
    # Также возобновляем задачи, ожидающие клиента
    waiting_for_client_prefix = "waiting_for_client_attempt_"

    tasks_items = list(app_state["webhook_tasks_db"].items()) # Копируем для безопасной итерации

    for task_id, task_info_dict in tasks_items:
        try:
            # Преобразуем словарь в Pydantic модель для удобства и валидации
            task_info = models.WebhookTask(**task_info_dict)
        except ValidationError as e:
            resume_logger.error(f"Invalid task data in DB for task_id {task_id}: {e}. Skipping resume.")
            continue

        current_status = task_info.status
        should_resume = False
        client_acquire_attempt_from_status = 1 # По умолчанию

        if current_status in stuck_statuses:
            should_resume = True
        elif current_status.startswith(waiting_for_client_prefix):
            try:
                client_acquire_attempt_from_status = int(current_status.split("_")[-1])
                should_resume = True
            except ValueError:
                resume_logger.error(f"Could not parse client acquire attempt from status '{current_status}' for task {task_id}. Skipping resume.")
                continue
        
        if should_resume:
            # Проверка на дубликаты возобновления (по client_request_id и original_url)
            client_req_id = task_info.metadata.get("client_request_id") if task_info.metadata else None
            task_identifier = (client_req_id, str(task_info.original_url))

            if client_req_id and task_identifier in resumed_task_identifiers:
                resume_logger.warning(f"Task {task_id} (URL: {task_info.original_url}, ClientReqID: {client_req_id}) is a duplicate for resumption. Marking as skipped_duplicate_on_restart.")
                async with webhook_db_lock_fastapi:
                    task_info.status = "skipped_duplicate_on_restart"
                    task_info.status_updated_at = get_utc_now()
                    app_state["webhook_tasks_db"][task_id] = task_info.model_dump(exclude_none=True)
                    # Сохранение будет выполнено позже одним батчем или при следующем изменении
                continue
            
            if client_req_id:
                resumed_task_identifiers.add(task_identifier)

            resume_logger.info(f"Resuming task {task_id} with status '{current_status}'. Original URL: {task_info.original_url}. Client acquire attempt: {client_acquire_attempt_from_status}")
            
            # Создаем фоновую задачу для возобновления
            # Используем asyncio.create_task, так как мы в async context
            asyncio.create_task(process_link_download_upload_task(
                original_url=task_info.original_url,
                task_id=task_id, # Используем существующий task_id
                webhook_url_to_send=task_info.webhook_url,
                client_metadata=task_info.metadata,
                _client_acquire_attempt=client_acquire_attempt_from_status
            ))
            tasks_to_resume_count += 1

    if tasks_to_resume_count > 0:
        resume_logger.info(f"Scheduled {tasks_to_resume_count} tasks for resumption.")
        # Сохраняем изменения в БД (например, статусы skipped_duplicate_on_restart)
        async with webhook_db_lock_fastapi:
            await save_json_data(config.WEBHOOK_DB_FILE, app_state["webhook_tasks_db"], webhook_db_lock_fastapi)
    else:
        resume_logger.info("No tasks found requiring resumption.")


# --- FastAPI Endpoints ---

@app.post("/files/v2/process-link",
            response_model=models.TaskAcceptedResponse,
            status_code=202, # Accepted
            summary="Asynchronously process a URL via Telegram and upload to S3 with webhook notifications.",
            dependencies=[Depends(verify_api_key)])
async def process_link_asynchronous(
    request_data: models.ProcessLinkWebhookRequest,
    background_tasks: BackgroundTasks,
    request: Request # Для получения request_id из заголовка, если есть
):
    """
    Accepts a URL to process. The service will:
    1. Interact with a Telegram bot to get file links.
    2. Download the files.
    3. Upload them to S3-compatible storage.
    4. Send notifications to the provided `webhook_url` about progress and completion.

    This endpoint returns immediately with a `task_id`.
    """
    # Используем client_request_id из метаданных для идемпотентности, если он предоставлен
    client_request_id = request_data.metadata.get("client_request_id") if request_data.metadata else None
    req_id_for_logging = client_request_id or get_request_id(request) # Используем client_request_id для логов, если есть
    endpoint_logger = get_logger("api.process_link_v2", req_id_for_logging)
    endpoint_logger.info(f"Received request: URL={request_data.url}, Webhook={request_data.webhook_url}, Meta={request_data.metadata}")

    # --- Idempotency Check ---
    if client_request_id:
        endpoint_logger.info(f"Performing idempotency check for client_request_id: {client_request_id}")
        async with webhook_db_lock_fastapi: # Блокировка на время чтения и возможной записи
            # Проверяем кэш app_state["webhook_tasks_db"]
            for task_id_db, task_data_dict in app_state["webhook_tasks_db"].items():
                try:
                    task_data_model = models.WebhookTask(**task_data_dict)
                except ValidationError:
                    continue # Пропускаем невалидные записи

                if task_data_model.metadata and task_data_model.metadata.get("client_request_id") == client_request_id and \
                   str(task_data_model.original_url) == str(request_data.url): # Доп. проверка по URL
                    
                    endpoint_logger.info(f"Found existing task {task_id_db} with client_request_id {client_request_id} and same URL. Status: {task_data_model.status}")
                    
                    if task_data_model.status == "completed" and task_data_model.webhook_status in ["sent", "not_configured"]:
                        # Задача успешно завершена, возвращаем ее результат
                        # Формируем ответ, похожий на успешный вебхук
                        response_data = models.WebhookSuccessPayload(
                            task_id=task_id_db,
                            original_url=task_data_model.original_url,
                            metadata=task_data_model.metadata,
                            processed_by_phone_number=task_data_model.processed_by_phone_number,
                            main_file_url=construct_s3_public_url(task_data_model.s3_main_file_key) if task_data_model.s3_main_file_key else str(task_data_model.main_download_url_from_bot),
                            main_file_original_name=task_data_model.s3_main_file_original_name,
                            main_file_s3_key=task_data_model.s3_main_file_key,
                            main_file_size_bytes=task_data_model.s3_main_file_size_bytes,
                            license_file_url=construct_s3_public_url(task_data_model.s3_license_file_key) if task_data_model.s3_license_file_key else (str(task_data_model.license_download_url_from_bot) if task_data_model.license_download_url_from_bot else None),
                            license_file_original_name=task_data_model.s3_license_file_original_name,
                            license_file_s3_key=task_data_model.s3_license_file_key,
                            license_file_size_bytes=task_data_model.s3_license_file_size_bytes
                        ).model_dump(exclude_none=True)
                        endpoint_logger.info(f"Returning 200 OK with existing completed task {task_id_db} details.")
                        return JSONResponse(status_code=200, content={"message": "Request previously completed.", "task_id": task_id_db, "data": response_data})

                    elif task_data_model.status.startswith("failed_"):
                         # Если предыдущая задача с таким ID провалилась, позволяем создать новую
                         endpoint_logger.info(f"Previous task {task_id_db} failed. Proceeding to create a new task.")
                         break # Выходим из цикла поиска, чтобы создать новую задачу

                    else: # Задача в процессе или ожидает
                        endpoint_logger.info(f"Returning 202 Accepted for existing processing task {task_id_db}.")
                        return models.TaskAcceptedResponse(request_id=req_id_for_logging, task_id=task_id_db, message="Request is already being processed or pending.")

    # --- Создание новой задачи ---
    internal_task_id = uuid.uuid4().hex[:12] # Генерируем новый ID для нашей системы
    endpoint_logger.info(f"Generated internal_task_id: {internal_task_id}")

    new_task_entry = models.WebhookTask(
        task_id=internal_task_id,
        original_url=request_data.url,
        webhook_url=request_data.webhook_url,
        metadata=request_data.metadata.copy() if request_data.metadata else {}, # Копируем метаданные
        status="pending_link_retrieval", # Начальный статус
        added_at=get_utc_now(),
        status_updated_at=get_utc_now()
    )
    if client_request_id: # Сохраняем client_request_id в метаданных задачи
        new_task_entry.metadata["client_request_id"] = client_request_id

    async with webhook_db_lock_fastapi:
        app_state["webhook_tasks_db"][internal_task_id] = new_task_entry.model_dump(exclude_none=True)
        await save_json_data(config.WEBHOOK_DB_FILE, app_state["webhook_tasks_db"], webhook_db_lock_fastapi)
    
    endpoint_logger.info(f"New task {internal_task_id} created and saved to DB.")

    background_tasks.add_task(
        process_link_download_upload_task,
        original_url=request_data.url,
        task_id=internal_task_id,
        webhook_url_to_send=request_data.webhook_url,
        client_metadata=request_data.metadata
    )
    endpoint_logger.info(f"Task {internal_task_id} added to background processing.")

    return models.TaskAcceptedResponse(request_id=req_id_for_logging, task_id=internal_task_id)


@app.get("/files/get-link",
           response_model=models.SynchronousLinkProcessResponse,
           summary="Synchronously process a URL (DEPRECATED).",
           dependencies=[Depends(verify_api_key)])
async def get_link_synchronous(
    url: HttpUrl, # FastAPI автоматически валидирует query-параметр
    redirect: bool = False,
    request: Request = None # Для request_id
):
    """
    Processes a URL synchronously: gets links from Telegram, downloads, uploads to S3, and returns results.
    **DEPRECATED**: Prefer the asynchronous `/files/v2/process-link` endpoint.
    """
    req_id = get_request_id(request)
    sync_logger = get_logger("api.get_link_sync", req_id)
    sync_logger.warning(f"Synchronous endpoint /files/get-link called for URL: {url}. This endpoint is DEPRECATED.")

    if not app_state.get("clients_initialized"):
        sync_logger.error("Clients are not initialized yet.")
        raise HTTPException(status_code=503, detail="Service Unavailable: Telegram clients not ready.")

    selected_client_info = await select_client_with_lock(
        select_client_lock=select_client_lock,
        clients=clients,
        client_status=client_status,
        client_locks=client_locks,
        client_details=client_details,
        client_cooldown_end_times=client_cooldown_end_times,
        app_state=app_state,
        stats_file_path=str(config.STATS_FILE_PATH),
        stats_file_lock=stats_file_lock_fastapi
    )

    if not selected_client_info:
        sync_logger.error("No Telegram client available for synchronous processing.")
        raise HTTPException(status_code=503, detail="Service Unavailable: No Telegram client available at the moment.")

    session_str, tg_client, client_lock, acc_name, phone_num = selected_client_info
    sync_logger.info(f"Using client: {acc_name} ({phone_num}) for synchronous request {req_id}")

    temp_files_to_clean: List[str] = []
    response_data = models.SynchronousLinkProcessResponse(
        request_id=req_id,
        original_url=url,
        processed_by_account=acc_name,
        processed_by_phone_number=phone_num
    )

    try:
        async with client_lock: # Блокируем клиента на время использования
            sync_logger.debug(f"Acquired lock for client {acc_name} for sync task.")
            if client_status.get(session_str) != "ok": # Повторная проверка
                sync_logger.error(f"Client {acc_name} status changed to '{client_status.get(session_str)}' before sync use.")
                raise HTTPException(status_code=503, detail="Service Unavailable: Selected client became unavailable.")

            tg_result = await process_link_with_telegram(
                client=tg_client, url_to_process=str(url), account_name=acc_name, phone_number=phone_num,
                request_id=req_id, client_status_dict=client_status,
                stats_file_path_str=str(config.STATS_FILE_PATH), stats_file_lock=stats_file_lock_fastapi, app_state=app_state
            )
            
            cooldown_duration = random.uniform(config.SESSION_REQUEST_DELAY_MIN, config.SESSION_REQUEST_DELAY_MAX)
            client_cooldown_end_times[session_str] = time.time() + cooldown_duration
            sync_logger.info(f"Applied cooldown of {cooldown_duration:.2f}s to session {phone_num} after sync use.")

        sync_logger.debug(f"Released lock for client {acc_name} for sync task.")

        if tg_result.get("error"):
            sync_logger.error(f"Telegram processing failed: {tg_result.get('telegram_error_type')} - {tg_result.get('error')}")
            response_data.error = tg_result.get("error")
            response_data.telegram_error_type = tg_result.get("telegram_error_type")
            # Определяем HTTP статус код на основе ошибки Telegram
            # Например, 404 если кнопка не найдена, 502 если бот вернул ошибку
            status_code = 502 # Bad Gateway (общая ошибка при взаимодействии с вышестоящим сервисом)
            if tg_result.get('telegram_error_type') == "ButtonNotFoundError": status_code = 404 # Not Found
            raise HTTPException(status_code=status_code, detail=response_data.model_dump_json(exclude_none=True))

        main_bot_url = tg_result.get("main_url")
        license_bot_url = tg_result.get("license_url")

        if not main_bot_url:
            sync_logger.error("Main URL not found in Telegram response.")
            response_data.error = "Main URL not received from Telegram bot."
            response_data.telegram_error_type = "MainUrlMissingAfterTelegram"
            raise HTTPException(status_code=502, detail=response_data.model_dump_json(exclude_none=True))

        # Обновление статистики использования
        await update_stats_on_request(phone_num, acc_name, req_id, str(config.STATS_FILE_PATH), stats_file_lock_fastapi, app_state)
        # Проверка дневного лимита после инкремента
        today_utc_str_stats_sync = get_utc_today_str()
        current_daily_uses_sync = app_state.get("current_stats", {}).get(phone_num, {}).get("daily_usage", {}).get(today_utc_str_stats_sync, 0)
        if current_daily_uses_sync >= config.DAILY_REQUEST_LIMIT_PER_SESSION:
            limit_status_str_sync = f"daily_limit_reached_{today_utc_str_stats_sync}"
            sync_logger.warning(f"Session {phone_num} reached daily limit ({current_daily_uses_sync}/{config.DAILY_REQUEST_LIMIT_PER_SESSION}) after this sync request.")
            client_status[session_str] = limit_status_str_sync
            await update_session_worker_status_in_stats(
                phone_num, session_str, limit_status_str_sync, acc_name, req_id,
                str(config.STATS_FILE_PATH), stats_file_lock_fastapi, app_state
            )


        # Скачивание основного файла
        sync_logger.info(f"Downloading main file: {main_bot_url}")
        dl_main_res = await download_file(main_bot_url, req_id, "main")
        if not dl_main_res:
            sync_logger.error("Main file download failed.")
            response_data.error = "Failed to download the main file."
            raise HTTPException(status_code=504, detail=response_data.model_dump_json(exclude_none=True)) # Gateway Timeout
        
        temp_main_path, main_s3_part, main_orig_name, main_size = dl_main_res
        temp_files_to_clean.append(temp_main_path)
        response_data.main_file_original_name = main_orig_name
        response_data.main_file_size_bytes = main_size

        # Загрузка основного файла в S3
        if config.S3_CONFIGURED and s3_client:
            sync_logger.info(f"Uploading main file to S3: {main_orig_name}")
            s3_main_key = await upload_file_to_s3(temp_main_path, req_id, main_s3_part, main_orig_name)
            if not s3_main_key:
                sync_logger.error("Main file S3 upload failed.")
                response_data.error = "Failed to upload the main file to S3."
                raise HTTPException(status_code=502, detail=response_data.model_dump_json(exclude_none=True)) # Bad Gateway
            response_data.main_file_s3_key = s3_main_key
            response_data.main_file_url = construct_s3_public_url(s3_main_key)
        else: # S3 не настроен, возвращаем URL от бота
            response_data.main_file_url = main_bot_url
            sync_logger.warning("S3 not configured. Returning direct download URL from bot for main file.")


        # Скачивание и загрузка файла лицензии
        if license_bot_url:
            sync_logger.info(f"Downloading license file: {license_bot_url}")
            dl_lic_res = await download_file(license_bot_url, req_id, "license")
            if dl_lic_res:
                temp_lic_path, lic_s3_part, lic_orig_name, lic_size = dl_lic_res
                temp_files_to_clean.append(temp_lic_path)
                response_data.license_file_original_name = lic_orig_name
                response_data.license_file_size_bytes = lic_size

                if config.S3_CONFIGURED and s3_client:
                    sync_logger.info(f"Uploading license file to S3: {lic_orig_name}")
                    s3_lic_key = await upload_file_to_s3(temp_lic_path, req_id, lic_s3_part, lic_orig_name)
                    if s3_lic_key:
                        response_data.license_file_s3_key = s3_lic_key
                        response_data.license_file_url = construct_s3_public_url(s3_lic_key)
                    else:
                        sync_logger.warning("License file S3 upload failed. Proceeding without S3 URL for license.")
                        response_data.license_file_url = license_bot_url # Возвращаем URL от бота если S3 не удалось
                else: # S3 не настроен для лицензии
                    response_data.license_file_url = license_bot_url
                    sync_logger.warning("S3 not configured. Returning direct download URL from bot for license file.")
            else:
                sync_logger.warning("License file download failed.")
        
        sync_logger.info(f"Synchronous processing for {req_id} completed.")
        if redirect and response_data.main_file_url:
            sync_logger.info(f"Redirecting to main file URL: {response_data.main_file_url}")
            return RedirectResponse(url=response_data.main_file_url)
        
        return response_data

    except HTTPException as e: # Перехватываем HTTPException, чтобы не попасть в общий Exception handler ниже
        sync_logger.error(f"HTTPException in sync task {req_id}: {e.status_code} - {e.detail}")
        raise e # Пробрасываем дальше
    except Exception as e:
        sync_logger.error(f"Unexpected error in synchronous task {req_id}: {e}", exc_info=True)
        # Убедимся, что error и telegram_error_type установлены, если это ошибка из TG
        if not response_data.error: response_data.error = f"Unexpected server error: {str(e)}"
        if not response_data.telegram_error_type and "Telegram" in str(type(e)): # Грубая проверка
             response_data.telegram_error_type = type(e).__name__
        raise HTTPException(status_code=500, detail=response_data.model_dump_json(exclude_none=True) if response_data.error else "Internal Server Error")
    finally:
        sync_logger.debug(f"Cleaning up temporary files for sync task {req_id}: {temp_files_to_clean}")
        for f_path in temp_files_to_clean:
            await cleanup_temp_file(f_path, req_id)


@app.get("/health",
           response_model=models.HealthResponse,
           summary="Get the health status of the service.",
           dependencies=[Depends(verify_api_key)])
async def health_check():
    health_logger = get_logger("api.health")
    health_logger.info("Health check requested.")

    if not app_state.get("clients_initialized"):
        health_logger.warning("Health check: Clients not fully initialized yet.")
        # Можно вернуть 503, если это критично, или информацию о состоянии инициализации
        # return JSONResponse(status_code=503, content={"detail": "Service initializing, clients not ready."})

    # Подсчет клиентов по статусам
    active_clients_count = 0
    cooldown_clients_count = 0
    flood_wait_clients_count = 0
    error_clients_count = 0 # Общий счетчик ошибок
    auth_error_clients_count = 0
    deactivated_clients_count = 0
    expired_clients_count = 0
    timeout_clients_count = 0 # Только timeout_connect
    other_status_clients_count = 0 # Для неклассифицированных статусов
    clients_at_daily_limit_today = 0
    
    detailed_statuses: Dict[str, str] = {}
    now_ts = time.time()
    today_utc_str_health = get_utc_today_str()

    # Используем копию client_status.items() для безопасной итерации
    # Блокировка select_client_lock здесь не нужна, т.к. мы только читаем статусы.
    # Но если select_client_with_lock может изменять статусы, нужна осторожность.
    # Для большей безопасности можно обернуть в select_client_lock, но это может замедлить /health
    # Решение: select_client_with_lock должен быть единственным местом изменения статусов из flood/limit в ok.
    # Здесь мы просто читаем текущее состояние.
    
    # Получаем актуальную статистику (может быть изменена другими задачами)
    async with stats_file_lock_fastapi: # Блокировка для чтения актуальных данных из app_state["current_stats"]
        current_stats_snapshot = app_state.get("current_stats", {}).copy()

    for s_str, status_val in list(client_status.items()): # list() для копии
        phone_hint = app_state.get("session_to_phone_map", {}).get(s_str, "UnknownPhone")
        client_name = client_details.get(s_str, models.TelegramClientDetails(phone=phone_hint, name="N/A", original_phone_hint=phone_hint)).name
        
        display_key = f"{client_name} ({phone_hint}, ...{s_str[-6:]})"
        
        daily_uses = 0
        if phone_hint in current_stats_snapshot:
            daily_uses = current_stats_snapshot[phone_hint].get("daily_usage", {}).get(today_utc_str_health, 0)

        status_display = status_val
        if status_val == "ok":
            if s_str in clients and clients[s_str].is_connected(): # Дополнительная проверка
                active_clients_count += 1
                status_display = f"ok (today: {daily_uses}/{config.DAILY_REQUEST_LIMIT_PER_SESSION})"
                if daily_uses >= config.DAILY_REQUEST_LIMIT_PER_SESSION:
                    clients_at_daily_limit_today +=1
                    status_display = f"daily_limit_reached_effective (today: {daily_uses}/{config.DAILY_REQUEST_LIMIT_PER_SESSION})" # Фактически лимит
            else: # Статус 'ok', но клиент не подключен - это ошибка
                status_display = f"error_disconnected_inconsistent (today: {daily_uses}/{config.DAILY_REQUEST_LIMIT_PER_SESSION})"
                error_clients_count += 1


        elif status_val.startswith("flood_wait_"):
            flood_wait_clients_count += 1
            try:
                end_time = int(status_val.split("_")[-1])
                remaining_flood = max(0, end_time - now_ts)
                status_display = f"{status_val} (~{remaining_flood:.0f}s left)"
            except: status_display = status_val # fallback
        elif status_val.startswith("daily_limit_reached_"):
            clients_at_daily_limit_today += 1
            status_display = f"{status_val} (today: {daily_uses}/{config.DAILY_REQUEST_LIMIT_PER_SESSION})"
        elif "error" in status_val: error_clients_count +=1
        elif status_val == "auth_key_error": auth_error_clients_count +=1 # auth_error - это общий, auth_key_error - конкретный
        elif status_val == "deactivated": deactivated_clients_count +=1
        elif status_val == "expired": expired_clients_count +=1
        elif status_val == "timeout_connect": timeout_clients_count +=1
        else: other_status_clients_count +=1 # Для статусов типа "blocked_by_bot", "chat_write_forbidden" etc.

        if client_cooldown_end_times.get(s_str, 0) > now_ts:
            cooldown_clients_count += 1
            remaining_cd = client_cooldown_end_times[s_str] - now_ts
            status_display += f" | cooldown (~{remaining_cd:.0f}s left)"
            
        detailed_statuses[display_key] = status_display

    # Количество задач, ожидающих клиента (по статусу в БД задач)
    tasks_waiting_for_client_count = 0
    async with webhook_db_lock_fastapi: # Только чтение из кэша
        for task_info_db in app_state.get("webhook_tasks_db", {}).values():
            if isinstance(task_info_db, dict) and task_info_db.get("status", "").startswith("waiting_for_client_attempt_"):
                tasks_waiting_for_client_count += 1
    
    service_status = "ok"
    status_message = "Service is operating normally."
    if error_clients_count > 0 or auth_error_clients_count > 0 or (active_clients_count == 0 and len(client_status) > 0) :
        service_status = "warning"
        status_message = "Service has some clients in error state or no active clients."
    if not app_state.get("clients_initialized") or (len(client_status) > 0 and active_clients_count == 0 and tasks_waiting_for_client_count > 0):
        service_status = "error"
        status_message = "Service has critical issues: no active clients and tasks are waiting, or not initialized."


    return models.HealthResponse(
        app_version=config.APP_VERSION,
        service_status=service_status,
        message=status_message,
        active_clients=active_clients_count,
        cooldown_clients_count=cooldown_clients_count,
        flood_wait_clients_count=flood_wait_clients_count,
        error_clients_count=error_clients_count,
        auth_error_clients_count=auth_error_clients_count, # Включает auth_key_error и другие auth_...
        deactivated_clients_count=deactivated_clients_count,
        expired_clients_count=expired_clients_count,
        timeout_clients_count=timeout_clients_count,
        other_status_clients_count=other_status_clients_count,
        tasks_waiting_for_client=tasks_waiting_for_client_count,
        total_configured_clients=len(app_state.get("session_to_phone_map", {})),
        s3_configured=config.S3_CONFIGURED,
        s3_public_base_url_configured=bool(config.S3_PUBLIC_BASE_URL),
        daily_request_limit_per_session=config.DAILY_REQUEST_LIMIT_PER_SESSION,
        clients_at_daily_limit_today=clients_at_daily_limit_today,
        clients_statuses_detailed=detailed_statuses
    )

@app.get("/stats/accounts",
           response_model=models.StatsFileContent,
           summary="Get all account usage statistics.",
           dependencies=[Depends(verify_api_key)])
async def get_account_stats():
    stats_logger = get_logger("api.stats")
    stats_logger.info("Account statistics requested.")
    
    async with stats_file_lock_fastapi: # Блокировка на время чтения из кэша
        # Возвращаем данные из кэша app_state["current_stats"]
        # Преобразуем в модели Pydantic для валидации и корректного ответа
        data_to_return: Dict[str, models.StatsAccountDetail] = {}
        for phone, stats_dict in app_state.get("current_stats", {}).items():
            try:
                data_to_return[phone] = models.StatsAccountDetail(**stats_dict)
            except ValidationError as e:
                stats_logger.error(f"Invalid stats data for phone {phone} in cache: {e}. Skipping.")
                # Можно добавить "сырые" данные или специальный маркер ошибки
                # data_to_return[phone] = {"error": "Invalid data structure", "raw": stats_dict}

    return models.StatsFileContent(
        retrieved_at_utc=get_utc_now(),
        data_source_file=str(config.STATS_FILE_PATH.name), # Только имя файла
        data=data_to_return
    )

@app.get("/logs/download",
           summary="Download the application log file.",
           dependencies=[Depends(verify_api_key)])
async def download_logs():
    logs_logger = get_logger("api.logs")
    log_file_path = config.LOG_FILE_PATH_FOR_DOWNLOAD
    if not log_file_path.exists():
        logs_logger.error(f"Log file not found: {log_file_path}")
        raise HTTPException(status_code=404, detail="Log file not found.")
    
    logs_logger.info(f"Log file download requested: {log_file_path.name}")
    return FileResponse(
        path=log_file_path,
        filename=log_file_path.name,
        media_type='text/plain'
    )

# --- Generic Exception Handler ---
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # Получаем или генерируем request_id
    req_id = get_request_id(request)
    err_logger = get_logger("exception_handler", req_id)
    
    err_logger.error(f"Unhandled exception caught: {type(exc).__name__} - {str(exc)} for request {request.method} {request.url.path}", exc_info=True)
    
    # Если это HTTPException, то у него уже есть status_code и detail
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"request_id": req_id, "detail": exc.detail}
        )
        
    # Для всех остальных непредвиденных ошибок
    return JSONResponse(
        status_code=500,
        content={"request_id": req_id, "detail": f"Internal Server Error: {type(exc).__name__}. Please check server logs for request ID {req_id}."}
    )

# --- Main execution (for uvicorn) ---
# if __name__ == "__main__":
#     import uvicorn
#     # Uvicorn будет запускать приложение через имя файла и объекта app, например:
#     # uvicorn main_fastapi_app:app --reload --port 8000
#     # Этот блок if __name__ == "__main__": здесь больше для примера,
#     # обычно запуск uvicorn происходит из командной строки.
#     uvicorn.run(app, host="0.0.0.0", port=8000, log_level=config.LOG_LEVEL.lower())
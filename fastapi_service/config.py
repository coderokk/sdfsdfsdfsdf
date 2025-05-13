import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional, List, Union

# Определяем базовую директорию проекта FastAPI
# Это предполагает, что config.py находится в корне fastapi_service
BASE_DIR = Path(__file__).resolve().parent

# Загружаем переменные окружения из .env файла, который должен быть в BASE_DIR
dotenv_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=dotenv_path)

logger = logging.getLogger(__name__)

def get_env_var(var_name: str, default: Optional[Union[str, int, bool]] = None, required: bool = False, var_type: type = str) -> Optional[Union[str, int, bool, List[str]]]:
    """
    Получает переменную окружения, приводит к нужному типу и проверяет на обязательность.
    """
    value = os.getenv(var_name)
    if value is None:
        if required:
            logger.error(f"Missing required environment variable: {var_name}")
            raise ValueError(f"Missing required environment variable: {var_name}")
        return default

    if var_type == bool:
        return value.lower() in ('true', '1', 't', 'yes', 'y')
    if var_type == int:
        try:
            return int(value)
        except ValueError:
            logger.error(f"Invalid integer value for {var_name}: {value}")
            if required:
                raise
            return default
    if var_type == list_str: # Специальный тип для списков строк через запятую
        return [item.strip() for item in value.split(',') if item.strip()]
    try:
        return var_type(value)
    except ValueError:
        logger.error(f"Invalid value type for {var_name}: {value}. Expected {var_type}")
        if required:
            raise
        return default

class list_str: # Фиктивный класс для type hinting
    pass

# --- Telegram Core API ---
API_ID: int = get_env_var("API_ID", required=True, var_type=int)
API_HASH: str = get_env_var("API_HASH", required=True)

# --- File Paths ---
# Пути теперь будут абсолютными, чтобы избежать проблем с относительными путями
SESSIONS_FILE_PATH: Path = BASE_DIR / get_env_var("SESSIONS_FILE_PATH", "data/sessions.json")
STATS_FILE_PATH: Path = BASE_DIR / get_env_var("STATS_FILE_PATH", "data/usage_stats.json")
WEBHOOK_DB_FILE: Path = BASE_DIR / get_env_var("WEBHOOK_DB_FILE", "data/webhook_tasks.json")
LOG_FILE_PATH_FOR_DOWNLOAD: Path = BASE_DIR / get_env_var("LOG_FILE_PATH_FOR_DOWNLOAD", "logs/fastapi_app.log")
TEMP_DOWNLOAD_DIR: Path = BASE_DIR / get_env_var("TEMP_DOWNLOAD_DIR", "temp_downloads")

# --- S3 Configuration ---
S3_ENDPOINT_URL: Optional[str] = get_env_var("S3_ENDPOINT_URL")
S3_ACCESS_KEY_ID: Optional[str] = get_env_var("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY: Optional[str] = get_env_var("S3_SECRET_ACCESS_KEY")
S3_BUCKET_NAME: Optional[str] = get_env_var("S3_BUCKET_NAME")
S3_REGION_NAME: Optional[str] = get_env_var("S3_REGION_NAME")
S3_PUBLIC_BASE_URL: Optional[str] = get_env_var("S3_PUBLIC_BASE_URL")
_s3_envato_folder_path_raw: Optional[str] = get_env_var("S3_ENVATO_FOLDER_PATH")
S3_ENVATO_FOLDER_PATH: str = _s3_envato_folder_path_raw.strip('/') if _s3_envato_folder_path_raw else ""

S3_CONFIGURED: bool = all([S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME])

# --- Telegram Bot Interaction ---
TARGET_BOT_USERNAME: str = get_env_var("TARGET_BOT_USERNAME", required=True)
TARGET_BUTTON_TEXT: str = get_env_var("TARGET_BUTTON_TEXT", required=True)
MAIN_FILE_KEYWORD: str = get_env_var("MAIN_FILE_KEYWORD", "получены").lower()
LICENSE_KEYWORD: str = get_env_var("LICENSE_KEYWORD", "скачана").lower()
LINK_KEYWORD: str = get_env_var("LINK_KEYWORD", "ссылка").lower()
OOPS_BOT_ERROR_KEYWORD: str = get_env_var("OOPS_BOT_ERROR_KEYWORD", "oops").lower()

# --- Timeouts ---
TELEGRAM_RESPONSE_TIMEOUT: int = get_env_var("TELEGRAM_RESPONSE_TIMEOUT", 1800, var_type=int)
TELEGRAM_BUTTON_RESPONSE_TIMEOUT: int = get_env_var("TELEGRAM_BUTTON_RESPONSE_TIMEOUT", 120, var_type=int)
TELEGRAM_INTERMEDIATE_MESSAGE_TIMEOUT: int = get_env_var("TELEGRAM_INTERMEDIATE_MESSAGE_TIMEOUT", 30, var_type=int)
DOWNLOAD_TIMEOUT: int = get_env_var("DOWNLOAD_TIMEOUT", 600, var_type=int)
UPLOAD_TIMEOUT: int = get_env_var("UPLOAD_TIMEOUT", 600, var_type=int)
TELEGRAM_CONNECT_TIMEOUT: int = get_env_var("TELEGRAM_CONNECT_TIMEOUT", 45, var_type=int)
WEBHOOK_SEND_TIMEOUT: int = get_env_var("WEBHOOK_SEND_TIMEOUT", 30, var_type=int)

# --- Retries & Delays ---
MAX_UPLOAD_RETRIES: int = get_env_var("MAX_UPLOAD_RETRIES", 1, var_type=int)
UPLOAD_RETRY_DELAY: int = get_env_var("UPLOAD_RETRY_DELAY", 5, var_type=int)
SESSION_REQUEST_DELAY_MIN: int = get_env_var("SESSION_REQUEST_DELAY_MIN", 10, var_type=int)
SESSION_REQUEST_DELAY_MAX: int = get_env_var("SESSION_REQUEST_DELAY_MAX", 20, var_type=int)
if SESSION_REQUEST_DELAY_MAX < SESSION_REQUEST_DELAY_MIN:
    logger.warning("SESSION_REQUEST_DELAY_MAX is less than SESSION_REQUEST_DELAY_MIN. Setting MAX to MIN.")
    SESSION_REQUEST_DELAY_MAX = SESSION_REQUEST_DELAY_MIN

MAX_CLIENT_ACQUIRE_RETRIES: int = get_env_var("MAX_CLIENT_ACQUIRE_RETRIES", 3, var_type=int)
CLIENT_ACQUIRE_RETRY_DELAY: int = get_env_var("CLIENT_ACQUIRE_RETRY_DELAY", 60, var_type=int)
WEBHOOK_MAX_RETRIES: int = get_env_var("WEBHOOK_MAX_RETRIES", 6, var_type=int)
_webhook_retry_delays_str: str = get_env_var("WEBHOOK_RETRY_DELAYS_SECONDS", "60,300,900,1800,3600,10800")
WEBHOOK_RETRY_DELAYS_SECONDS: List[int] = [int(d.strip()) for d in _webhook_retry_delays_str.split(',') if d.strip()]

# --- Limits & Misc ---
DAILY_REQUEST_LIMIT_PER_SESSION: int = get_env_var("DAILY_REQUEST_LIMIT_PER_SESSION", 100, var_type=int)
FASTAPI_CLIENT_API_KEY: Optional[str] = get_env_var("FASTAPI_CLIENT_API_KEY")
API_KEY_NAME_HEADER: str = get_env_var("API_KEY_NAME_HEADER", "X-API-Key")
LOG_LEVEL: str = get_env_var("LOG_LEVEL", "INFO").upper()
MOSCOW_TZ: str = get_env_var("MOSCOW_TZ", "Europe/Moscow")
APP_VERSION: str = get_env_var("APP_VERSION", "1.0.0")
DEFAULT_USER_AGENT: str = get_env_var("DEFAULT_USER_AGENT", f"TelegramS3Uploader/{APP_VERSION} (FastAPI)")


# Создаем директории, если они не существуют
def create_dirs():
    SESSIONS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEBHOOK_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE_PATH_FOR_DOWNLOAD.parent.mkdir(parents=True, exist_ok=True)
    TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Вызываем функцию создания директорий при импорте модуля
create_dirs()

# Проверка обязательных S3 переменных, если S3_ENDPOINT_URL указан
if S3_ENDPOINT_URL and not S3_CONFIGURED:
    missing_s3_vars = []
    if not S3_ACCESS_KEY_ID: missing_s3_vars.append("S3_ACCESS_KEY_ID")
    if not S3_SECRET_ACCESS_KEY: missing_s3_vars.append("S3_SECRET_ACCESS_KEY")
    if not S3_BUCKET_NAME: missing_s3_vars.append("S3_BUCKET_NAME")
    if missing_s3_vars:
        msg = f"S3_ENDPOINT_URL is set, but some S3 configuration variables are missing: {', '.join(missing_s3_vars)}"
        logger.error(msg)
        # Можно решить, стоит ли здесь вызывать исключение, или просто логировать
        # raise ValueError(msg)

logger.info("Configuration loaded successfully.")
if S3_CONFIGURED:
    logger.info(f"S3 Uploads ENABLED. Bucket: {S3_BUCKET_NAME}, Endpoint: {S3_ENDPOINT_URL}")
    if S3_PUBLIC_BASE_URL:
        logger.info(f"S3 Public Base URL configured: {S3_PUBLIC_BASE_URL}")
    else:
        logger.warning("S3_PUBLIC_BASE_URL is not set. Synchronous endpoint will return S3 keys instead of full URLs.")
else:
    logger.warning("S3 configuration is INCOMPLETE. S3 uploads will be DISABLED.")

if not FASTAPI_CLIENT_API_KEY:
    logger.warning("FASTAPI_CLIENT_API_KEY is not set. API endpoints will be PUBLIC.")
else:
    logger.info("FASTAPI_CLIENT_API_KEY is set. API endpoints will be protected.")
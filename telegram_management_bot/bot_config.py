# telegram_management_bot/bot_config.py
import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from typing import List, Optional

# Определяем базовую директорию проекта бота
# Это предполагает, что bot_config.py находится в корне telegram_management_bot
BOT_BASE_DIR = Path(__file__).resolve().parent

# Загружаем переменные окружения из .env_bot файла, который должен быть в BOT_BASE_DIR
dotenv_path = BOT_BASE_DIR / ".env_bot" # Используем .env_bot
load_dotenv(dotenv_path=dotenv_path)

logger = logging.getLogger(__name__)

def get_env_var_bot(var_name: str, default: Optional[str] = None, required: bool = False, var_type: type = str) -> Optional[any]:
    value = os.getenv(var_name)
    if value is None:
        if required:
            logger.error(f"Missing required environment variable in .env_bot: {var_name}")
            raise ValueError(f"Missing required environment variable in .env_bot: {var_name}")
        return default

    if var_type == bool:
        return value.lower() in ('true', '1', 't', 'yes', 'y')
    if var_type == int:
        try:
            return int(value)
        except ValueError:
            logger.error(f"Invalid integer value for {var_name} in .env_bot: {value}")
            if required: raise
            return default
    if var_type == list_int: # Для списка ID администраторов
        try:
            return [int(item.strip()) for item in value.split(',') if item.strip()]
        except ValueError:
            logger.error(f"Invalid list of integers for {var_name} in .env_bot: {value}")
            if required: raise
            return default
    try:
        return var_type(value)
    except ValueError:
        logger.error(f"Invalid value type for {var_name} in .env_bot: {value}. Expected {var_type}")
        if required: raise
        return default

class list_int: # Фиктивный класс для type hinting
    pass

# --- Telegram Bot (управляющий бот) ---
BOT_TOKEN: str = get_env_var_bot("BOT_TOKEN", required=True)
ADMIN_IDS: List[int] = get_env_var_bot("ADMIN_IDS", required=True, var_type=list_int)

# --- FastAPI Service Interaction ---
FASTAPI_URL: str = get_env_var_bot("FASTAPI_URL", required=True)
FASTAPI_API_KEY: str = get_env_var_bot("FASTAPI_API_KEY", required=True) # Должен совпадать с ключом в FastAPI

# --- Paths to FastAPI Service components (АБСОЛЮТНЫЕ ПУТИ) ---
# Важно: эти пути должны быть абсолютными и указывать на файлы FastAPI сервиса
FASTAPI_SCRIPT_PATH_STR: str = get_env_var_bot("FASTAPI_SCRIPT_PATH", required=True)
SESSIONS_JSON_PATH_STR: str = get_env_var_bot("SESSIONS_JSON_PATH", required=True)
STATS_JSON_PATH_STR: str = get_env_var_bot("STATS_JSON_PATH", required=True)
WEBHOOK_DB_JSON_PATH_STR: str = get_env_var_bot("WEBHOOK_DB_JSON_PATH", required=True)
FASTAPI_ENV_PATH_STR: str = get_env_var_bot("FASTAPI_ENV_PATH", required=True)
FASTAPI_LOG_PATH_STR: str = get_env_var_bot("FASTAPI_LOG_PATH", required=True)

# Преобразуем строковые пути в объекты Path для удобства
FASTAPI_SCRIPT_PATH: Path = Path(FASTAPI_SCRIPT_PATH_STR)
SESSIONS_JSON_PATH: Path = Path(SESSIONS_JSON_PATH_STR)
STATS_JSON_PATH: Path = Path(STATS_JSON_PATH_STR)
WEBHOOK_DB_JSON_PATH: Path = Path(WEBHOOK_DB_JSON_PATH_STR)
FASTAPI_ENV_PATH: Path = Path(FASTAPI_ENV_PATH_STR)
FASTAPI_LOG_PATH: Path = Path(FASTAPI_LOG_PATH_STR)

# --- Bot Self Management ---
BOT_SCRIPT_PATH_STR: str = get_env_var_bot("BOT_SCRIPT_PATH", required=True)
BOT_SCRIPT_PATH: Path = Path(BOT_SCRIPT_PATH_STR)

_bot_log_path_str: str = get_env_var_bot("BOT_LOG_PATH", "logs/telegram_bot.log")
# Если путь относительный, делаем его относительно BOT_BASE_DIR
BOT_LOG_PATH: Path
if not Path(_bot_log_path_str).is_absolute():
    BOT_LOG_PATH = BOT_BASE_DIR / _bot_log_path_str
else:
    BOT_LOG_PATH = Path(_bot_log_path_str)


# --- Telethon settings for session creation (can be overridden by user) ---
# Эти значения могут быть взяты из .env FastAPI сервиса, если бот имеет к нему доступ,
# или запрошены у пользователя. Для простоты, можно их здесь определить или оставить пустыми.
# Если они есть в .env_bot, можно их загрузить.
TELETHON_API_ID: Optional[int] = get_env_var_bot("TELETHON_API_ID", var_type=int) # Не обязательное поле в .env_bot
TELETHON_API_HASH: Optional[str] = get_env_var_bot("TELETHON_API_HASH") # Не обязательное поле в .env_bot

# --- Other Bot settings ---
LOG_LEVEL_BOT: str = get_env_var_bot("LOG_LEVEL_BOT", "INFO").upper()
DEFAULT_RESTART_COMMAND_FASTAPI: str = get_env_var_bot("RESTART_COMMAND_FASTAPI", "systemctl restart your_fastapi_service_name") # Пример
DEFAULT_RESTART_COMMAND_BOT: str = get_env_var_bot("RESTART_COMMAND_BOT", "systemctl restart your_bot_service_name") # Пример

# Создаем директории для логов бота, если они не существуют
def create_bot_dirs():
    BOT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

create_bot_dirs()

logger.info("Bot configuration loaded.")
if not ADMIN_IDS:
    logger.critical("ADMIN_IDS is not set in .env_bot! The bot will not be usable by anyone.")
else:
    logger.info(f"Admin IDs: {ADMIN_IDS}")

# Проверка существования путей к файлам FastAPI (опционально, но полезно)
critical_paths_fastapi = {
    "FastAPI Script": FASTAPI_SCRIPT_PATH,
    "Sessions JSON": SESSIONS_JSON_PATH,
    "Stats JSON": STATS_JSON_PATH,
    "Webhook DB JSON": WEBHOOK_DB_JSON_PATH,
    "FastAPI .env": FASTAPI_ENV_PATH,
    "FastAPI Log": FASTAPI_LOG_PATH,
    "Bot Script": BOT_SCRIPT_PATH,
}
for name, path_obj in critical_paths_fastapi.items():
    if not path_obj.exists() and name not in ["FastAPI Log"]: # Лог файл может не существовать сразу
        logger.warning(f"Path for '{name}' does not exist: {path_obj}. Check .env_bot configuration.")
    elif not path_obj.is_absolute():
         logger.warning(f"Path for '{name}' is not absolute: {path_obj}. This might cause issues.")
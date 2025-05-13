# telegram_management_bot/main_bot.py
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage # Простое хранилище FSM в памяти

# Импортируем конфигурацию и утилиты
from . import bot_config
from .utils.bot_utils import setup_bot_logging

# Импортируем роутеры с хэндлерами
from .handlers import common_handlers, admin_handlers, service_management_handlers, \
                      config_management_handlers, session_management_handlers, \
                      stats_monitoring_handlers

logger = logging.getLogger(__name__)

async def main():
    # Настройка логирования для бота
    setup_bot_logging(log_level_str=bot_config.LOG_LEVEL_BOT, log_file=bot_config.BOT_LOG_PATH)
    logger.info("Starting Telegram Management Bot...")

    if not bot_config.BOT_TOKEN:
        logger.critical("BOT_TOKEN is not configured in .env_bot. Bot cannot start.")
        return
    if not bot_config.ADMIN_IDS:
        logger.warning("ADMIN_IDS is not configured. The bot will not be restricted.")
        # В AdminFilter все равно будет проверка, но лучше иметь это в конфиге.

    # Инициализация бота и диспетчера
    # Используем MemoryStorage для FSM. Для продакшена можно рассмотреть RedisStorage или другое.
    storage = MemoryStorage()
    bot = Bot(token=bot_config.BOT_TOKEN, parse_mode=ParseMode.HTML) # HTML как основной parse_mode
    dp = Dispatcher(storage=storage)

    # Регистрация роутеров
    # Порядок регистрации важен, если есть пересекающиеся фильтры.
    # Сначала более специфичные, потом более общие.
    
    # Роутеры для конкретных функциональных блоков
    dp.include_router(service_management_handlers.router)
    dp.include_router(config_management_handlers.router)
    dp.include_router(session_management_handlers.router)
    dp.include_router(stats_monitoring_handlers.router)
    
    # Роутеры для навигации по меню (должны идти после специфичных, если есть пересечения по callback data)
    dp.include_router(admin_handlers.router) # Обработка кнопок главного меню
    
    # Общие хэндлеры (start, help, unknown commands) - обычно идут последними или первыми, если они не должны перекрываться
    dp.include_router(common_handlers.router)


    logger.info("Bot dispatcher configured with all handlers.")

    # Удаление старых вебхуков (если бот ранее работал через вебхуки)
    # Это полезно при переключении с вебхуков на long polling.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Any existing webhook was deleted.")
    except Exception as e:
        logger.error(f"Error deleting webhook: {e}")

    # Запуск long polling
    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.critical(f"Critical error during bot polling: {e}", exc_info=True)
    finally:
        await bot.session.close()
        logger.info("Bot polling stopped and session closed.")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user (KeyboardInterrupt/SystemExit).")
    except Exception as e:
        # Это для отлова ошибок на самом верхнем уровне, если asyncio.run падает
        logger.critical(f"Unhandled exception at top level: {e}", exc_info=True)
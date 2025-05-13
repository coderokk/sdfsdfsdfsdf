# telegram_management_bot/handlers/common_handlers.py
import logging
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext # Если понадобится сброс состояния

from .. import bot_config
from ..keyboards.inline_keyboards import get_main_menu_keyboard, get_back_to_menu_keyboard
from ..utils.bot_utils import AdminFilter, get_user_info, CANCEL_ACTION

logger = logging.getLogger(__name__)
router = Router()

# Применяем фильтр администратора ко всем хэндлерам в этом роутере, кроме тех, где он не нужен
# router.message.filter(AdminFilter(bot_config.ADMIN_IDS))
# router.callback_query.filter(AdminFilter(bot_config.ADMIN_IDS))
# Лучше применять фильтр точечно к защищаемым командам.

# --- Обработка команды /start ---
@router.message(CommandStart(), AdminFilter(bot_config.ADMIN_IDS))
async def handle_start_admin(message: Message, state: FSMContext):
    user_info = get_user_info(message.from_user)
    logger.info(f"/start command received from admin {user_info}")
    await state.clear() # Сбрасываем состояние FSM на всякий случай
    await message.answer(
        f"👋 Welcome, Admin {message.from_user.first_name}!\n\n"
        "This bot helps you manage the FastAPI Telegram File Processor service.",
        reply_markup=get_main_menu_keyboard()
    )

@router.message(CommandStart()) # Для не-админов
async def handle_start_non_admin(message: Message):
    user_info = get_user_info(message.from_user)
    logger.warning(f"/start command received from NON-ADMIN {user_info}. Access denied.")
    await message.answer(
        "🚫 Access Denied.\n"
        "You are not authorized to use this bot. Please contact the administrator."
    )
    # Можно добавить отправку уведомления администраторам о попытке доступа
    # for admin_id in bot_config.ADMIN_IDS:
    #     try:
    #         await message.bot.send_message(admin_id, f"Unauthorized access attempt by {user_info}")
    #     except Exception as e:
    #         logger.error(f"Failed to send unauthorized access notification to admin {admin_id}: {e}")


# --- Обработка команды /help ---
@router.message(Command("help"), AdminFilter(bot_config.ADMIN_IDS))
async def handle_help_admin(message: Message):
    user_info = get_user_info(message.from_user)
    logger.info(f"/help command received from admin {user_info}")
    help_text = (
        "ℹ️ **Bot Help & Commands**\n\n"
        "This bot allows you to manage the FastAPI service and its Telegram client sessions.\n\n"
        "🔹 **Main Menu Navigation**:\n"
        "  - Use the inline buttons from /start to navigate through features.\n\n"
        "🔹 **Available Commands for Admins**:\n"
        "  - `/start` - Show the main menu.\n"
        "  - `/help` - Show this help message.\n"
        "  - `/status` - Get a quick health status of the FastAPI service.\n\n"
        "🔹 **Feature Categories**:\n"
        "  - `Manage Services`: Restart services, view logs, update scripts.\n"
        "  - `Manage Sessions`: Add, delete, list, freeze/unfreeze Telegram client sessions.\n"
        "  - `FastAPI Configuration`: View and manage the .env file of the FastAPI service.\n"
        "  - `Stats & Monitoring`: View FastAPI health, session statistics, etc.\n\n"
        "⚠️ **High-Risk Operations**:\n"
        "  - Operations like restarting services, updating scripts, or modifying .env files "
        "are high-risk. Always proceed with caution and ensure you have backups if necessary.\n"
        "  - Such operations will require explicit confirmation.\n\n"
        "For detailed information on each feature, navigate to the respective menu section."
    )
    await message.answer(help_text, parse_mode="Markdown")

@router.message(Command("help")) # Для не-админов
async def handle_help_non_admin(message: Message):
    user_info = get_user_info(message.from_user)
    logger.warning(f"/help command received from NON-ADMIN {user_info}. Access denied.")
    await message.answer(
        "🚫 Access Denied.\n"
        "You are not authorized to use this bot. Please contact the administrator."
    )

# --- Обработка callback'а для возврата в главное меню ---
@router.callback_query(F.data == "main_menu", AdminFilter(bot_config.ADMIN_IDS))
async def cq_back_to_main_menu(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.debug(f"Callback 'main_menu' received from admin {user_info}")
    await state.clear() # Сбрасываем состояние FSM
    try:
        await callback_query.message.edit_text(
            "🏠 Main Menu:",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e: # Если сообщение не изменилось или другая ошибка
        logger.warning(f"Error editing message for main_menu, sending new one: {e}")
        await callback_query.message.answer(
            "🏠 Main Menu:",
            reply_markup=get_main_menu_keyboard()
        )
        # Если исходное сообщение было с клавиатурой, ее можно убрать
        # await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.answer()


# --- Обработка callback'а для отмены действия (общая) ---
@router.callback_query(F.data == CANCEL_ACTION, AdminFilter(bot_config.ADMIN_IDS))
async def cq_cancel_action(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Callback '{CANCEL_ACTION}' received from admin {user_info}. Clearing state and returning to main menu.")
    current_state = await state.get_state()
    if current_state is not None:
        logger.info(f"Cancelling state {current_state} for {user_info}")
        await state.clear()
    
    try:
        await callback_query.message.edit_text(
            "🚫 Action Canceled. Returning to Main Menu.",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        logger.warning(f"Error editing message for cancel_action, sending new one: {e}")
        await callback_query.message.answer(
            "🚫 Action Canceled. Returning to Main Menu.",
            reply_markup=get_main_menu_keyboard()
        )
    await callback_query.answer("Action canceled.")


# --- Обработка неизвестных команд от админов ---
@router.message(AdminFilter(bot_config.ADMIN_IDS))
async def handle_unknown_command_admin(message: Message):
    user_info = get_user_info(message.from_user)
    logger.info(f"Unknown command/message '{message.text}' from admin {user_info}")
    await message.answer(
        "❓ Unknown command or message.\n"
        "Please use /start to see the main menu or /help for assistance.",
        reply_markup=get_back_to_menu_keyboard()
    )

# --- Обработка неизвестных callback'ов от админов (если не пойманы другими хэндлерами) ---
# Это может быть полезно для отладки, но в продакшене может быть излишним
@router.callback_query(AdminFilter(bot_config.ADMIN_IDS))
async def handle_unknown_callback_admin(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.warning(f"Unknown callback query '{callback_query.data}' from admin {user_info}")
    await callback_query.answer("Unknown action.", show_alert=True)
    # Можно отправить сообщение с кнопкой "Назад в меню"
    # await callback_query.message.answer("Unknown action. Please use the main menu.", reply_markup=get_main_menu_keyboard())
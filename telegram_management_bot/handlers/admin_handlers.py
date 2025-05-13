# telegram_management_bot/handlers/admin_handlers.py
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext # Для сброса состояния при навигации

from ..keyboards.inline_keyboards import (
    get_main_menu_keyboard,
    get_manage_services_keyboard,
    get_manage_sessions_keyboard,
    get_fastapi_config_keyboard,
    get_stats_monitoring_keyboard
)
from ..utils.bot_utils import AdminFilter, get_user_info

logger = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(AdminFilter(bot_config.ADMIN_IDS)) # Все callback'и здесь только для админов

# --- Обработка нажатий на кнопки главного меню ---

@router.callback_query(F.data == "manage_services")
async def cq_manage_services(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.debug(f"Callback 'manage_services' from {user_info}")
    await state.clear() # Сбрасываем состояние при переходе в новое меню
    await callback_query.message.edit_text(
        "🖥️ **Manage Services**\n\n"
        "Select an action to manage FastAPI or Bot services:",
        reply_markup=get_manage_services_keyboard()
    )
    await callback_query.answer()

@router.callback_query(F.data == "manage_sessions")
async def cq_manage_sessions(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.debug(f"Callback 'manage_sessions' from {user_info}")
    await state.clear()
    await callback_query.message.edit_text(
        "📱 **Manage Sessions**\n\n"
        "Select an action to manage Telegram client sessions used by FastAPI:",
        reply_markup=get_manage_sessions_keyboard()
    )
    await callback_query.answer()

@router.callback_query(F.data == "fastapi_config")
async def cq_fastapi_config(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.debug(f"Callback 'fastapi_config' from {user_info}")
    await state.clear()
    await callback_query.message.edit_text(
        "⚙️ **FastAPI Configuration**\n\n"
        "Select an action to manage the FastAPI service's .env file:",
        reply_markup=get_fastapi_config_keyboard()
    )
    await callback_query.answer()

@router.callback_query(F.data == "stats_monitoring")
async def cq_stats_monitoring(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.debug(f"Callback 'stats_monitoring' from {user_info}")
    await state.clear()
    await callback_query.message.edit_text(
        "📊 **Stats & Monitoring**\n\n"
        "Select an action to view statistics or monitor the service:",
        reply_markup=get_stats_monitoring_keyboard()
    )
    await callback_query.answer()
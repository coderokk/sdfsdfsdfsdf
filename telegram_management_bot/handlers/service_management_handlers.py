# telegram_management_bot/handlers/service_management_handlers.py
import logging
import os
from pathlib import Path
import tempfile

from aiogram import Router, F, Bot
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import aiofiles

from .. import bot_config
from ..keyboards.inline_keyboards import (
    get_confirmation_keyboard,
    get_manage_services_keyboard,
    get_back_to_menu_keyboard
)
from ..utils.bot_utils import AdminFilter, get_user_info, CONFIRM_YES, CONFIRM_NO
from ..utils.fastapi_interaction import download_fastapi_logs
from ..utils.system_commands import restart_fastapi_service, restart_bot_service

logger = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(AdminFilter(bot_config.ADMIN_IDS))
router.message.filter(AdminFilter(bot_config.ADMIN_IDS)) # Для FSM

# --- FSM для обновления скриптов ---
class ScriptUpdateStates(StatesGroup):
    waiting_for_fastapi_script = State()
    waiting_for_bot_script = State()

# --- Перезапуск FastAPI ---
CALLBACK_PREFIX_RESTART_FASTAPI = "confirm_restart_fastapi"
@router.callback_query(F.data == "svc_restart_fastapi")
async def cq_restart_fastapi_confirm(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} initiated FastAPI restart confirmation.")
    await callback_query.message.edit_text(
        "⚠️ **Confirm FastAPI Restart** ⚠️\n\n"
        "Are you sure you want to restart the FastAPI service? This will interrupt any ongoing operations.",
        reply_markup=get_confirmation_keyboard(CALLBACK_PREFIX_RESTART_FASTAPI)
    )
    await callback_query.answer()

@router.callback_query(F.data.startswith(CALLBACK_PREFIX_RESTART_FASTAPI))
async def cq_restart_fastapi_action(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    action = callback_query.data.split(":")[2]

    if action == CONFIRM_YES:
        logger.info(f"Admin {user_info} confirmed FastAPI restart.")
        await callback_query.message.edit_text("🚀 Restarting FastAPI service... Please wait.", reply_markup=None)
        
        success, message = await restart_fastapi_service()
        
        if success:
            await callback_query.message.edit_text(
                f"✅ FastAPI service restart command executed.\n\n{message}",
                reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
            )
        else:
            await callback_query.message.edit_text(
                f"❌ Failed to execute FastAPI service restart command.\n\nError: {message}",
                reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
            )
    elif action == CONFIRM_NO:
        logger.info(f"Admin {user_info} canceled FastAPI restart.")
        await callback_query.message.edit_text(
            "🚫 FastAPI restart canceled.",
            reply_markup=get_manage_services_keyboard()
        )
    await callback_query.answer()

# --- Перезапуск Бота ---
CALLBACK_PREFIX_RESTART_BOT = "confirm_restart_bot"
@router.callback_query(F.data == "svc_restart_bot")
async def cq_restart_bot_confirm(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} initiated Bot restart confirmation.")
    await callback_query.message.edit_text(
        "⚠️ **Confirm Bot Restart** ⚠️\n\n"
        "Are you sure you want to restart this bot? You will lose connection temporarily.",
        reply_markup=get_confirmation_keyboard(CALLBACK_PREFIX_RESTART_BOT)
    )
    await callback_query.answer()

@router.callback_query(F.data.startswith(CALLBACK_PREFIX_RESTART_BOT))
async def cq_restart_bot_action(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    action = callback_query.data.split(":")[2]

    if action == CONFIRM_YES:
        logger.info(f"Admin {user_info} confirmed Bot restart.")
        await callback_query.message.edit_text("🤖 Restarting Bot... Please wait. You might need to send /start again after restart.", reply_markup=None)
        await callback_query.answer("Bot is restarting...") # Ответ перед фактическим перезапуском
        
        # Команда перезапуска бота. Ответ от этой команды может не дойти.
        success, message = await restart_bot_service()
        if not success: # Если команда не удалась, бот еще жив, можно отправить сообщение
            logger.error(f"Bot restart command failed: {message}")
            # Попытка отправить сообщение об ошибке, если бот еще работает
            try:
                await callback_query.bot.send_message(
                    callback_query.from_user.id,
                    f"❌ Failed to execute Bot restart command.\n\nError: {message}\n\nThe bot might still be running.",
                    reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
                )
            except Exception as e:
                logger.error(f"Could not send bot restart failure message: {e}")
        # Если success, бот должен быть перезапущен менеджером процессов.
        
    elif action == CONFIRM_NO:
        logger.info(f"Admin {user_info} canceled Bot restart.")
        await callback_query.message.edit_text(
            "🚫 Bot restart canceled.",
            reply_markup=get_manage_services_keyboard()
        )
        await callback_query.answer()


# --- Просмотр логов FastAPI ---
@router.callback_query(F.data == "svc_view_fastapi_logs")
async def cq_view_fastapi_logs(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} requested FastAPI logs.")
    await callback_query.message.edit_text("📝 Fetching FastAPI logs... Please wait.", reply_markup=None)
    
    log_content = await download_fastapi_logs() # Эта функция уже есть в fastapi_interaction
    
    if log_content:
        # Отправляем логи как документ, если они слишком длинные, или как сообщение
        if len(log_content) > 4000: # Telegram лимит на сообщение ~4096
            try:
                with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log", encoding="utf-8") as tmp_file:
                    tmp_file.write(log_content)
                    tmp_file_path = tmp_file.name
                
                await callback_query.message.answer_document(
                    document=aiofiles.os.path.abspath(tmp_file_path), # Используем FSInputFile когда будет доступен в aiogram 3.x
                    caption="FastAPI Logs",
                    reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
                )
                await callback_query.message.delete() # Удаляем "Fetching..."
                os.remove(tmp_file_path) # Удаляем временный файл
            except Exception as e:
                logger.error(f"Error sending FastAPI logs as document: {e}")
                await callback_query.message.edit_text(
                    f"📝 FastAPI Logs (last 50 lines from server, full log too large to send as message):\n\n"
                    f"```\n{log_content[-2000:]}\n```", # Показываем хвост
                    parse_mode="Markdown",
                    reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
                )
        else:
            await callback_query.message.edit_text(
                f"📝 FastAPI Logs:\n\n```\n{log_content}\n```",
                parse_mode="Markdown",
                reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
            )
    else:
        await callback_query.message.edit_text(
            "❌ Could not fetch FastAPI logs. The service might be down or logs unavailable.",
            reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
        )
    await callback_query.answer()

# --- Просмотр логов Бота ---
@router.callback_query(F.data == "svc_view_bot_logs")
async def cq_view_bot_logs(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} requested Bot logs.")
    
    log_file_path = bot_config.BOT_LOG_PATH
    if not log_file_path.exists():
        await callback_query.message.edit_text(
            "📜 Bot log file not found.",
            reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
        )
        await callback_query.answer()
        return

    await callback_query.message.edit_text("📜 Fetching Bot logs... Please wait.", reply_markup=None)
    
    try:
        async with aiofiles.open(log_file_path, "r", encoding="utf-8") as f:
            log_lines = await f.readlines()
        log_content = "".join(log_lines[-50:]) # Последние 50 строк
        
        if log_content:
            await callback_query.message.edit_text(
                f"📜 Bot Logs (last 50 lines):\n\n```\n{log_content}\n```",
                parse_mode="Markdown",
                reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
            )
        else:
            await callback_query.message.edit_text(
                "📜 Bot log file is empty.",
                reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
            )
    except Exception as e:
        logger.error(f"Error reading bot log file: {e}")
        await callback_query.message.edit_text(
            f"❌ Error reading bot log file: {e}",
            reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services")
        )
    await callback_query.answer()


# --- Обновление скрипта FastAPI ---
CALLBACK_PREFIX_UPDATE_FASTAPI_SCRIPT = "confirm_upd_fastapi_scr"
@router.callback_query(F.data == "svc_update_fastapi_script")
async def cq_update_fastapi_script_start(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} initiated FastAPI script update.")
    await state.set_state(ScriptUpdateStates.waiting_for_fastapi_script)
    await callback_query.message.edit_text(
        "🔄 **Update FastAPI Script**\n\n"
        "Please send the new `.py` file for the FastAPI service.\n"
        "Current script path: `{}`\n\n"
        "⚠️ **HIGH RISK**: This will replace the existing script. Ensure you have a backup.".format(bot_config.FASTAPI_SCRIPT_PATH),
        parse_mode="Markdown",
        reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services") # Кнопка отмены
    )
    await callback_query.answer()

@router.message(StateFilter(ScriptUpdateStates.waiting_for_fastapi_script), F.document)
async def process_fastapi_script_upload(message: Message, state: FSMContext, bot: Bot):
    user_info = get_user_info(message.from_user)
    if not message.document.file_name.endswith(".py"):
        await message.reply("❌ Invalid file type. Please upload a `.py` file.",
                            reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services"))
        return

    logger.info(f"Admin {user_info} uploaded FastAPI script: {message.document.file_name}")
    
    # Сохраняем документ во временный файл
    with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as tmp_file:
        await bot.download(message.document, destination=tmp_file.name)
        temp_script_path = tmp_file.name
        
    await state.update_data(temp_script_path=temp_script_path, original_filename=message.document.file_name)
    
    await message.answer(
        f"✅ File `{message.document.file_name}` received.\n\n"
        f"This will replace the script at `{bot_config.FASTAPI_SCRIPT_PATH}`.\n"
        "Are you sure you want to proceed?",
        parse_mode="Markdown",
        reply_markup=get_confirmation_keyboard(CALLBACK_PREFIX_UPDATE_FASTAPI_SCRIPT)
    )

@router.callback_query(F.data.startswith(CALLBACK_PREFIX_UPDATE_FASTAPI_SCRIPT), StateFilter(ScriptUpdateStates.waiting_for_fastapi_script))
async def cq_confirm_fastapi_script_update(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    action = callback_query.data.split(":")[2]
    data = await state.get_data()
    temp_script_path = data.get("temp_script_path")
    original_filename = data.get("original_filename")

    if not temp_script_path or not Path(temp_script_path).exists():
        logger.error(f"Temporary script path not found in state or file missing for FastAPI update by {user_info}.")
        await callback_query.message.edit_text(
            "❌ Error: Uploaded file not found. Please try again.",
            reply_markup=get_manage_services_keyboard()
        )
        await state.clear()
        await callback_query.answer("Error with uploaded file.", show_alert=True)
        return

    if action == CONFIRM_YES:
        logger.info(f"Admin {user_info} confirmed update of FastAPI script with {original_filename}.")
        target_script_path = bot_config.FASTAPI_SCRIPT_PATH
        backup_path = target_script_path.with_suffix(f".backup_{Path(temp_script_path).stat().st_mtime:.0f}.py")

        try:
            # 1. Backup
            if target_script_path.exists():
                logger.info(f"Backing up current FastAPI script from {target_script_path} to {backup_path}")
                os.rename(target_script_path, backup_path) # Синхронно, но быстро
            
            # 2. Replace
            logger.info(f"Replacing FastAPI script at {target_script_path} with {temp_script_path}")
            os.rename(temp_script_path, target_script_path) # Перемещаем временный файл

            await callback_query.message.edit_text(
                f"✅ FastAPI script updated successfully with `{original_filename}`.\n"
                f"Backup created at `{backup_path.name}` (in the same directory).\n\n"
                "It's highly recommended to **restart the FastAPI service** now.",
                parse_mode="Markdown",
                reply_markup=get_manage_services_keyboard() # Предлагаем вернуться в меню сервисов (где есть кнопка рестарта)
            )
        except Exception as e:
            logger.error(f"Error updating FastAPI script: {e}", exc_info=True)
            await callback_query.message.edit_text(
                f"❌ Error updating FastAPI script: {e}\n"
                "Please check file permissions and paths. The old script might be in backup.",
                reply_markup=get_manage_services_keyboard()
            )
            # Попытка восстановить бэкап, если замена не удалась, а бэкап был создан
            if backup_path.exists() and not target_script_path.exists():
                try:
                    os.rename(backup_path, target_script_path)
                    logger.info(f"Restored backup to {target_script_path} after update failure.")
                except Exception as e_restore:
                    logger.error(f"Failed to restore backup {backup_path} to {target_script_path}: {e_restore}")
        finally:
            if Path(temp_script_path).exists(): # Если временный файл все еще существует (например, rename не удался)
                os.remove(temp_script_path)
            await state.clear()

    elif action == CONFIRM_NO:
        logger.info(f"Admin {user_info} canceled FastAPI script update.")
        if Path(temp_script_path).exists():
            os.remove(temp_script_path)
        await callback_query.message.edit_text(
            "🚫 FastAPI script update canceled.",
            reply_markup=get_manage_services_keyboard()
        )
        await state.clear()
    
    await callback_query.answer()

# Обработка сообщений не-документов в состоянии ожидания скрипта
@router.message(StateFilter(ScriptUpdateStates.waiting_for_fastapi_script))
async def process_fastapi_script_invalid_input(message: Message, state: FSMContext):
    await message.reply("Please upload a `.py` file or cancel the operation.",
                        reply_markup=get_back_to_menu_keyboard("manage_services", "⬅️ Back to Services"))
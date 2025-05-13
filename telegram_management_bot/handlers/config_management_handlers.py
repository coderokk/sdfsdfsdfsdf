# telegram_management_bot/handlers/config_management_handlers.py
import logging
import os
from pathlib import Path
import re
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
    get_fastapi_config_keyboard,
    get_back_to_menu_keyboard
)
from ..utils.bot_utils import AdminFilter, get_user_info, CONFIRM_YES, CONFIRM_NO, CANCEL_ACTION

logger = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(AdminFilter(bot_config.ADMIN_IDS))
router.message.filter(AdminFilter(bot_config.ADMIN_IDS)) # Для FSM

# --- FSM для редактирования/добавления переменных .env ---
class EnvEditStates(StatesGroup):
    waiting_for_var_name_to_edit = State()
    waiting_for_var_value_to_edit = State()
    waiting_for_var_name_to_add = State()
    waiting_for_var_value_to_add = State()

# --- Утилиты для работы с .env файлом ---
SENSITIVE_ENV_KEYS = ["API_HASH", "S3_SECRET_ACCESS_KEY", "FASTAPI_CLIENT_API_KEY", "BOT_TOKEN"] # Ключи для маскирования

def mask_sensitive_value(key: str, value: str) -> str:
    if key.upper() in SENSITIVE_ENV_KEYS:
        if len(value) > 4:
            return value[:2] + "*" * (len(value) - 4) + value[-2:]
        return "****"
    return value

async def read_env_file_content(path: Path) -> str:
    if not path.exists():
        return "Error: .env file not found at specified path."
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            lines = await f.readlines()
        
        masked_lines = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"): # Пропускаем пустые строки и комментарии
                masked_lines.append(line)
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'") # Убираем кавычки для маскирования
                masked_lines.append(f"{key}={mask_sensitive_value(key, value)}")
            else:
                masked_lines.append(line) # Строки без "=" (невалидные, но сохраняем)
        return "\n".join(masked_lines)
    except Exception as e:
        logger.error(f"Error reading .env file {path}: {e}")
        return f"Error reading .env file: {e}"

async def update_env_variable(path: Path, var_name: str, new_value: str) -> bool:
    if not path.exists():
        logger.error(f".env file not found at {path} for update.")
        return False
    try:
        async with aiofiles.open(path, "r+", encoding="utf-8") as f:
            lines = await f.readlines()
            new_lines = []
            found = False
            var_name_upper = var_name.upper() # Сравнение без учета регистра для ключа

            for line in lines:
                stripped_line = line.strip()
                if not stripped_line.startswith("#") and "=" in stripped_line:
                    key, _ = stripped_line.split("=", 1)
                    if key.strip().upper() == var_name_upper:
                        # Форматируем значение: если есть пробелы, заключаем в кавычки
                        formatted_value = f'"{new_value}"' if " " in new_value else new_value
                        new_lines.append(f"{var_name}={formatted_value}\n")
                        found = True
                        logger.info(f"Updated variable '{var_name}' in .env file.")
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)
            
            if not found: # Если переменная не найдена, это ошибка для функции update
                logger.warning(f"Variable '{var_name}' not found in .env for update. This function should only update existing.")
                return False # Или можно решить добавлять, но это логика add_env_variable

            await f.seek(0)
            await f.writelines(new_lines)
            await f.truncate()
        return True
    except Exception as e:
        logger.error(f"Error updating .env file {path}: {e}")
        return False

async def add_env_variable(path: Path, var_name: str, var_value: str) -> bool:
    try:
        # Проверяем, существует ли уже такая переменная (без учета регистра)
        var_name_upper = var_name.upper()
        if path.exists():
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                lines = await f.readlines()
            for line in lines:
                stripped_line = line.strip()
                if not stripped_line.startswith("#") and "=" in stripped_line:
                    key, _ = stripped_line.split("=", 1)
                    if key.strip().upper() == var_name_upper:
                        logger.warning(f"Variable '{var_name}' already exists in .env. Use edit instead.")
                        return False # Переменная уже существует

        # Добавляем в конец файла
        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            formatted_value = f'"{var_value}"' if " " in var_value else var_value
            await f.write(f"\n{var_name}={formatted_value}\n") # Добавляем с новой строки для чистоты
        logger.info(f"Added variable '{var_name}' to .env file.")
        return True
    except Exception as e:
        logger.error(f"Error adding to .env file {path}: {e}")
        return False

# --- Просмотр .env ---
@router.callback_query(F.data == "config_view_env")
async def cq_view_env(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} requested to view FastAPI .env file.")
    await callback_query.message.edit_text("📄 Fetching .env content... Please wait.", reply_markup=None)
    
    env_content = await read_env_file_content(bot_config.FASTAPI_ENV_PATH)
    
    # Отправляем как документ, если слишком длинный
    if len(env_content) > 4000:
        try:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".env", encoding="utf-8") as tmp_file:
                # Пишем оригинальный (немаскированный) контент в файл для скачивания, если это нужно
                # Но для безопасности лучше тоже маскированный
                async with aiofiles.open(bot_config.FASTAPI_ENV_PATH, "r", encoding="utf-8") as f_orig:
                     original_content_for_file = await f_orig.read()
                tmp_file.write(original_content_for_file) # Или env_content, если хотим маскированный файл
                tmp_file_path = tmp_file.name
            
            await callback_query.message.answer_document(
                document=aiofiles.os.path.abspath(tmp_file_path),
                caption="FastAPI .env File (sensitive values might be masked in preview)",
                reply_markup=get_back_to_menu_keyboard("fastapi_config", "⬅️ Back to Config")
            )
            await callback_query.message.delete()
            os.remove(tmp_file_path)
        except Exception as e:
            logger.error(f"Error sending .env as document: {e}")
            await callback_query.message.edit_text(
                f"📄 FastAPI .env Content (masked, full content too large for message):\n\n"
                f"```\n{env_content[-2000:]}\n```", # Показываем хвост
                parse_mode="Markdown",
                reply_markup=get_back_to_menu_keyboard("fastapi_config", "⬅️ Back to Config")
            )
    else:
        await callback_query.message.edit_text(
            f"📄 FastAPI .env Content (sensitive values masked):\n\n```\n{env_content}\n```",
            parse_mode="Markdown",
            reply_markup=get_back_to_menu_keyboard("fastapi_config", "⬅️ Back to Config")
        )
    await callback_query.answer()

# --- Редактирование переменной .env ---
CALLBACK_PREFIX_CONFIRM_EDIT_ENV = "confirm_edit_env"
@router.callback_query(F.data == "config_edit_env_var_name")
async def cq_edit_env_var_name_start(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} initiated .env variable edit.")
    await state.set_state(EnvEditStates.waiting_for_var_name_to_edit)
    await callback_query.message.edit_text(
        "✏️ **Edit .env Variable**\n\n"
        "Please send the name of the variable you want to edit (e.g., `S3_BUCKET_NAME`).",
        reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel") # Кнопка отмены FSM
    )
    await callback_query.answer()

@router.message(StateFilter(EnvEditStates.waiting_for_var_name_to_edit))
async def process_env_var_name_to_edit(message: Message, state: FSMContext):
    var_name = message.text.strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", var_name): # Простая проверка имени переменной
        await message.reply("❌ Invalid variable name format. Please use letters, numbers, and underscores, starting with a letter or underscore.")
        return
    
    await state.update_data(var_name_to_edit=var_name)
    await state.set_state(EnvEditStates.waiting_for_var_value_to_edit)
    await message.answer(
        f"OK. Now send the new value for variable `{var_name}`.\n"
        "Sensitive values will be stored as is.",
        parse_mode="Markdown",
        reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
    )

@router.message(StateFilter(EnvEditStates.waiting_for_var_value_to_edit))
async def process_env_var_value_to_edit(message: Message, state: FSMContext):
    var_value = message.text.strip() # Не убираем кавычки, если пользователь их ввел
    data = await state.get_data()
    var_name = data.get("var_name_to_edit")

    await state.update_data(var_value_to_edit=var_value)
    
    await message.answer(
        f"⚠️ **Confirm .env Edit** ⚠️\n\n"
        f"You are about to set:\n`{var_name}` = `{mask_sensitive_value(var_name, var_value)}` (value will be stored as entered)\n\n"
        "This will modify the `.env` file of the FastAPI service. Are you sure?",
        parse_mode="Markdown",
        reply_markup=get_confirmation_keyboard(CALLBACK_PREFIX_CONFIRM_EDIT_ENV)
    )

@router.callback_query(F.data.startswith(CALLBACK_PREFIX_CONFIRM_EDIT_ENV), StateFilter(EnvEditStates.waiting_for_var_value_to_edit))
async def cq_confirm_env_edit_action(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    action = callback_query.data.split(":")[2]
    data = await state.get_data()
    var_name = data.get("var_name_to_edit")
    var_value = data.get("var_value_to_edit")

    if action == CONFIRM_YES:
        logger.info(f"Admin {user_info} confirmed editing .env variable '{var_name}'.")
        
        # Рекомендуется сделать бэкап .env перед изменением
        env_path = bot_config.FASTAPI_ENV_PATH
        backup_env_path = env_path.with_suffix(f".backup_{Path(env_path).stat().st_mtime:.0f}.env")
        if env_path.exists():
            try:
                os.rename(env_path, backup_env_path)
                logger.info(f"Backed up .env to {backup_env_path.name}")
            except Exception as e_backup:
                logger.error(f"Failed to backup .env before edit: {e_backup}")
                await callback_query.message.edit_text(
                    f"❌ Failed to create .env backup. Aborting edit.",
                    reply_markup=get_fastapi_config_keyboard()
                )
                await state.clear()
                await callback_query.answer("Backup failed.", show_alert=True)
                return

        success = await update_env_variable(backup_env_path, var_name, var_value) # Обновляем бэкап
        
        if success:
            os.rename(backup_env_path, env_path) # Переименовываем обновленный бэкап обратно
            await callback_query.message.edit_text(
                f"✅ Variable `{var_name}` updated in `.env` file.\n"
                "A restart of the FastAPI service is required for changes to take effect.",
                parse_mode="Markdown",
                reply_markup=get_fastapi_config_keyboard()
            )
        else:
            # Восстанавливаем оригинальный бэкап, если обновление не удалось
            if backup_env_path.exists() and env_path.exists() and env_path.read_bytes() != backup_env_path.read_bytes(): # Если env_path был создан пустой или изменен
                 os.remove(env_path) # Удаляем потенциально испорченный
                 os.rename(backup_env_path, env_path)
                 logger.info(f"Restored original .env from backup after failed update.")
            elif not env_path.exists() and backup_env_path.exists(): # Если env_path был удален
                 os.rename(backup_env_path, env_path)
                 logger.info(f"Restored original .env from backup after failed update (env was missing).")


            await callback_query.message.edit_text(
                f"❌ Failed to update variable `{var_name}` in `.env` file. "
                "This could be because the variable was not found or due to a file error. Original .env restored if backup was made.",
                parse_mode="Markdown",
                reply_markup=get_fastapi_config_keyboard()
            )
    elif action == CONFIRM_NO:
        logger.info(f"Admin {user_info} canceled editing .env variable '{var_name}'.")
        await callback_query.message.edit_text(
            "🚫 .env variable edit canceled.",
            reply_markup=get_fastapi_config_keyboard()
        )
    
    await state.clear()
    await callback_query.answer()


# --- Добавление переменной в .env ---
CALLBACK_PREFIX_CONFIRM_ADD_ENV = "confirm_add_env"
# (Логика FSM будет очень похожа на редактирование, поэтому для краткости можно ее доработать по аналогии)
# Начало FSM для добавления переменной
@router.callback_query(F.data == "config_add_env_var_name")
async def cq_add_env_var_name_start(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} initiated adding new .env variable.")
    await state.set_state(EnvEditStates.waiting_for_var_name_to_add)
    await callback_query.message.edit_text(
        "➕ **Add .env Variable**\n\n"
        "Please send the name of the new variable (e.g., `NEW_FEATURE_FLAG`).",
        reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
    )
    await callback_query.answer()

@router.message(StateFilter(EnvEditStates.waiting_for_var_name_to_add))
async def process_env_var_name_to_add(message: Message, state: FSMContext):
    var_name = message.text.strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", var_name):
        await message.reply("❌ Invalid variable name format.")
        return
    
    await state.update_data(var_name_to_add=var_name)
    await state.set_state(EnvEditStates.waiting_for_var_value_to_add)
    await message.answer(
        f"OK. Now send the value for the new variable `{var_name}`.",
        parse_mode="Markdown",
        reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel")
    )

@router.message(StateFilter(EnvEditStates.waiting_for_var_value_to_add))
async def process_env_var_value_to_add(message: Message, state: FSMContext):
    var_value = message.text.strip()
    data = await state.get_data()
    var_name = data.get("var_name_to_add")

    await state.update_data(var_value_to_add=var_value)
    
    await message.answer(
        f"⚠️ **Confirm .env Add** ⚠️\n\n"
        f"You are about to add:\n`{var_name}` = `{mask_sensitive_value(var_name, var_value)}`\n\n"
        "This will append to the `.env` file of the FastAPI service. Are you sure?",
        parse_mode="Markdown",
        reply_markup=get_confirmation_keyboard(CALLBACK_PREFIX_CONFIRM_ADD_ENV)
    )

@router.callback_query(F.data.startswith(CALLBACK_PREFIX_CONFIRM_ADD_ENV), StateFilter(EnvEditStates.waiting_for_var_value_to_add))
async def cq_confirm_env_add_action(callback_query: CallbackQuery, state: FSMContext):
    user_info = get_user_info(callback_query.from_user)
    action = callback_query.data.split(":")[2]
    data = await state.get_data()
    var_name = data.get("var_name_to_add")
    var_value = data.get("var_value_to_add")

    if action == CONFIRM_YES:
        logger.info(f"Admin {user_info} confirmed adding .env variable '{var_name}'.")
        
        # Бэкап перед изменением
        env_path = bot_config.FASTAPI_ENV_PATH
        backup_env_path = env_path.with_suffix(f".backup_{Path(env_path).stat().st_mtime:.0f}.env")
        if env_path.exists():
            try:
                # Копируем, а не переименовываем, т.к. будем добавлять в существующий
                async with aiofiles.open(env_path, "rb") as f_src, aiofiles.open(backup_env_path, "wb") as f_dst:
                    await f_dst.write(await f_src.read())
                logger.info(f"Backed up .env to {backup_env_path.name}")
            except Exception as e_backup:
                logger.error(f"Failed to backup .env before add: {e_backup}")
                # Продолжаем без бэкапа, но с предупреждением
                await callback_query.answer("Warning: Failed to create .env backup.", show_alert=True)


        success = await add_env_variable(env_path, var_name, var_value)
        
        if success:
            await callback_query.message.edit_text(
                f"✅ Variable `{var_name}` added to `.env` file.\n"
                "A restart of the FastAPI service is required for changes to take effect.",
                parse_mode="Markdown",
                reply_markup=get_fastapi_config_keyboard()
            )
        else:
            await callback_query.message.edit_text(
                f"❌ Failed to add variable `{var_name}` to `.env` file. "
                "This could be because the variable already exists or due to a file error.",
                parse_mode="Markdown",
                reply_markup=get_fastapi_config_keyboard()
            )
    elif action == CONFIRM_NO:
        logger.info(f"Admin {user_info} canceled adding .env variable '{var_name}'.")
        await callback_query.message.edit_text(
            "🚫 .env variable add canceled.",
            reply_markup=get_fastapi_config_keyboard()
        )
    
    await state.clear()
    await callback_query.answer()

# Обработка некорректного ввода в FSM для .env
@router.message(StateFilter(EnvEditStates))
async def process_env_edit_invalid_input(message: Message, state: FSMContext):
    await message.reply("Invalid input for current step. Please provide the requested information or cancel.",
                        reply_markup=get_back_to_menu_keyboard(CANCEL_ACTION, "❌ Cancel Operation"))
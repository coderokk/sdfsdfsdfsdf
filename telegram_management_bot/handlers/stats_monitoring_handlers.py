# telegram_management_bot/handlers/stats_monitoring_handlers.py
import logging
import json
import tempfile
import os

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, FSInputFile # FSInputFile для aiogram 3.x
import aiofiles

from .. import bot_config
from ..keyboards.inline_keyboards import get_stats_monitoring_keyboard, get_back_to_menu_keyboard
from ..utils.bot_utils import AdminFilter, get_user_info
from ..utils.fastapi_interaction import get_fastapi_health, get_fastapi_account_stats

logger = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(AdminFilter(bot_config.ADMIN_IDS))

# --- FastAPI Status (/health) ---
@router.callback_query(F.data == "stats_fastapi_health")
async def cq_fastapi_health(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} requested FastAPI health status.")
    await callback_query.message.edit_text("📈 Fetching FastAPI status... Please wait.", reply_markup=None)
    
    health_data = await get_fastapi_health()
    
    if health_data and not health_data.get("error"):
        # Форматируем ответ для лучшей читаемости
        status_text = f"📊 **FastAPI Service Status** ({health_data.get('app_version', 'N/A')})\n\n"
        status_text += f"**Overall Status**: `{health_data.get('service_status', 'Unknown').upper()}`\n"
        if health_data.get('message'):
            status_text += f"**Message**: {health_data['message']}\n\n"
        
        status_text += "**Client Summary**:\n"
        status_text += f"  - Configured: {health_data.get('total_configured_clients', 0)}\n"
        status_text += f"  - Active: {health_data.get('active_clients', 0)}\n"
        status_text += f"  - Cooldown: {health_data.get('cooldown_clients_count', 0)}\n"
        status_text += f"  - Flood Wait: {health_data.get('flood_wait_clients_count', 0)}\n"
        status_text += f"  - Errors (total): {health_data.get('error_clients_count', 0)}\n"
        status_text += f"  - Auth Errors: {health_data.get('auth_error_clients_count', 0)}\n"
        status_text += f"  - Daily Limit Reached: {health_data.get('clients_at_daily_limit_today', 0)}\n"
        status_text += f"  - Tasks Waiting for Client: {health_data.get('tasks_waiting_for_client', 0)}\n\n"
        
        status_text += f"**S3 Configured**: {'✅ Yes' if health_data.get('s3_configured') else '❌ No'}\n"
        if health_data.get('s3_configured'):
            status_text += f"  - Public Base URL: {'✅ Yes' if health_data.get('s3_public_base_url_configured') else '❌ No'}\n"
        status_text += f"**Daily Limit/Session**: {health_data.get('daily_request_limit_per_session', 'N/A')}\n\n"

        detailed_statuses = health_data.get("clients_statuses_detailed", {})
        if detailed_statuses:
            status_text += "**Client Details**:\n"
            for client_display_key, client_status_text in detailed_statuses.items():
                # Убираем SID из ключа для краткости в боте
                client_name_phone = client_display_key.split(", ...")[0] 
                status_text += f"  - `{client_name_phone}`: {client_status_text}\n"
        
        if len(status_text) > 4000: # Если слишком длинно, отправляем как документ
            try:
                with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt", encoding="utf-8") as tmp_file:
                    tmp_file.write(status_text.replace("`", "").replace("*", "")) # Убираем Markdown для txt
                    tmp_file_path = tmp_file.name
                
                await callback_query.message.answer_document(
                    document=FSInputFile(tmp_file_path, filename="fastapi_health.txt"),
                    caption="FastAPI Health Status",
                    reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
                )
                await callback_query.message.delete() # Удаляем "Fetching..."
                os.remove(tmp_file_path)
            except Exception as e_doc:
                logger.error(f"Error sending health status as document: {e_doc}")
                # Показываем только начало, если не удалось отправить как документ
                await callback_query.message.edit_text(
                    status_text[:4000] + "\n\n... (message truncated)",
                    parse_mode="Markdown",
                    reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
                )
        else:
            await callback_query.message.edit_text(
                status_text,
                parse_mode="Markdown",
                reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
            )
    elif health_data and health_data.get("error"):
        await callback_query.message.edit_text(
            f"❌ Error fetching FastAPI status:\n`{health_data.get('detail', 'Unknown error')}`",
            parse_mode="Markdown",
            reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
        )
    else:
        await callback_query.message.edit_text(
            "❌ Could not connect to FastAPI service or received an invalid response.",
            reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
        )
    await callback_query.answer()

# --- Session Stats Overview (/stats/accounts) ---
@router.callback_query(F.data == "stats_session_overview")
async def cq_session_stats_overview(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} requested session stats overview.")
    await callback_query.message.edit_text("💾 Fetching session stats... Please wait.", reply_markup=None)

    stats_response = await get_fastapi_account_stats()

    if stats_response and not stats_response.get("error"):
        data = stats_response.get("data", {})
        if not data:
            await callback_query.message.edit_text(
                "💾 **Session Statistics**\n\nNo statistics data available from FastAPI.",
                reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
            )
            await callback_query.answer()
            return

        # Отправляем как JSON документ
        try:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".json", encoding="utf-8") as tmp_file:
                json.dump(data, tmp_file, indent=2, ensure_ascii=False)
                tmp_file_path = tmp_file.name
            
            await callback_query.message.answer_document(
                document=FSInputFile(tmp_file_path, filename="session_stats.json"),
                caption=f"Session Statistics Overview (from {stats_response.get('data_source_file', 'N/A')})\n"
                        f"Retrieved at: {stats_response.get('retrieved_at_utc', 'N/A')}",
                reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
            )
            await callback_query.message.delete() # Удаляем "Fetching..."
            os.remove(tmp_file_path)
        except Exception as e:
            logger.error(f"Error sending session stats as document: {e}")
            await callback_query.message.edit_text(
                "❌ Error preparing session stats for download. Check bot logs.",
                reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
            )
    elif stats_response and stats_response.get("error"):
        await callback_query.message.edit_text(
            f"❌ Error fetching session stats:\n`{stats_response.get('detail', 'Unknown error')}`",
            parse_mode="Markdown",
            reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
        )
    else:
        await callback_query.message.edit_text(
            "❌ Could not connect to FastAPI service for session stats or received an invalid response.",
            reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
        )
    await callback_query.answer()

# --- Экспорт Webhook Tasks DB ---
@router.callback_query(F.data == "stats_webhook_db_export")
async def cq_webhook_db_export(callback_query: CallbackQuery):
    user_info = get_user_info(callback_query.from_user)
    logger.info(f"Admin {user_info} requested webhook_tasks.json export.")
    
    webhook_db_path = bot_config.WEBHOOK_DB_JSON_PATH
    if not webhook_db_path.exists():
        await callback_query.message.edit_text(
            f"📨 Webhook tasks database file (`{webhook_db_path.name}`) not found.",
            parse_mode="Markdown",
            reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
        )
        await callback_query.answer()
        return

    try:
        await callback_query.message.answer_document(
            document=FSInputFile(webhook_db_path), # Отправляем напрямую
            caption="FastAPI Webhook Tasks Database",
            reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
        )
        # Не удаляем исходное сообщение, если это callback от кнопки
        if callback_query.message.text and "Fetching" in callback_query.message.text : # Если было сообщение "Fetching..."
             await callback_query.message.delete()
        else: # Если это было меню, то просто отвечаем на callback
             await callback_query.answer("Webhook DB sent.")

    except Exception as e:
        logger.error(f"Error sending webhook_tasks.json: {e}")
        await callback_query.message.answer(
            f"❌ Error sending webhook tasks database: {e}",
            reply_markup=get_back_to_menu_keyboard("stats_monitoring", "⬅️ Back")
        )
        await callback_query.answer("Error sending file.", show_alert=True)
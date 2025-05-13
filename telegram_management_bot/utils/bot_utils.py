# telegram_management_bot/keyboards/inline_keyboards.py
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder # Для aiogram 3.x
from typing import List, Optional, Dict, Any

from ..utils.bot_utils import CONFIRM_YES, CONFIRM_NO, CANCEL_ACTION, PaginatorCallback, ActionWithIdCallback, ConfirmationCallback

# --- Main Menu ---
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🖥️ Manage Services", callback_data="manage_services"))
    builder.row(InlineKeyboardButton(text="📱 Manage Sessions", callback_data="manage_sessions"))
    builder.row(InlineKeyboardButton(text="⚙️ FastAPI Configuration", callback_data="fastapi_config"))
    builder.row(InlineKeyboardButton(text="📊 Stats & Monitoring", callback_data="stats_monitoring"))
    # builder.row(InlineKeyboardButton(text="❔ Help", callback_data="help_info")) # Можно добавить кнопку помощи
    return builder.as_markup()

# --- Manage Services Menu ---
def get_manage_services_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚀 Restart FastAPI", callback_data="svc_restart_fastapi"),
        InlineKeyboardButton(text="🤖 Restart Bot", callback_data="svc_restart_bot")
    )
    builder.row(
        InlineKeyboardButton(text="📝 FastAPI Logs", callback_data="svc_view_fastapi_logs"),
        InlineKeyboardButton(text="📜 Bot Logs", callback_data="svc_view_bot_logs")
    )
    builder.row(
        InlineKeyboardButton(text="🔄 Update FastAPI Script", callback_data="svc_update_fastapi_script"),
        # InlineKeyboardButton(text="🔄 Update Bot Script", callback_data="svc_update_bot_script") # Пока закомментируем, т.к. сложнее
    )
    builder.row(InlineKeyboardButton(text="⬅️ Back to Main Menu", callback_data="main_menu"))
    return builder.as_markup()

# --- Manage Sessions Menu ---
def get_manage_sessions_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Add Session", callback_data="session_add_new"))
    builder.row(InlineKeyboardButton(text="➖ Delete Session", callback_data="session_delete_select"))
    builder.row(InlineKeyboardButton(text="📋 List Sessions", callback_data="session_list_all:page:0")) # Начинаем с 0 страницы
    # builder.row(InlineKeyboardButton(text="ℹ️ Session Details", callback_data="session_details_select")) # Детали можно встроить в List
    builder.row(
        InlineKeyboardButton(text="❄️ Freeze Session", callback_data="session_freeze_select"),
        InlineKeyboardButton(text="☀️ Unfreeze Session", callback_data="session_unfreeze_select")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Back to Main Menu", callback_data="main_menu"))
    return builder.as_markup()

# --- FastAPI Configuration Menu ---
def get_fastapi_config_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📄 View .env", callback_data="config_view_env"))
    builder.row(InlineKeyboardButton(text="✏️ Edit .env Variable", callback_data="config_edit_env_var_name"))
    builder.row(InlineKeyboardButton(text="➕ Add .env Variable", callback_data="config_add_env_var_name"))
    builder.row(InlineKeyboardButton(text="⬅️ Back to Main Menu", callback_data="main_menu"))
    return builder.as_markup()

# --- Stats & Monitoring Menu ---
def get_stats_monitoring_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📈 FastAPI Status (/health)", callback_data="stats_fastapi_health"))
    builder.row(InlineKeyboardButton(text="💾 Session Stats Overview", callback_data="stats_session_overview"))
    builder.row(InlineKeyboardButton(text="📨 Webhook Tasks DB", callback_data="stats_webhook_db_export"))
    # builder.row(InlineKeyboardButton(text="⚙️ Server Resources", callback_data="stats_server_resources")) # Опционально
    builder.row(InlineKeyboardButton(text="⬅️ Back to Main Menu", callback_data="main_menu"))
    return builder.as_markup()

# --- Confirmation Keyboard ---
def get_confirmation_keyboard(action_prefix: str, item_id: Optional[str] = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    yes_cb = ConfirmationCallback(action_prefix, CONFIRM_YES, item_id)
    no_cb = ConfirmationCallback(action_prefix, CONFIRM_NO, item_id)
    
    builder.row(
        InlineKeyboardButton(text="✅ Confirm Yes", callback_data=yes_cb),
        InlineKeyboardButton(text="❌ Cancel No", callback_data=no_cb)
    )
    return builder.as_markup()

# --- Back to Menu Keyboard ---
def get_back_to_menu_keyboard(menu_callback: str = "main_menu", text: str = "⬅️ Back") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=text, callback_data=menu_callback))
    return builder.as_markup()

# --- Pagination Keyboard ---
def get_pagination_keyboard(action_prefix: str, current_page: int, total_pages: int,
                            back_menu_callback: Optional[str] = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    row_buttons = []
    if current_page > 0:
        row_buttons.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=PaginatorCallback(action_prefix, current_page - 1)))
    
    row_buttons.append(InlineKeyboardButton(text=f"📄 {current_page + 1}/{total_pages}", callback_data="noop")) # noop - ничего не делать

    if current_page < total_pages - 1:
        row_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=PaginatorCallback(action_prefix, current_page + 1)))
    
    if row_buttons: # Только если есть кнопки пагинации
        builder.row(*row_buttons)

    if back_menu_callback:
        builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data=back_menu_callback))
    return builder.as_markup()


# --- Keyboard for selecting an item from a list (e.g., session to delete/freeze) ---
def get_item_selection_keyboard(
    items: List[Dict[str, str]], # Список словарей, каждый с 'id' и 'text'
    action_prefix: str, # Префикс для callback_data при выборе элемента
    page: int,
    page_size: int,
    back_menu_callback: str
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    if not items:
        builder.row(InlineKeyboardButton(text="No items to display.", callback_data="noop"))
        builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data=back_menu_callback))
        return builder.as_markup()

    total_items = len(items)
    total_pages = (total_items + page_size - 1) // page_size
    
    start_index = page * page_size
    end_index = start_index + page_size
    current_page_items = items[start_index:end_index]

    for item in current_page_items:
        builder.row(InlineKeyboardButton(text=item['text'], callback_data=ActionWithIdCallback(action_prefix, item['id'])))
        
    # Pagination controls
    pagination_row = []
    if page > 0:
        pagination_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=PaginatorCallback(action_prefix + "_page", page - 1)))
    
    if total_pages > 1 : # Показываем номер страницы, только если их больше одной
         pagination_row.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop"))

    if page < total_pages - 1:
        pagination_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=PaginatorCallback(action_prefix + "_page", page + 1)))
    
    if pagination_row:
        builder.row(*pagination_row)
        
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data=back_menu_callback))
    return builder.as_markup()
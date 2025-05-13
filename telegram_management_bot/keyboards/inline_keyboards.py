# Inline keyboard generators

def start_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("Button 1", callback_data="button1"),
            InlineKeyboardButton("Button 2", callback_data="button2"),
        ],
        [
            InlineKeyboardButton("Button 3", callback_data="button3"),
            InlineKeyboardButton("Button 4", callback_data="button4"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("Admin Button 1", callback_data="admin_button1"),
            InlineKeyboardButton("Admin Button 2", callback_data="admin_button2"),
        ],
        [
            InlineKeyboardButton("Admin Button 3", callback_data="admin_button3"),
            InlineKeyboardButton("Admin Button 4", callback_data="admin_button4"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

# Add more keyboard functions here as needed
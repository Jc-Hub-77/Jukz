import telebot
from telebot import types
import json
import datetime
import os
import logging
from decimal import Decimal

from bot import bot, get_user_state, update_user_state, clear_user_state
from modules.auth_utils import is_admin
from modules.db_utils import (
    get_all_open_tickets_admin, get_ticket_details_by_id,
    add_message_to_ticket, update_ticket_status,
    update_admin_ticket_view_message_id,
    get_or_create_user, get_user_transaction_history
)
from modules import file_system_utils
from modules.message_utils import send_or_edit_message, delete_message
from modules.text_utils import escape_md
import config
from modules import db_utils
from handlers.utils import format_transaction_history_display, TX_HISTORY_PAGE_SIZE


logger = logging.getLogger(__name__)

TICKETS_PER_PAGE = 5
ITEMS_PER_PAGE_ADMIN = 5
USERS_PER_PAGE_ADMIN = 10


# --- Admin Support Ticket Management ---
def format_ticket_summary_for_list(ticket):
    messages = json.loads(ticket['messages_json']) if ticket['messages_json'] else []
    first_message_snippet = "No messages yet."
    if messages:
        first_message_obj = messages[0]
        text_snippet = escape_md(first_message_obj['text'][:40])
        if len(first_message_obj['text']) > 40: text_snippet += "..."
        first_message_snippet = f"_{text_snippet}_"

    last_active_dt = datetime.datetime.fromisoformat(ticket['last_message_at'])
    last_active_str = escape_md(last_active_dt.strftime("%Y-%m-%d %H:%M UTC"))
    status_escaped = escape_md(ticket['status'])

    return (
        f"Ticket ID: `{ticket['ticket_id']}` (User: `{ticket['user_id']}`)\n"
        f"Status: *{status_escaped}*\n"
        f"Last Update: _{last_active_str}_\n"
        f"Snippet: {first_message_snippet}"
    )

@bot.message_handler(commands=['tickets'], func=is_admin)
def handle_admin_list_tickets_command(message, page=1):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    logger.info(f"Admin {admin_id} listing tickets, page {page}.")

    open_tickets = get_all_open_tickets_admin()

    if not open_tickets:
        bot.send_message(chat_id, "No open support tickets.")
        return

    total_tickets = len(open_tickets)
    total_pages = (total_tickets + TICKETS_PER_PAGE - 1) // TICKETS_PER_PAGE
    page = max(1, min(page, total_pages))
    start_index = (page - 1) * TICKETS_PER_PAGE
    end_index = start_index + TICKETS_PER_PAGE
    tickets_to_display = open_tickets[start_index:end_index]

    old_pagination_msg_id = get_user_state(admin_id, 'admin_ticket_list_pagination_msg_id')
    if old_pagination_msg_id:
        try: delete_message(bot, chat_id, old_pagination_msg_id)
        except Exception: pass
        update_user_state(admin_id, 'admin_ticket_list_pagination_msg_id', None)

    if hasattr(message, 'text') and message.text and message.text.startswith('/tickets'):
        try: delete_message(bot, chat_id, message.message_id)
        except Exception: pass


    header_text = f"📋 *Open Support Tickets (Page {page}/{total_pages}):*\n"
    bot.send_message(chat_id, header_text, parse_mode="MarkdownV2")


    if not tickets_to_display and page > 1:
        bot.send_message(chat_id, "No tickets on this page. This is unexpected.")

    for ticket in tickets_to_display:
        summary = format_ticket_summary_for_list(ticket)
        ticket_markup = types.InlineKeyboardMarkup(row_width=2)
        ticket_markup.add(
            types.InlineKeyboardButton(f"👁️ View/Reply #{ticket['ticket_id']}", callback_data=f"admin_view_ticket_{ticket['ticket_id']}"),
            types.InlineKeyboardButton(f"❌ Close #{ticket['ticket_id']}", callback_data=f"admin_close_ticket_{ticket['ticket_id']}")
        )
        bot.send_message(chat_id, summary + "\n--------------------", reply_markup=ticket_markup, parse_mode="MarkdownV2")

    if total_pages > 1:
        pagination_markup = types.InlineKeyboardMarkup(row_width=3)
        nav_buttons = []
        if page > 1: nav_buttons.append(types.InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_list_tickets_page_{page-1}"))
        nav_buttons.append(types.InlineKeyboardButton(f"🔄 Pg {page}/{total_pages}", callback_data=f"admin_list_tickets_page_{page}"))
        if page < total_pages: nav_buttons.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"admin_list_tickets_page_{page+1}"))

        if nav_buttons:
            pagination_markup.add(*nav_buttons)
            pg_msg = bot.send_message(chat_id, "Ticket List Navigation:", reply_markup=pagination_markup)
            update_user_state(admin_id, 'admin_ticket_list_pagination_msg_id', pg_msg.message_id)

    update_user_state(admin_id, 'admin_ticket_current_page', page)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_list_tickets_page_') and is_admin(call))
def handle_admin_list_tickets_page_callback(call):
    try: page = int(call.data.split('admin_list_tickets_page_')[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid page.", show_alert=True); return

    try: delete_message(bot, call.message.chat.id, call.message.message_id)
    except: pass

    mock_message = telebot.types.Message(
        message_id=0,
        from_user=call.from_user,
        date=datetime.datetime.now().timestamp(),
        chat=call.message.chat,
        content_type='text',
        options={},
        json_string=""
    )
    mock_message.text = None

    handle_admin_list_tickets_command(mock_message, page=page)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_view_ticket_') and is_admin(call))
def handle_admin_view_ticket_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    try: ticket_id = int(call.data.split('admin_view_ticket_')[1])
    except (IndexError, ValueError): bot.answer_callback_query(call.id, "Bad Ticket ID.", show_alert=True); return

    ticket = db_utils.get_ticket_details_by_id(ticket_id)
    if not ticket:
        bot.answer_callback_query(call.id, f"Ticket #{ticket_id} not found."); return

    update_user_state(admin_id, 'admin_current_ticket_id', ticket_id)
    update_user_state(admin_id, 'admin_flow', 'viewing_ticket')


    messages_list = json.loads(ticket['messages_json']) if ticket['messages_json'] else []
    conversation_history = [f"📜 *Conversation for Ticket \\#{ticket_id}* (User ID: `{ticket['user_id']}`)"]
    conversation_history.append(f"Status: *{escape_md(ticket['status'].replace('_', ' ').title())}*")

    for msg_data in messages_list:
        sender = escape_md(msg_data.get('sender', 'System').title())
        text = escape_md(msg_data.get('text', ''))
        ts_str = "Unknown time"
        try:
            ts_dt = datetime.datetime.fromisoformat(msg_data.get('timestamp'))
            ts_str = escape_md(ts_dt.strftime('%Y-%m-%d %H:%M:%S UTC'))
        except: pass
        conversation_history.append(f"\n*{sender}* ({ts_str}):\n{text}")

    full_conversation_text = "\n".join(conversation_history)
    if len(full_conversation_text) > 4000: full_conversation_text = full_conversation_text[:4000] + "\n\\.\\.\\. (truncated)"

    markup = types.InlineKeyboardMarkup(row_width=1)
    if ticket['status'] == 'open':
        markup.add(types.InlineKeyboardButton(f"✍️ Reply to Ticket #{ticket_id}", callback_data=f"admin_reply_ticket_{ticket_id}"))
        markup.add(types.InlineKeyboardButton(f"❌ Close Ticket #{ticket_id}", callback_data=f"admin_close_ticket_{ticket_id}")
    )
    markup.add(types.InlineKeyboardButton("⬅️ Back to Tickets List", callback_data="admin_list_tickets_cmd_from_view"))

    try: delete_message(bot, chat_id, call.message.message_id)
    except: pass

    sent_msg = bot.send_message(chat_id, full_conversation_text, reply_markup=markup, parse_mode="MarkdownV2")
    update_user_state(admin_id, 'admin_ticket_view_msg_id', sent_msg.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'admin_list_tickets_cmd_from_view' and is_admin(call))
def handle_admin_list_tickets_cmd_from_view_callback(call):
    admin_id = call.from_user.id
    current_page = get_user_state(admin_id, 'admin_ticket_current_page', 1)
    try: delete_message(bot, call.message.chat.id, call.message.message_id)
    except: pass

    mock_message = telebot.types.Message(0,call.from_user,datetime.datetime.now().timestamp(),call.message.chat,'text',{},"")
    mock_message.text = None
    handle_admin_list_tickets_command(mock_message, page=current_page)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_reply_ticket_') and is_admin(call))
def handle_admin_initiate_reply_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    try: ticket_id = int(call.data.split('admin_reply_ticket_')[1])
    except (IndexError, ValueError): bot.answer_callback_query(call.id, "Bad Ticket ID.", show_alert=True); return

    ticket = db_utils.get_ticket_details_by_id(ticket_id)
    if not ticket or ticket['status'] != 'open':
        bot.answer_callback_query(call.id, f"Ticket #{ticket_id} not found or not open."); return

    update_user_state(admin_id, 'admin_replying_to_ticket_id', ticket_id)
    update_user_state(admin_id, 'admin_replying_to_user_id', ticket['user_id'])
    update_user_state(admin_id, 'admin_flow', 'awaiting_admin_reply_text')

    ticket_view_msg_id = get_user_state(admin_id, 'admin_ticket_view_msg_id')
    if ticket_view_msg_id:
        try: delete_message(bot, chat_id, ticket_view_msg_id)
        except: pass
        update_user_state(admin_id, 'admin_ticket_view_msg_id', None)
    elif call.message.message_id:
        try: delete_message(bot, chat_id, call.message.message_id)
        except: pass


    reply_prompt_text = f"✍️ Replying to Ticket \\#{ticket_id} \\(User ID: `{ticket['user_id']}`\\)\\.\nSend your reply message now\\. Type /cancel\\_admin\\_action to abort\\."
    pm = bot.send_message(chat_id, reply_prompt_text, parse_mode="MarkdownV2", reply_markup=types.ForceReply(selective=True))
    update_user_state(admin_id, 'admin_reply_prompt_msg_id', pm.message_id)
    bot.answer_callback_query(call.id, f"Ready for reply to Ticket #{ticket_id}")


@bot.message_handler(func=lambda message: str(message.from_user.id) == str(config.ADMIN_ID) and \
                       get_user_state(message.from_user.id, 'admin_flow') == 'awaiting_admin_reply_text',
                     content_types=['text', 'photo'])
def handle_admin_ticket_reply_message_content(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    ticket_id = get_user_state(admin_id, 'admin_replying_to_ticket_id')
    target_user_id = get_user_state(admin_id, 'admin_replying_to_user_id')

    prompt_id = get_user_state(admin_id, 'admin_reply_prompt_msg_id')
    if prompt_id:
        try: delete_message(bot, chat_id, prompt_id)
        except: pass
        update_user_state(admin_id, 'admin_reply_prompt_msg_id', None)

    if message.text and message.text.lower() == '/cancel_admin_action':
        try: delete_message(bot, chat_id, message.message_id)
        except: pass
        clear_user_state(admin_id)
        bot.send_message(chat_id, "Reply cancelled.")
        return

    if not ticket_id or not target_user_id:
        bot.reply_to(message, "Error: No ticket context for reply. Please start over."); return

    admin_reply_text = message.text or message.caption or ""
    photo_file_id = message.photo[-1].file_id if message.photo else None
    message_content_for_db = admin_reply_text
    if photo_file_id: message_content_for_db += f" [Admin Image Attached: {photo_file_id}]"

    if not message_content_for_db.strip():
        bot.reply_to(message, "Reply seems empty. Please try again or /cancel_admin_action."); return

    add_success = db_utils.add_message_to_ticket(ticket_id, 'admin', message_content_for_db, admin_tg_message_id=message.message_id)
    if add_success:
        bot.send_message(admin_id, f"✅ Reply sent for Ticket \\#{ticket_id}\\.", parse_mode="MarkdownV2")
        user_notification_text = f"💬 Admin has replied to your Ticket \\#`{ticket_id}`:\n\n{escape_md(admin_reply_text)}"
        try:
            if photo_file_id: bot.send_photo(target_user_id, photo_file_id, caption=user_notification_text, parse_mode="MarkdownV2")
            else: bot.send_message(target_user_id, user_notification_text, parse_mode="MarkdownV2")
        except Exception as e_user_notify:
            logger.error(f"Failed to send admin reply for ticket {ticket_id} to user {target_user_id}: {e_user_notify}")
            bot.send_message(admin_id, f"⚠️ Failed to deliver your reply to user {target_user_id} for Ticket \\#{ticket_id}\\. Error: {escape_md(str(e_user_notify))}", parse_mode="MarkdownV2")
    else:
        bot.send_message(admin_id, f"⚠️ Error saving reply for Ticket \\#{ticket_id}\\.", parse_mode="MarkdownV2")

    try: delete_message(bot, chat_id, message.message_id)
    except: pass
    clear_user_state(admin_id)

@bot.message_handler(commands=['cancel_admin_action'], func=is_admin)
def handle_general_cancel_admin_action(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id

    prompt_ids_keys = [
        'admin_reply_prompt_msg_id', 'add_item_prompt_msg_id',
        'add_item_confirm_msg_id', 'admin_ticket_view_msg_id',
        'admin_edit_item_prompt_msg_id', 'admin_edit_item_generic_prompt_id',
        'admin_edit_item_menu_msg_id', 'admin_delete_item_confirm_msg_id',
        'admin_adjust_balance_prompt_msg_id', 'admin_adjust_balance_reason_prompt_msg_id',
        'admin_adjust_balance_confirm_msg_id', 'admin_view_user_details_msg_id'
    ]
    for key in prompt_ids_keys:
        prompt_id = get_user_state(admin_id, key)
        if prompt_id:
            try: delete_message(bot, chat_id, prompt_id)
            except: pass

    clear_user_state(admin_id)
    bot.send_message(chat_id, "Your current admin action has been cancelled.")
    try: delete_message(bot, chat_id, message.message_id)
    except: pass


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_close_ticket_') and is_admin(call))
def handle_admin_close_ticket_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    try: ticket_id = int(call.data.split('admin_close_ticket_')[1])
    except (IndexError, ValueError): bot.answer_callback_query(call.id, "Bad Ticket ID.", show_alert=True); return

    ticket = db_utils.get_ticket_details_by_id(ticket_id)
    if not ticket: bot.answer_callback_query(call.id, f"Ticket #{ticket_id} not found."); return

    if ticket['status'] != 'open':
        bot.answer_callback_query(call.id, f"Ticket #{ticket_id} is already {ticket['status']}.")
        ticket_view_msg_id = get_user_state(admin_id, 'admin_ticket_view_msg_id')
        if ticket_view_msg_id == call.message.message_id:
            closed_text = f"Ticket \\#`{ticket_id}` is already *{escape_md(ticket['status'].replace('_', ' '))}*\\."
            markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Tickets List", callback_data="admin_list_tickets_cmd_from_view"))
            send_or_edit_message(bot, chat_id, closed_text, existing_message_id=ticket_view_msg_id, reply_markup=markup, parse_mode="MarkdownV2")
        return

    updated = db_utils.update_ticket_status(ticket_id, 'closed_by_admin')
    if updated:
        bot.answer_callback_query(call.id, f"Ticket #{ticket_id} closed.")

        ticket_view_msg_id = get_user_state(admin_id, 'admin_ticket_view_msg_id')
        closed_text = f"Ticket \\#`{ticket_id}` is now *closed\\_by\\_admin*\\."
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Tickets List", callback_data="admin_list_tickets_cmd_from_view"))

        if ticket_view_msg_id and str(get_user_state(admin_id, 'admin_current_ticket_id')) == str(ticket_id) and call.message.message_id == ticket_view_msg_id:
             send_or_edit_message(bot, chat_id, closed_text, existing_message_id=ticket_view_msg_id, reply_markup=markup, parse_mode="MarkdownV2")
        else:
            try: delete_message(bot, chat_id, call.message.message_id)
            except: pass
            bot.send_message(chat_id, closed_text, parse_mode="MarkdownV2")


        try: bot.send_message(ticket['user_id'], f"ℹ️ Your Support Ticket \\#`{ticket_id}` has been closed by an administrator\\.", parse_mode="MarkdownV2")
        except Exception as e_notify: logger.error(f"Failed to notify user {ticket['user_id']} of ticket {ticket_id} closure: {e_notify}")
    else:
        bot.answer_callback_query(call.id, f"Error closing ticket \\#{ticket_id}\\.", show_alert=True)

    if str(get_user_state(admin_id, 'admin_current_ticket_id')) == str(ticket_id):
        update_user_state(admin_id, 'admin_current_ticket_id', None)
        update_user_state(admin_id, 'admin_flow', None)


# --- Admin Item Management ---
@bot.message_handler(commands=['additem'], func=is_admin)
def command_add_item_start(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    logger.info(f"Admin {admin_id} initiated /additem command.")

    try: delete_message(bot, chat_id, message.message_id)
    except: pass

    clear_user_state(admin_id)
    update_user_state(admin_id, 'admin_flow', 'admin_adding_item_city')
    update_user_state(admin_id, 'add_item_data', {'images': []})

    prompt_msg = bot.send_message(chat_id,
                     "Okay, let's add a new item type\\.\n"
                     "🏙️ First, what **city** is this item for?\n"
                     "Type /cancel\\_admin\\_action to abort at any time\\.",
                     parse_mode="MarkdownV2",
                     reply_markup=types.ForceReply(selective=True))
    update_user_state(admin_id, 'add_item_prompt_msg_id', prompt_msg.message_id)

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id, 'admin_flow') == 'admin_adding_item_city' and is_admin(message), content_types=['text'])
def process_add_item_city(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    city_name = message.text.strip()

    prompt_msg_id = get_user_state(admin_id, 'add_item_prompt_msg_id')
    if prompt_msg_id:
        try: delete_message(bot, chat_id, prompt_msg_id)
        except Exception: pass
        update_user_state(admin_id, 'add_item_prompt_msg_id', None)
    try: delete_message(bot, chat_id, message.message_id)
    except: pass

    if not city_name or len(city_name) > 50 or not all(part.isalnum() or part == '' for part in city_name.split(' ')):
        new_prompt = bot.send_message(chat_id, "Invalid city name. Please use 1-50 alphanumeric characters (spaces allowed, but not special symbols). Try again:", reply_markup=types.ForceReply(selective=True))
        update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)
        return

    add_item_data = get_user_state(admin_id, 'add_item_data', {'images': []})
    add_item_data['city'] = city_name
    update_user_state(admin_id, 'add_item_data', add_item_data)
    update_user_state(admin_id, 'admin_flow', 'admin_adding_item_name')

    new_prompt = bot.send_message(chat_id,
                     f"👍 City: *{escape_md(city_name)}*\n\n"
                     "🏷️ Next, what is the **name** of this item type \\(e\\.g\\., `PizzaMargherita`\\)? "
                     "This will also be used for the folder name, so keep it filesystem\\-friendly \\(alphanumeric, no spaces, underscores allowed\\)\\.",
                     parse_mode="MarkdownV2",
                     reply_markup=types.ForceReply(selective=True))
    update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id, 'admin_flow') == 'admin_adding_item_name' and is_admin(message), content_types=['text'])
def process_add_item_name(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    item_name = message.text.strip()

    prompt_msg_id = get_user_state(admin_id, 'add_item_prompt_msg_id')
    if prompt_msg_id:
        try: delete_message(bot, chat_id, prompt_msg_id)
        except Exception: pass
        update_user_state(admin_id, 'add_item_prompt_msg_id', None)
    try: delete_message(bot, chat_id, message.message_id)
    except: pass

    if not item_name or len(item_name) > 70 or not item_name.replace('_','').isalnum():
        new_prompt = bot.send_message(chat_id, "Invalid item name. Use 1-70 alphanumeric characters or underscores (no spaces). Try again:", reply_markup=types.ForceReply(selective=True))
        update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)
        return

    add_item_data = get_user_state(admin_id, 'add_item_data')
    potential_path = os.path.join(config.ITEMS_BASE_DIR, add_item_data['city'], item_name)
    if os.path.exists(potential_path):
        new_prompt = bot.send_message(chat_id, f"An item type folder named '{escape_md(item_name)}' already exists in '{escape_md(add_item_data['city'])}'. Please choose a different name:", parse_mode="MarkdownV2", reply_markup=types.ForceReply(selective=True))
        update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)
        return

    add_item_data['name'] = item_name
    update_user_state(admin_id, 'add_item_data', add_item_data)
    update_user_state(admin_id, 'admin_flow', 'admin_adding_item_description')

    new_prompt = bot.send_message(chat_id,
                     f"🏷️ Item Type Name: *{escape_md(item_name)}*\n\n"
                     "📝 Now, provide the **description** for this item type\\.",
                     parse_mode="MarkdownV2",
                     reply_markup=types.ForceReply(selective=True))
    update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id, 'admin_flow') == 'admin_adding_item_description' and is_admin(message), content_types=['text'])
def process_add_item_description(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    description = message.text.strip()

    prompt_msg_id = get_user_state(admin_id, 'add_item_prompt_msg_id')
    if prompt_msg_id:
        try: delete_message(bot, chat_id, prompt_msg_id)
        except Exception: pass
        update_user_state(admin_id, 'add_item_prompt_msg_id', None)
    try: delete_message(bot, chat_id, message.message_id)
    except: pass

    if not description or len(description) < 5:
        new_prompt = bot.send_message(chat_id, "Description too short. Please provide a meaningful description (at least 5 characters).", reply_markup=types.ForceReply(selective=True))
        update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)
        return

    add_item_data = get_user_state(admin_id, 'add_item_data')
    add_item_data['description'] = description
    update_user_state(admin_id, 'add_item_data', add_item_data)
    update_user_state(admin_id, 'admin_flow', 'admin_adding_item_price')

    desc_preview = escape_md(description[:100] + ('...' if len(description) > 100 else ''))
    new_prompt = bot.send_message(chat_id,
                     f"📝 Description Set: \"_{desc_preview}_\"\n\n"
                     "💶 What is the **price in EUR** for one unit of this item \\(e\\.g\\., `12.99`\\)?",
                     parse_mode="MarkdownV2",
                     reply_markup=types.ForceReply(selective=True))
    update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id, 'admin_flow') == 'admin_adding_item_price' and is_admin(message), content_types=['text'])
def process_add_item_price(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    price_str = message.text.strip().replace(',', '.')

    prompt_msg_id = get_user_state(admin_id, 'add_item_prompt_msg_id')
    if prompt_msg_id:
        try: delete_message(bot, chat_id, prompt_msg_id)
        except Exception: pass
        update_user_state(admin_id, 'add_item_prompt_msg_id', None)
    try: delete_message(bot, chat_id, message.message_id)
    except: pass

    try:
        price = float(price_str)
        if price < 0:
            raise ValueError("Price must be non-negative.")
    except ValueError:
        new_prompt = bot.send_message(chat_id, "Invalid price format. Please enter a non-negative number (e.g., 12.99 or 0).", reply_markup=types.ForceReply(selective=True))
        update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)
        return

    add_item_data = get_user_state(admin_id, 'add_item_data')
    add_item_data['price'] = price
    update_user_state(admin_id, 'add_item_data', add_item_data)
    update_user_state(admin_id, 'admin_flow', 'admin_adding_item_images')

    new_prompt = bot.send_message(chat_id,
                     f"💶 Price: *{price:.2f} EUR*\n\n"
                     "🖼️ Now, send up to 3 **images** for this item type, one by one\\. "
                     "Send a photo, then wait for confirmation before sending the next\\. "
                     "When you are done adding images \\(or if you don't want to add any\\), type /doneimages\\.",
                     parse_mode="MarkdownV2")
    update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)

@bot.message_handler(
    func=lambda message: get_user_state(message.from_user.id, 'admin_flow') == 'admin_adding_item_images' and is_admin(message),
    content_types=['photo', 'text']
)
def process_add_item_images_or_done(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    add_item_data = get_user_state(admin_id, 'add_item_data')

    prompt_msg_id = get_user_state(admin_id, 'add_item_prompt_msg_id')

    if message.text and message.text.lower() == '/doneimages':
        if prompt_msg_id:
            try: delete_message(bot, chat_id, prompt_msg_id)
            except Exception: pass
            update_user_state(admin_id, 'add_item_prompt_msg_id', None)
        try: delete_message(bot, chat_id, message.message_id)
        except: pass
        update_user_state(admin_id, 'admin_flow', 'admin_adding_item_confirm')
        prompt_confirmation_add_item(admin_id, chat_id)
        return

    if message.photo:
        if prompt_msg_id:
            try: delete_message(bot, chat_id, prompt_msg_id)
            except Exception: pass
            update_user_state(admin_id, 'add_item_prompt_msg_id', None)
        try: delete_message(bot, chat_id, message.message_id)
        except: pass

        if len(add_item_data.get('images', [])) < 3:
            photo_file = bot.get_file(message.photo[-1].file_id)
            downloaded_file_bytes = bot.download_file(photo_file.file_path)

            original_filename = f"image_{len(add_item_data.get('images', [])) + 1 }.jpg"
            if hasattr(photo_file, 'file_path') and photo_file.file_path:
                 fname_from_path = os.path.basename(photo_file.file_path)
                 if fname_from_path: original_filename = fname_from_path

            add_item_data.setdefault('images', []).append({'file_bytes': downloaded_file_bytes, 'filename': original_filename})
            update_user_state(admin_id, 'add_item_data', add_item_data)

            new_prompt_text = f"Image {len(add_item_data['images'])}/3 received. Send another, or type /doneimages."
            if len(add_item_data['images']) == 3:
                new_prompt_text = "Maximum 3 images received. Type /doneimages to continue."
            new_prompt = bot.send_message(chat_id, new_prompt_text)
            update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)
        else:
            new_prompt = bot.send_message(chat_id, "You've already added the maximum of 3 images. Type /doneimages to continue.")
            update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)
        return

    if message.text:
        if prompt_msg_id:
            try: delete_message(bot, chat_id, prompt_msg_id)
            except Exception: pass
            update_user_state(admin_id, 'add_item_prompt_msg_id', None)
        try: delete_message(bot, chat_id, message.message_id)
        except: pass
        new_prompt = bot.send_message(chat_id, "Invalid input. Please send a photo or type /doneimages to finish adding images.")
        update_user_state(admin_id, 'add_item_prompt_msg_id', new_prompt.message_id)

def prompt_confirmation_add_item(admin_id, chat_id):
    add_item_data = get_user_state(admin_id, 'add_item_data')
    if not add_item_data:
        bot.send_message(chat_id, "Error: Item data missing for confirmation.")
        clear_user_state(admin_id)
        return

    summary_text = "*Please confirm item details:*\n\n"
    summary_text += f"🏙️ City: *{escape_md(add_item_data['city'])}*\n"
    summary_text += f"🏷️ Name: *{escape_md(add_item_data['name'])}*\n"
    desc_preview = escape_md(add_item_data['description'][:200] + ('...' if len(add_item_data['description']) > 200 else ''))
    summary_text += f"📝 Description: \"_{desc_preview}_\"\n"
    summary_text += f"💶 Price: *{add_item_data['price']:.2f} EUR*\n"
    summary_text += f"🖼️ Images: *{len(add_item_data.get('images', []))} image(s)*\n\n"
    summary_text += "Create this item type and its first instance (`instance_01`)?"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("✅ Yes, Create Item", callback_data="admin_confirm_add_item_yes"))
    markup.add(types.InlineKeyboardButton("❌ No, Cancel", callback_data="admin_confirm_add_item_no"))

    confirm_msg = bot.send_message(chat_id, summary_text, reply_markup=markup, parse_mode="MarkdownV2")
    update_user_state(admin_id, 'add_item_confirm_msg_id', confirm_msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_confirm_add_item_') and is_admin(call))
def handle_add_item_confirmation_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id

    confirm_msg_id = get_user_state(admin_id, 'add_item_confirm_msg_id')
    if confirm_msg_id:
        try: delete_message(bot, chat_id, confirm_msg_id)
        except Exception: pass
        update_user_state(admin_id, 'add_item_confirm_msg_id', None)

    bot.answer_callback_query(call.id)

    if call.data == 'admin_confirm_add_item_yes':
        add_item_data = get_user_state(admin_id, 'add_item_data')
        if not add_item_data:
            bot.send_message(chat_id, "Error: Item data not found. Please start over using /additem.")
            clear_user_state(admin_id); return

        city = add_item_data['city']
        name = add_item_data['name']
        description = add_item_data['description']
        price = add_item_data['price']
        images_data = add_item_data.get('images', [])
        initial_instance_name = "instance_01"

        fs_image_files = [(img_dict['file_bytes'], img_dict['filename']) for img_dict in images_data]

        fs_success, fs_message, product_fs_path = file_system_utils.create_product_type_with_instance(
            city, name, initial_instance_name, description, fs_image_files
        )

        if fs_success and product_fs_path:
            product_id = db_utils.add_product_type(
                city=city,
                name=name,
                price=price,
                folder_path=product_fs_path,
                description=description,
                image_paths_json=json.dumps([img['filename'] for img in images_data]) if images_data else None,
                initial_quantity=1
            )
            if product_id:
                bot.send_message(chat_id, f"✅ Item type *{escape_md(name)}* in *{escape_md(city)}* added successfully with price {price:.2f} EUR! Product ID: `{product_id}`", parse_mode="MarkdownV2")
            else:
                bot.send_message(chat_id, f"⚠️ Item created on filesystem, but failed to add to database: {escape_md(fs_message)}. Please check logs and sync manually if needed.")
        else:
            bot.send_message(chat_id, f"⚠️ Error creating item type on filesystem: {escape_md(fs_message)}")

    elif call.data == 'admin_confirm_add_item_no':
        bot.send_message(chat_id, "Item creation cancelled.")

    clear_user_state(admin_id)

# --- Edit Item Flow ---
@bot.message_handler(commands=['edititem'], func=is_admin)
def command_edit_item_list(message, page=0):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    logger.info(f"Admin {admin_id} initiated /edititem, page {page}.")

    if hasattr(message, 'text') and message.text and message.text.startswith('/edititem'):
        try: delete_message(bot, chat_id, message.message_id)
        except: pass

    if hasattr(message, 'message') and message.message:
         current_list_msg_id = get_user_state(admin_id, 'admin_item_list_main_msg_id')
         if current_list_msg_id and current_list_msg_id == message.message.message_id:
             pass
         elif message.message.message_id:
             try: delete_message(bot, chat_id, message.message.message_id)
             except: pass

    update_user_state(admin_id, 'admin_flow', 'editing_item_list')

    products = db_utils.get_all_products_admin()
    if not products:
        old_list_msg_id = get_user_state(admin_id, 'admin_item_list_main_msg_id')
        if old_list_msg_id:
            try: delete_message(bot, chat_id, old_list_msg_id)
            except:pass
            update_user_state(admin_id, 'admin_item_list_main_msg_id', None)
        bot.send_message(chat_id, "No items found in the database to edit.")
        clear_user_state(admin_id)
        return

    total_items = len(products)
    total_pages = (total_items + ITEMS_PER_PAGE_ADMIN - 1) // ITEMS_PER_PAGE_ADMIN
    page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0

    start_index = page * ITEMS_PER_PAGE_ADMIN
    end_index = start_index + ITEMS_PER_PAGE_ADMIN
    items_to_display = products[start_index:end_index]

    response_text = f"🛠️ *Edit Items (Page {page + 1}/{total_pages if total_pages > 0 else 1})*\nSelect an item to manage:\n\n"
    if not items_to_display :
        response_text += "No items on this page." if page > 0 else "No items available."

    item_buttons = []
    for item in items_to_display:
        item_id = item['product_id']
        item_name_escaped = escape_md(item['name'])
        availability_icon = "✅" if item['is_available'] else "🅾️"

        item_button_text = f"{availability_icon} {item_name_escaped} ({escape_md(item['city'])}) - {item['price']:.2f} EUR"
        item_buttons.append(types.InlineKeyboardButton(item_button_text, callback_data=f"admin_edit_item_select_{item_id}"))

    markup = types.InlineKeyboardMarkup(row_width=1)
    for btn in item_buttons: markup.add(btn)

    nav_buttons_row = []
    if page > 0:
        nav_buttons_row.append(types.InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_edit_item_page_{page - 1}"))
    if total_pages > 1 :
        nav_buttons_row.append(types.InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data=f"admin_edit_item_page_{page}"))
    if (page + 1) < total_pages:
        nav_buttons_row.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"admin_edit_item_page_{page + 1}"))

    if nav_buttons_row:
        markup.row(*nav_buttons_row)

    markup.add(types.InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu"))

    existing_list_msg_id = get_user_state(admin_id, 'admin_item_list_main_msg_id')
    sent_list_msg = send_or_edit_message(bot, chat_id, response_text,
                                         reply_markup=markup,
                                         existing_message_id=existing_list_msg_id,
                                         parse_mode="MarkdownV2",
                                         disable_web_page_preview=True)

    if sent_list_msg:
        update_user_state(admin_id, 'admin_item_list_main_msg_id', sent_list_msg.message_id)
    update_user_state(admin_id, 'admin_item_list_current_page', page)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_edit_item_page_') and is_admin(call))
def handle_edit_item_page_callback(call):
    admin_id = call.from_user.id
    try:
        page = int(call.data.split('_')[-1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid page number.", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    command_edit_item_list(call, page=page)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_toggle_item_avail_') and is_admin(call))
def handle_admin_toggle_availability_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    try:
        product_id = int(call.data.split('admin_toggle_item_avail_')[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid product ID.", show_alert=True)
        return

    product = db_utils.get_product_details_by_id(product_id)
    if not product:
        bot.answer_callback_query(call.id, "Product not found.", show_alert=True)
        return

    new_availability = not product['is_available']
    update_success = db_utils.update_product_availability(product_id, new_availability)

    if update_success:
        bot.answer_callback_query(call.id, f"Availability updated to: {'Available' if new_availability else 'Unavailable'}")

        if get_user_state(admin_id, 'admin_flow') == 'admin_editing_item_menu' and \
           get_user_state(admin_id, 'admin_editing_product_id') == product_id:

            edit_menu_msg_id = get_user_state(admin_id, 'admin_edit_item_menu_msg_id')
            if edit_menu_msg_id:
                try: delete_message(bot, chat_id, edit_menu_msg_id)
                except: pass

            mock_call_data = f"admin_edit_item_select_{product_id}"
            mock_call_obj = types.CallbackQuery(id=f"cb_{datetime.datetime.now().timestamp()}", from_user=call.from_user, data=mock_call_data, chat_instance=call.chat_instance, json_string="", message=None)
            handle_admin_edit_item_select_callback(mock_call_obj)

        else:
            current_page = get_user_state(admin_id, 'admin_item_list_current_page', 0)
            command_edit_item_list(call, page=current_page)
    else:
        bot.answer_callback_query(call.id, "Failed to update availability.", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_edit_item_select_') and is_admin(call))
def handle_admin_edit_item_select_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    try:
        product_id = int(call.data.split('admin_edit_item_select_')[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid product ID.", show_alert=True)
        return

    product = db_utils.get_product_details_by_id(product_id)
    if not product:
        bot.answer_callback_query(call.id, "Product not found.", show_alert=True)
        return

    update_user_state(admin_id, 'admin_editing_product_id', product_id)
    update_user_state(admin_id, 'admin_flow', 'admin_editing_item_menu')

    main_list_msg_id = get_user_state(admin_id, 'admin_item_list_main_msg_id')
    if main_list_msg_id:
        if call.message and main_list_msg_id == call.message.message_id:
             try: delete_message(bot, chat_id, main_list_msg_id)
             except: pass
        update_user_state(admin_id, 'admin_item_list_main_msg_id', None)


    availability_icon = "✅" if product['is_available'] else "🅾️"
    menu_text = (f"🛠️ Editing Item: *{escape_md(product['name'])}* \\(ID: `{product_id}`\\)\n"
                 f"City: *{escape_md(product['city'])}*\n"
                 f"Price: *{product['price']:.2f} EUR*\n"
                 f"Available: *{'Yes' if product['is_available'] else 'No'}*\n\n"
                 f"What would you like to edit?")

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Name 🏷️", callback_data=f"admin_edit_attr_name_{product_id}"),
        types.InlineKeyboardButton("Price 💶", callback_data=f"admin_edit_attr_price_{product_id}")
    )
    markup.add(
        types.InlineKeyboardButton("City 🏙️", callback_data=f"admin_edit_attr_city_{product_id}"),
        types.InlineKeyboardButton(f"Toggle Avail. {availability_icon}", callback_data=f"admin_toggle_item_avail_{product_id}")
    )
    markup.add(types.InlineKeyboardButton("⬅️ Back to Item List", callback_data="admin_list_items_cmd_from_edit"))

    edit_menu_msg = bot.send_message(chat_id, menu_text, reply_markup=markup, parse_mode="MarkdownV2")
    update_user_state(admin_id, 'admin_edit_item_menu_msg_id', edit_menu_msg.message_id)
    if hasattr(call, 'id') and not str(call.id).startswith("mockcall"):
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == 'admin_list_items_cmd_from_edit' and is_admin(call))
def handle_admin_list_items_cmd_from_edit_callback(call):
    admin_id = call.from_user.id
    current_page = get_user_state(admin_id, 'admin_item_list_current_page', 0)

    edit_menu_msg_id = get_user_state(admin_id, 'admin_edit_item_menu_msg_id')
    if edit_menu_msg_id:
        try: delete_message(bot, call.message.chat.id, edit_menu_msg_id)
        except: pass
        update_user_state(admin_id, 'admin_edit_item_menu_msg_id', None)

    mock_message = telebot.types.Message(0,call.from_user,datetime.datetime.now().timestamp(),call.message.chat,'text',{},"")
    mock_message.text = None
    command_edit_item_list(mock_message, page=current_page)
    bot.answer_callback_query(call.id)

# --- Generic Attribute Edit ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_edit_attr_') and is_admin(call))
def handle_admin_edit_item_attr_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id

    try:
        parts = call.data.split('_')
        action = parts[3]
        product_id = int(parts[4])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid edit action.", show_alert=True)
        return

    product = db_utils.get_product_details_by_id(product_id)
    if not product:
        bot.answer_callback_query(call.id, "Product not found.", show_alert=True); return

    update_user_state(admin_id, 'admin_editing_product_id', product_id)
    update_user_state(admin_id, 'admin_editing_attribute', action)
    update_user_state(admin_id, 'admin_flow', f'admin_awaiting_edit_{action}')

    current_value_escaped = ""
    if action == 'name': current_value_escaped = escape_md(product['name'])
    elif action == 'price': current_value_escaped = f"{product['price']:.2f}"
    elif action == 'city': current_value_escaped = escape_md(product['city'])

    prompt_text = f"Editing *{escape_md(action.title())}* for item *{escape_md(product['name'])}* \\(ID: `{product_id}`\\)\\.\n"
    prompt_text += f"Current value: *{current_value_escaped}*\n\n"
    prompt_text += f"Please enter the new {escape_md(action.lower())}\\. Type /cancel\\_admin\\_action to abort\\."

    edit_menu_msg_id = get_user_state(admin_id, 'admin_edit_item_menu_msg_id')
    if edit_menu_msg_id:
        try: delete_message(bot, chat_id, edit_menu_msg_id)
        except: pass
        update_user_state(admin_id, 'admin_edit_item_menu_msg_id', None)

    prompt_msg = bot.send_message(chat_id, prompt_text, parse_mode="MarkdownV2", reply_markup=types.ForceReply(selective=True))
    update_user_state(admin_id, 'admin_edit_item_generic_prompt_id', prompt_msg.message_id)
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda msg: str(msg.from_user.id) == str(config.ADMIN_ID) and \
                                   get_user_state(msg.from_user.id, 'admin_flow', '').startswith('admin_awaiting_edit_'),
                     content_types=['text'])
def handle_admin_edit_item_attr_input(message):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    new_value_str = message.text.strip()

    product_id = get_user_state(admin_id, 'admin_editing_product_id')
    action = get_user_state(admin_id, 'admin_editing_attribute')

    prompt_msg_id = get_user_state(admin_id, 'admin_edit_item_generic_prompt_id')
    if prompt_msg_id:
        try: delete_message(bot, chat_id, prompt_msg_id)
        except: pass
    try: delete_message(bot, chat_id, message.message_id)
    except: pass

    if not product_id or not action:
        bot.send_message(chat_id, "Error: Editing context lost. Please start over.")
        clear_user_state(admin_id); return

    product_before_edit = db_utils.get_product_details_by_id(product_id)
    if not product_before_edit:
        bot.send_message(chat_id, "Error: Product details lost. Please start over.")
        clear_user_state(admin_id); return


    success = False
    error_message = ""

    if action == 'name':
        if not new_value_str or len(new_value_str) > 70 or not new_value_str.replace('_','').isalnum():
            error_message = "Invalid item name. Use 1-70 alphanumeric or underscores (no spaces)."
        else:
            success = db_utils.update_product_details(product_id, name=new_value_str)
    elif action == 'price':
        try:
            new_price = float(new_value_str.replace(',', '.'))
            if new_price < 0: raise ValueError("Price must be non-negative.")
            success = db_utils.update_product_details(product_id, price=new_price)
        except ValueError:
            error_message = "Invalid price. Must be a non-negative number."
    elif action == 'city':
        if not new_value_str or len(new_value_str) > 50 or not all(part.isalnum() or part == '' for part in new_value_str.split(' ')):
            error_message = "Invalid city name. Use 1-50 alphanumeric (spaces allowed)."
        else:
            success = db_utils.update_product_details(product_id, city=new_value_str)
            if success: bot.send_message(chat_id, "⚠️ *Warning:* City changed in database only. Filesystem folder was NOT moved. Manual sync or FS adjustment may be needed.", parse_mode="MarkdownV2")

    if success:
        bot.send_message(chat_id, f"✅ *{escape_md(action.title())}* for item *{escape_md(product_before_edit['name'])}* \\(ID: `{product_id}`\\) updated to *{escape_md(new_value_str)}*\\.", parse_mode="MarkdownV2")
    else:
        bot.send_message(chat_id, f"❌ Error updating {escape_md(action.title())}: {escape_md(error_message) if error_message else 'Database update failed.'}", parse_mode="MarkdownV2")

    update_user_state(admin_id, 'admin_flow', None)
    update_user_state(admin_id, 'admin_editing_attribute', None)
    update_user_state(admin_id, 'admin_edit_item_generic_prompt_id', None)

    class MockMessageForSelect:
        def __init__(self, chat, from_user, message_id=0):
            self.chat = chat
            self.message_id = message_id
            self.from_user = from_user

    chat_instance_mock = f"{message.chat.id}_{admin_id}_{datetime.datetime.now().timestamp()}"


    mock_call_obj = types.CallbackQuery(
        id=f"mockcall_reselect_{product_id}_{datetime.datetime.now().timestamp()}",
        from_user=message.from_user,
        data=f"admin_edit_item_select_{product_id}",
        chat_instance=chat_instance_mock,
        json_string="",
        message=MockMessageForSelect(message.chat, message.from_user)
    )
    handle_admin_edit_item_select_callback(mock_call_obj)


# --- Delete Item Flow ---
@bot.message_handler(commands=['deleteitem'], func=is_admin)
def command_delete_item_list(message, page=0):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    logger.info(f"Admin {admin_id} initiated /deleteitem, page {page}.")

    if hasattr(message, 'text') and message.text and message.text.startswith('/deleteitem'):
        try: delete_message(bot, chat_id, message.message_id)
        except: pass

    if hasattr(message, 'message') and message.message:
         current_list_msg_id = get_user_state(admin_id, 'admin_delete_item_list_msg_id')
         if current_list_msg_id and current_list_msg_id == message.message.message_id:
             pass
         elif message.message.message_id:
             try: delete_message(bot, chat_id, message.message.message_id)
             except: pass

    update_user_state(admin_id, 'admin_flow', 'deleting_item_list')

    products = db_utils.get_all_products_admin()
    if not products:
        old_list_msg_id = get_user_state(admin_id, 'admin_delete_item_list_msg_id')
        if old_list_msg_id:
            try: delete_message(bot, chat_id, old_list_msg_id)
            except:pass
            update_user_state(admin_id, 'admin_delete_item_list_msg_id', None)
        bot.send_message(chat_id, "No items found to delete.")
        clear_user_state(admin_id)
        return

    total_items = len(products)
    total_pages = (total_items + ITEMS_PER_PAGE_ADMIN - 1) // ITEMS_PER_PAGE_ADMIN
    page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0

    start_index = page * ITEMS_PER_PAGE_ADMIN
    end_index = start_index + ITEMS_PER_PAGE_ADMIN
    items_to_display = products[start_index:end_index]

    response_text = f"🗑️ *Delete Items (Page {page + 1}/{total_pages if total_pages > 0 else 1})*\n"
    response_text += "Select an item type to delete\\. This action is IRREVERSIBLE and will delete all its instances and data\\.\n\n"
    if not items_to_display:
        response_text += "No items on this page." if page > 0 else "No items available."

    markup = types.InlineKeyboardMarkup(row_width=1)
    for item in items_to_display:
        item_id = item['product_id']
        item_name_escaped = escape_md(item['name'])
        item_city_escaped = escape_md(item['city'])
        availability_icon = "✅" if item['is_available'] else "🅾️"
        item_button_text = f"{availability_icon} {item_name_escaped} ({item_city_escaped}) - {item['price']:.2f} EUR"
        markup.add(types.InlineKeyboardButton(f"🗑️ {item_button_text}", callback_data=f"admin_delete_item_confirm_{item_id}"))

    nav_buttons_row = []
    if page > 0:
        nav_buttons_row.append(types.InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_delete_item_page_{page - 1}"))
    if total_pages > 1:
        nav_buttons_row.append(types.InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data=f"admin_delete_item_page_{page}"))
    if (page + 1) < total_pages:
        nav_buttons_row.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"admin_delete_item_page_{page + 1}"))

    if nav_buttons_row:
        markup.row(*nav_buttons_row)
    markup.add(types.InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_main_menu"))

    existing_list_msg_id = get_user_state(admin_id, 'admin_delete_item_list_msg_id')
    sent_list_msg = send_or_edit_message(bot, chat_id, response_text,
                                         reply_markup=markup,
                                         existing_message_id=existing_list_msg_id,
                                         parse_mode="MarkdownV2")
    if sent_list_msg:
        update_user_state(admin_id, 'admin_delete_item_list_msg_id', sent_list_msg.message_id)
    update_user_state(admin_id, 'admin_delete_item_current_page', page)

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_delete_item_page_') and is_admin(call))
def handle_delete_item_page_callback(call):
    admin_id = call.from_user.id
    try: page = int(call.data.split('_')[-1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid page number.", show_alert=True); return
    bot.answer_callback_query(call.id)
    command_delete_item_list(call, page=page)

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_delete_item_confirm_') and is_admin(call))
def handle_admin_delete_item_confirm_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    try: product_id = int(call.data.split('admin_delete_item_confirm_')[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid product ID.", show_alert=True); return

    product = db_utils.get_product_details_by_id(product_id)
    if not product:
        bot.answer_callback_query(call.id, "Product not found.", show_alert=True); return

    update_user_state(admin_id, 'admin_deleting_product_id', product_id)
    update_user_state(admin_id, 'admin_flow', 'admin_confirming_delete_item')

    list_msg_id = get_user_state(admin_id, 'admin_delete_item_list_msg_id')
    if list_msg_id:
        try: delete_message(bot, chat_id, list_msg_id)
        except: pass
        update_user_state(admin_id, 'admin_delete_item_list_msg_id', None)

    confirm_text = (f"⚠️ *Confirm Deletion*\n\n"
                    f"Are you sure you want to permanently delete the item type:\n"
                    f"Name: *{escape_md(product['name'])}*\n"
                    f"City: *{escape_md(product['city'])}*\n"
                    f"ID: `{product_id}`\n\n"
                    f"This will remove all its data, instances, and files from the system\\. This action cannot be undone\\.")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🗑️ YES, DELETE", callback_data=f"admin_delete_item_do_{product_id}"),
        types.InlineKeyboardButton("❌ NO, Cancel", callback_data="admin_delete_item_cancel")
    )
    confirm_msg = bot.send_message(chat_id, confirm_text, reply_markup=markup, parse_mode="MarkdownV2")
    update_user_state(admin_id, 'admin_delete_item_confirm_msg_id', confirm_msg.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'admin_delete_item_cancel' and is_admin(call))
def handle_admin_delete_item_cancel_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id

    confirm_msg_id = get_user_state(admin_id, 'admin_delete_item_confirm_msg_id')
    if confirm_msg_id:
        try: delete_message(bot, chat_id, confirm_msg_id)
        except: pass

    bot.send_message(chat_id, "Item deletion cancelled.")
    current_page = get_user_state(admin_id, 'admin_delete_item_current_page', 0)
    clear_user_state(admin_id)
    update_user_state(admin_id, 'admin_delete_item_current_page', current_page)
    bot.answer_callback_query(call.id, "Cancelled.")

    mock_message = telebot.types.Message(0,call.from_user,datetime.datetime.now().timestamp(),call.message.chat,'text',{},"")
    mock_message.text = None
    command_delete_item_list(mock_message, page=current_page)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_delete_item_do_') and is_admin(call))
def handle_admin_delete_item_do_callback(call):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    try: product_id = int(call.data.split('admin_delete_item_do_')[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid product ID for deletion.", show_alert=True); return

    stored_product_id = get_user_state(admin_id, 'admin_deleting_product_id')
    if stored_product_id != product_id:
        bot.answer_callback_query(call.id, "Confirmation mismatch. Please try again.", show_alert=True)
        clear_user_state(admin_id); return

    confirm_msg_id = get_user_state(admin_id, 'admin_delete_item_confirm_msg_id')
    if confirm_msg_id:
        try: delete_message(bot, chat_id, confirm_msg_id)
        except: pass

    product = db_utils.get_product_details_by_id(product_id)
    if not product:
        bot.send_message(chat_id, "Error: Product to delete not found in database.")
        clear_user_state(admin_id); bot.answer_callback_query(call.id); return

    folder_path_to_delete = product['folder_path']
    fs_deleted, fs_msg = file_system_utils.delete_item_folder_by_path(folder_path_to_delete)

    if not fs_deleted:
        bot.send_message(chat_id, f"⚠️ Filesystem deletion failed for '{escape_md(product['name'])}': {escape_md(fs_msg)}\nDatabase record NOT deleted.", parse_mode="MarkdownV2")
        clear_user_state(admin_id); bot.answer_callback_query(call.id, "Filesystem error."); return

    db_deleted = db_utils.delete_product_type_db_record(product_id)
    if db_deleted:
        bot.send_message(chat_id, f"✅ Item type *{escape_md(product['name'])}* \\(ID: `{product_id}`\\) and its files have been permanently deleted.", parse_mode="MarkdownV2")
    else:
        bot.send_message(chat_id, f"⚠️ Item files for *{escape_md(product['name'])}* deleted, but database record removal failed. Please check logs. Product ID: `{product_id}`", parse_mode="MarkdownV2")

    current_page = get_user_state(admin_id, 'admin_delete_item_current_page', 0)
    clear_user_state(admin_id)
    update_user_state(admin_id, 'admin_delete_item_current_page', current_page)
    bot.answer_callback_query(call.id)

    mock_message = telebot.types.Message(0,call.from_user,datetime.datetime.now().timestamp(),call.message.chat,'text',{},"")
    mock_message.text = None
    command_delete_item_list(mock_message, page=current_page)


# --- Admin User Management ---
@bot.message_handler(commands=['viewusers'], func=is_admin)
def command_view_users(message, page=0):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    logger.info(f"Admin {admin_id} requested /viewusers, page {page}.")

    existing_list_msg_id = get_user_state(admin_id, 'admin_user_list_main_msg_id')

    if hasattr(message, 'text') and message.text and message.text.startswith('/viewusers'):
        try: delete_message(bot, chat_id, message.message_id)
        except: pass
        if existing_list_msg_id:
            try: delete_message(bot, chat_id, existing_list_msg_id)
            except: pass
        update_user_state(admin_id, 'admin_user_list_main_msg_id', None)
        existing_list_msg_id = None
    elif hasattr(message, 'message') and message.message:
        existing_list_msg_id = message.message.message_id

    users_list, total_users = db_utils.get_all_users_admin(limit=USERS_PER_PAGE_ADMIN, offset=page * USERS_PER_PAGE_ADMIN)

    if not users_list and total_users == 0:
        if existing_list_msg_id:
            try: delete_message(bot, chat_id, existing_list_msg_id)
            except: pass
            update_user_state(admin_id, 'admin_user_list_main_msg_id', None)
        bot.send_message(chat_id, "No users found in the system.")
        return

    response_text = f"👥 *User List (Page {page + 1} / { (total_users + USERS_PER_PAGE_ADMIN -1) // USERS_PER_PAGE_ADMIN })*\nSelect a user to view details:\n\n"

    markup = types.InlineKeyboardMarkup(row_width=1)

    if not users_list and page > 0 :
        response_text += "No users on this page."
    else:
        for user_row in users_list:
            user_id_to_view = user_row['user_id']
            user_info_line = (f"ID: `{user_id_to_view}` B: *{user_row['balance']:.2f}€* Txs: *{user_row['transaction_count']}*")
            markup.add(types.InlineKeyboardButton(user_info_line, callback_data=f"admin_view_user_details_{user_id_to_view}"))

    nav_buttons_row = []
    if page > 0:
        nav_buttons_row.append(types.InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_users_page_{page - 1}"))
    if (total_users > USERS_PER_PAGE_ADMIN) and not (page == 0 and total_users <= USERS_PER_PAGE_ADMIN) :
         nav_buttons_row.append(types.InlineKeyboardButton(f"📄 {page+1}", callback_data=f"admin_users_page_{page}"))
    if (page + 1) * USERS_PER_PAGE_ADMIN < total_users:
        nav_buttons_row.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_page_{page + 1}"))

    if nav_buttons_row:
        markup.row(*nav_buttons_row)

    sent_msg = send_or_edit_message(bot, chat_id, response_text,
                                    reply_markup=markup,
                                    existing_message_id=existing_list_msg_id,
                                    parse_mode="MarkdownV2")
    if sent_msg:
        update_user_state(admin_id, 'admin_user_list_main_msg_id', sent_msg.message_id)
    update_user_state(admin_id, 'admin_user_list_current_page', page)


@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_users_page_') and is_admin(call))
def callback_view_users_page(call):
    admin_id = call.from_user.id
    try:
        page = int(call.data.split('_')[-1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Invalid page number.", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    command_view_users(call, page=page)

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_view_user_details_') and is_admin(call))
def handle_admin_view_user_details_callback(call, page=0):
    admin_id = call.from_user.id
    chat_id = call.message.chat.id
    target_user_id = None # Initialize

    if call.data.startswith('admin_view_user_details_page_'):
        try:
            parts = call.data.split('_')
            target_user_id = int(parts[4])
            page = int(parts[5])
            update_user_state(admin_id, 'admin_viewing_user_id', target_user_id)
            update_user_state(admin_id, 'admin_viewing_user_tx_page', page)
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "Invalid user/page for details.", show_alert=True); return
    elif call.data.startswith('admin_view_user_details_'):
        try:
            target_user_id = int(call.data.split('admin_view_user_details_')[1])
            update_user_state(admin_id, 'admin_viewing_user_id', target_user_id)
            update_user_state(admin_id, 'admin_viewing_user_tx_page', 0)
            page = 0
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "Invalid user ID for details.", show_alert=True); return
    else:
        bot.answer_callback_query(call.id, "Unknown action.", show_alert=True); return

    if target_user_id is None: # Should be set by one of the branches above
        bot.answer_callback_query(call.id, "Target user ID not determined.", show_alert=True); return

    logger.info(f"Admin {admin_id} viewing details for user {target_user_id}, tx page {page}.")
    update_user_state(admin_id, 'admin_flow', 'admin_viewing_user_details')

    user_data = get_or_create_user(target_user_id)
    if not user_data:
        bot.answer_callback_query(call.id, "User not found.", show_alert=True)
        user_list_page = get_user_state(admin_id, 'admin_user_list_current_page', 0)
        command_view_users(call, page=user_list_page)
        return

    # Delete the main user list message or previous detail view message
    # If called from user list, call.message.id is the user list message.
    # If called from its own pagination, call.message.id is the user detail message.
    if call.message and call.message.message_id:
        try: delete_message(bot, chat_id, call.message.message_id)
        except: pass
    # Clear specific message IDs from state
    update_user_state(admin_id, 'admin_user_list_main_msg_id', None)
    update_user_state(admin_id, 'admin_view_user_details_msg_id', None)


    balance = user_data['balance']
    total_user_transactions = user_data['transaction_count']

    user_info_text = (
        f"👤 *User Details: ID `{target_user_id}`*\n\n"
        f"Current Balance: *{balance:.2f} EUR*\n"
        f"Total Transactions: *{total_user_transactions}*"
    )

    offset = page * TX_HISTORY_PAGE_SIZE
    transactions = get_user_transaction_history(target_user_id, limit=TX_HISTORY_PAGE_SIZE, offset=offset)
    history_text_formatted = format_transaction_history_display(transactions)

    full_user_detail_text = user_info_text + history_text_formatted

    markup = types.InlineKeyboardMarkup(row_width=2)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Prev TXs", callback_data=f"admin_view_user_details_page_{target_user_id}_{page - 1}"))

    total_tx_pages_for_user = (total_user_transactions + TX_HISTORY_PAGE_SIZE - 1) // TX_HISTORY_PAGE_SIZE
    if (page + 1) < total_tx_pages_for_user:
        nav_buttons.append(types.InlineKeyboardButton("Next TXs ➡️", callback_data=f"admin_view_user_details_page_{target_user_id}_{page + 1}"))

    if nav_buttons:
        markup.row(*nav_buttons)

    markup.add(types.InlineKeyboardButton("💰 Adjust Balance", callback_data=f"admin_adjust_bal_init_{target_user_id}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back to User List", callback_data="admin_back_to_user_list"))

    sent_msg = bot.send_message(chat_id, full_user_detail_text, reply_markup=markup, parse_mode="MarkdownV2")
    update_user_state(admin_id, 'admin_view_user_details_msg_id', sent_msg.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == 'admin_back_to_user_list' and is_admin(call))
def handle_admin_back_to_user_list_callback(call):
    admin_id = call.from_user.id
    details_msg_id = get_user_state(admin_id, 'admin_view_user_details_msg_id')
    if details_msg_id: # This is the user detail message with TX history
        try: delete_message(bot, call.message.chat.id, details_msg_id)
        except: pass
        update_user_state(admin_id, 'admin_view_user_details_msg_id', None)

    page = get_user_state(admin_id, 'admin_user_list_current_page', 0)
    # Clear specific user view states, but keep admin_user_list_current_page
    update_user_state(admin_id, 'admin_viewing_user_id', None)
    update_user_state(admin_id, 'admin_viewing_user_tx_page', None)
    update_user_state(admin_id, 'admin_flow', None) # Reset general admin flow

    command_view_users(call, page=page)
    bot.answer_callback_query(call.id)


if __name__ == '__main__':
    logger.info("Admin Handler module loaded.")

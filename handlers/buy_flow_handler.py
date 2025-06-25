import telebot
from telebot import types
# from bot import bot, get_user_state, update_user_state, clear_user_state # Added clear_user_state # Removed to break circular import
import json
import time
import logging # Added logging
from modules.db_utils import (
    get_cities_with_available_items, get_available_items_in_city,
    get_product_details_by_id, get_or_create_user, update_user_balance,
    record_transaction, update_transaction_status,
    get_pending_payment_by_transaction_id,
    update_pending_payment_status,
    increment_user_transaction_count,
    sync_item_from_fs_to_db,
    get_next_address_index, create_pending_payment, # Added for HD Wallet
    update_main_transaction_for_hd_payment, # Added for HD Wallet
    get_transaction_by_id # Added for checking status
)
from modules import file_system_utils
from modules.message_utils import send_or_edit_message, delete_message
from modules.text_utils import escape_md
from modules import hd_wallet_utils, exchange_rate_utils, payment_monitor # Added these
import config
import os
import datetime # Ensure datetime is imported
from decimal import Decimal, ROUND_UP # Ensure ROUND_UP is imported
import sqlite3 # For specific exception handling in finalize

from handlers.main_menu_handler import get_main_menu_text_and_markup # For fallbacks


logger = logging.getLogger(__name__)

# @bot.callback_query_handler(func=lambda call: call.data == 'buy_initiate') # Commented out to break circular import
def handle_buy_initiate_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    logger.info(f"handle_buy_initiate_callback called for user {call.from_user.id}")
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    existing_message_id = call.message.message_id
    logger.info(f"User {user_id} initiated buy flow.")

    try:
        clear_user_state(user_id)
        update_user_state(user_id, 'current_flow', 'buy_selecting_city')

        cities = get_cities_with_available_items()
        markup = types.InlineKeyboardMarkup(row_width=2)
        prompt_text = "🏙️ Please select a city:"

        if not cities:
            prompt_text = "😔 We're sorry, there are currently no items available for purchase. Please check back later."
        else:
            city_buttons = []
            for city_name in cities:
                city_name_escaped_for_button = escape_md(city_name)
                city_buttons.append(types.InlineKeyboardButton(text=f"🏙️ {city_name_escaped_for_button}", callback_data=f"select_city_{city_name}"))

            for i in range(0, len(city_buttons), 2):
                if i + 1 < len(city_buttons):
                    markup.add(city_buttons[i], city_buttons[i+1])
                else:
                    markup.add(city_buttons[i])

        markup.add(types.InlineKeyboardButton(text="⬅️ Back to Main Menu", callback_data="back_to_main"))

        sent_message = None
        buy_flow_image_path = getattr(config, 'BUY_FLOW_IMAGE_PATH', None)
        photo_exists_and_valid = buy_flow_image_path and os.path.exists(buy_flow_image_path)

        if photo_exists_and_valid:
            if existing_message_id:
                try:
                    delete_message(bot_instance, chat_id, existing_message_id)
                except Exception as e_del:
                    logger.warning(f"Notice: Could not delete previous message {existing_message_id} before sending buy_initiate photo for user {user_id}: {e_del}")

            with open(buy_flow_image_path, 'rb') as photo_file:
                sent_message = bot_instance.send_photo(
                    chat_id,
                    photo=photo_file,
                    caption=prompt_text,
                    reply_markup=markup
                )
        else:
            sent_message = send_or_edit_message(
                bot=bot_instance,
                chat_id=chat_id,
                text=prompt_text,
                reply_markup=markup,
                existing_message_id=existing_message_id
            )

        if sent_message:
            update_user_state(user_id, 'last_bot_message_id', sent_message.message_id)

        bot_instance.answer_callback_query(call.id)

    except Exception as e:
        logger.exception(f"Error in handle_buy_initiate_callback for user {user_id}: {e}")
        bot_instance.answer_callback_query(call.id, "An error occurred while loading cities.")
        try:
            fallback_markup = types.InlineKeyboardMarkup()
            btn_fallback_back = types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main")
            fallback_markup.add(btn_fallback_back)
            send_or_edit_message(bot_instance, chat_id, "Sorry, there was an error. Please try returning to the main menu.",
                                 reply_markup=fallback_markup, existing_message_id=existing_message_id)
        except Exception as e_fallback:
            logger.error(f"Error sending fallback message in handle_buy_initiate_callback to user {user_id}: {e_fallback}")


# @bot.callback_query_handler(func=lambda call: call.data.startswith('select_city_')) # Commented out to break circular import
def handle_city_selection_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    existing_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    logger.info(f"User {user_id} selected city via callback: {call.data}")

    try:
        city_name = call.data.split('select_city_', 1)[1]
    except IndexError:
        logger.warning(f"Invalid callback data for city selection: {call.data} by user {user_id}")
        bot.answer_callback_query(call.id, "Error processing city selection. Please try again.", show_alert=True)
        return

    update_user_state(user_id, 'buy_selected_city', city_name)
    update_user_state(user_id, 'current_flow', 'buy_selecting_item')

    items = get_available_items_in_city(city_name)
    markup = types.InlineKeyboardMarkup(row_width=1)
    escaped_city_name = escape_md(city_name)
    prompt_text = ""

    if not items:
        prompt_text = f"😔 No items currently available in *{escaped_city_name}*\\."
    else:
        prompt_text = f"You selected city: *{escaped_city_name}*\\.\nPlease select an item:"
        for item in items:
            item_name_escaped = escape_md(item['name'])
            markup.add(types.InlineKeyboardButton(item_name_escaped, callback_data=f"select_item_{item['product_id']}"))

    markup.add(types.InlineKeyboardButton("⬅️ Back to City Selection", callback_data="buy_initiate"))
    markup.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

    sent_message_id = send_or_edit_message(
        bot=bot,
        chat_id=chat_id,
        text=prompt_text,
        reply_markup=markup,
        existing_message_id=existing_message_id,
        parse_mode="MarkdownV2"
    )

    if sent_message_id:
        update_user_state(user_id, 'last_bot_message_id', sent_message_id)

    bot.answer_callback_query(call.id)

# @bot.callback_query_handler(func=lambda call: call.data.startswith('select_item_')) # Commented out to break circular import
def handle_item_selection_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    existing_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    logger.info(f"User {user_id} selected item via callback: {call.data}")

    try:
        product_id_str = call.data.split('select_item_', 1)[1]
        product_id = int(product_id_str)
    except (IndexError, ValueError):
        logger.warning(f"Invalid product ID in callback data: {call.data} for user {user_id}")
        bot.answer_callback_query(call.id, "Error: Invalid item ID.", show_alert=True)
        return

    update_user_state(user_id, 'buy_selected_product_id', product_id)

    product_db_data = get_product_details_by_id(product_id)

    if not product_db_data or not product_db_data['is_available']:
        logger.warning(f"Product {product_id} unavailable or not found for user {user_id}.")
        error_text = "This product is currently unavailable or an error occurred while fetching details."
        markup = types.InlineKeyboardMarkup()
        selected_city_for_back = get_user_state(user_id, 'buy_selected_city')
        cb_data_back = f"select_city_{selected_city_for_back}" if selected_city_for_back else "buy_initiate"
        markup.add(types.InlineKeyboardButton("⬅️ Back to Item List", callback_data=cb_data_back))
        markup.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
        send_or_edit_message(bot, chat_id, error_text, reply_markup=markup, existing_message_id=existing_message_id, parse_mode="MarkdownV2")
        bot.answer_callback_query(call.id, "Product unavailable.")
        return

    item_display_details = file_system_utils.get_item_details(product_db_data['city'], product_db_data['name'])

    if not item_display_details or not item_display_details.get('description'):
        logger.warning(f"Display details/description missing for product {product_id}, user {user_id}.")
        error_text = "Details for this item could not be loaded. It might be temporarily unavailable or missing essential information."
        markup = types.InlineKeyboardMarkup()
        selected_city = get_user_state(user_id, 'buy_selected_city')
        cb_data_back_item = f"select_city_{selected_city}" if selected_city else "buy_initiate"
        markup.add(types.InlineKeyboardButton("⬅️ Back to Item List", callback_data=cb_data_back_item))
        markup.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
        send_or_edit_message(bot, chat_id, error_text, reply_markup=markup, existing_message_id=existing_message_id, parse_mode="MarkdownV2")
        bot.answer_callback_query(call.id, "Item details missing.")
        return

    user_data = get_or_create_user(user_id)
    item_price = Decimal(str(product_db_data['price']))
    try:
        service_fee = Decimal(str(config.SERVICE_FEE_EUR))
    except (AttributeError, ValueError, TypeError):
        logger.critical(f"SERVICE_FEE_EUR ('{getattr(config, 'SERVICE_FEE_EUR', 'NOT SET')}') is not a valid Decimal. Defaulting to 0.0.")
        service_fee = Decimal('0.0')

    total_cost = item_price + service_fee
    user_balance = Decimal(str(user_data['balance'])) if user_data and 'balance' in user_data else Decimal('0.0')

    product_city = product_db_data['city']
    product_type_folder_name = product_db_data['name']
    product_main_folder_path = product_db_data['folder_path']

    if user_balance >= total_cost:
        logger.info(f"User {user_id} purchasing product {product_id} entirely with balance. Total: {total_cost}, Balance: {user_balance}")
        update_user_state(user_id, 'current_flow', 'buy_processing_balance_payment')
        new_balance = user_balance - total_cost
        update_user_balance(user_id, float(new_balance), increment_transactions=True)

        actual_instance_path = item_display_details.get('actual_instance_path')
        if not actual_instance_path:
            logger.critical(f"No actual_instance_path for product ID {product_id} during balance purchase for user {user_id}.")
            bot.send_message(chat_id, "Purchase successful, but there's an issue with item delivery. Please contact support with your User ID and transaction details.", parse_mode="MarkdownV2")
            record_transaction(user_id=user_id, product_id=product_id, charge_id=None, type='purchase_balance', eur_amount=float(total_cost), payment_status='completed_fulfillment_error', notes=f"Paid from balance. CRITICAL: Instance path missing for product type {product_type_folder_name}")
            clear_user_state(user_id)
            bot.answer_callback_query(call.id, "Purchase complete, item error.")
            return

        instance_folder_name = os.path.basename(actual_instance_path)
        move_success = file_system_utils.move_item_to_purchased(product_city, product_type_folder_name, instance_folder_name)

        if move_success:
            logger.info(f"Successfully moved instance {instance_folder_name} for product {product_id} for user {user_id}.")
            sync_item_from_fs_to_db(product_city, product_type_folder_name, product_main_folder_path)
        else:
            logger.error(f"Filesystem move failed for product ID {product_id}, instance {instance_folder_name} (User: {user_id}). Payment processed.")

        record_transaction(user_id=user_id, product_id=product_id, charge_id=None, type='purchase_balance', eur_amount=float(total_cost), payment_status='completed' if move_success else 'completed_fs_move_error', notes=f"Paid from balance. Instance: {instance_folder_name}. FS Move: {'OK' if move_success else 'FAIL'}")

        product_name_escaped = escape_md(product_db_data['name'])
        city_escaped = escape_md(product_city)
        full_desc_escaped = escape_md(item_display_details['description'])

        success_message = (f"🎉 Your purchase of *{product_name_escaped}* in *{city_escaped}* is complete, paid with balance\\!\n\n"
                           f"*Item Details:*\n{full_desc_escaped}")

        markup_main_menu = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

        if existing_message_id:
            try: delete_message(bot, chat_id, existing_message_id)
            except Exception as e_del: logger.warning(f"Notice: Failed to delete message {existing_message_id} in balance purchase for user {user_id}: {e_del}")

        sent_msg = bot.send_message(chat_id, success_message, reply_markup=markup_main_menu, parse_mode="MarkdownV2")

        image_paths_delivery = item_display_details.get('image_paths', [])
        if image_paths_delivery and os.path.exists(image_paths_delivery[0]):
            try:
                with open(image_paths_delivery[0], 'rb') as photo_file:
                    bot.send_photo(chat_id, photo=photo_file, caption="Your purchased item image.")
            except Exception as e_photo:
                logger.error(f"Error sending delivery photo for product {product_id} (User {user_id}): {e_photo}")

        clear_user_state(user_id)
        update_user_state(user_id, 'last_bot_message_id', sent_msg.message_id)
        bot.answer_callback_query(call.id, "Purchase successful!")
        return

    paid_from_balance = Decimal('0.0')
    amount_to_pay_externally = total_cost

    if user_balance > Decimal('0.0'):
        paid_from_balance = min(user_balance, total_cost)
        amount_to_pay_externally = total_cost - paid_from_balance

    if amount_to_pay_externally < Decimal('0.0'): amount_to_pay_externally = Decimal('0.0')


    if amount_to_pay_externally == Decimal('0.0') and total_cost > Decimal('0.0') : # Should not happen if logic above is correct
        logger.error(f"LOGIC ERROR: amount_to_pay_externally is 0 but balance was less than total_cost. User: {user_id}, Balance: {user_balance}, Total: {total_cost}")
        bot.send_message(chat_id, "There was an issue calculating payment. Please try again or contact support.")
        clear_user_state(user_id) # Clear potentially corrupted state
        bot.answer_callback_query(call.id, "Calculation error.")
        return

    update_user_state(user_id, 'buy_amount_due_eur', float(amount_to_pay_externally))
    update_user_state(user_id, 'buy_paid_from_balance', float(paid_from_balance))
    update_user_state(user_id, 'buy_total_cost_eur', float(total_cost))
    update_user_state(user_id, 'current_flow', 'buy_awaiting_payment_method')
    logger.info(f"User {user_id} proceeding to crypto payment for product {product_id}. Amount due: {amount_to_pay_externally}, Paid from balance: {paid_from_balance}")

    product_name_escaped = escape_md(product_db_data['name'])
    description_raw = item_display_details['description']
    max_desc_len_caption = 600
    if len(description_raw) > max_desc_len_caption:
        description_raw = description_raw[:max_desc_len_caption] + "..."
    description_escaped = escape_md(description_raw)

    price_info_parts = [
        f"Item: *{product_name_escaped}*",
        f"Original Price: *{item_price:.2f} EUR*",
        f"Service Fee: *{service_fee:.2f} EUR*",
        f"Total Cost: *{total_cost:.2f} EUR*",
    ]
    if paid_from_balance > Decimal('0.0'):
      price_info_parts.append(f"Paid from balance: *{paid_from_balance:.2f} EUR*")
    price_info_parts.append(f"Amount Due: *{amount_to_pay_externally:.2f} EUR*")

    price_info_text = "\n".join(price_info_parts)

    final_caption = f"{price_info_text}\n\n*Item Info:*\n{description_escaped}\n\nPlease select a payment method:"
    if len(final_caption) > 1024: # Telegram caption limit
        final_caption = final_caption[:1021] + "..."

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🪙 USDT (TRC20)", callback_data="pay_buy_USDT"))
    markup.add(types.InlineKeyboardButton("🪙 BTC (Bitcoin)", callback_data="pay_buy_BTC"))
    markup.add(types.InlineKeyboardButton("🪙 LTC (Litecoin)", callback_data="pay_buy_LTC"))
    markup.add(types.InlineKeyboardButton("⬅️ Back to Item List", callback_data=f"select_city_{product_db_data['city']}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

    sent_message_id_val = None
    image_paths = item_display_details.get('image_paths', [])
    first_image_path = image_paths[0] if image_paths and isinstance(image_paths[0], str) and os.path.exists(image_paths[0]) else None

    current_msg_is_photo = call.message.content_type == 'photo'

    if first_image_path:
        if current_msg_is_photo and existing_message_id == call.message.message_id : # Current message is a photo, edit caption
            try:
                bot.edit_message_caption(caption=final_caption, chat_id=chat_id, message_id=existing_message_id, reply_markup=markup, parse_mode="MarkdownV2")
                sent_message_id_val = existing_message_id
            except Exception as e_caption:
                logger.warning(f"Error editing caption for item {product_id}, user {user_id}: {e_caption}. Deleting and resending photo.")
                if existing_message_id: delete_message(bot, chat_id, existing_message_id)
                with open(first_image_path, 'rb') as photo_file:
                    new_msg = bot.send_photo(chat_id, photo=photo_file, caption=final_caption, reply_markup=markup, parse_mode="MarkdownV2")
                    sent_message_id_val = new_msg.message_id
        else: # Not a photo or different message, send new photo
            if existing_message_id: delete_message(bot, chat_id, existing_message_id) # Delete old message (text or different photo)
            with open(first_image_path, 'rb') as photo_file:
                new_msg = bot.send_photo(chat_id, photo=photo_file, caption=final_caption, reply_markup=markup, parse_mode="MarkdownV2")
                sent_message_id_val = new_msg.message_id
    else: # No image for item, send/edit text message
        if current_msg_is_photo and existing_message_id == call.message.message_id : # If old was photo, delete it
            if existing_message_id: delete_message(bot, chat_id, existing_message_id)
            existing_message_id = None # Force send_or_edit_message to send new text message

        sent_message_id_val = send_or_edit_message(
            bot, chat_id, final_caption,
            reply_markup=markup,
            existing_message_id=existing_message_id,
            parse_mode="MarkdownV2"
        )

    if sent_message_id_val:
        update_user_state(user_id, 'last_bot_message_id', sent_message_id_val)

    bot.answer_callback_query(call.id)

# @bot.callback_query_handler(func=lambda call: call.data.startswith('pay_buy_')) # Commented out to break circular import
def handle_pay_buy_crypto_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    original_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    ack_msg = None # Initialize ack_msg

    try:
        crypto_currency = call.data.split('pay_buy_')[1] # e.g. "USDT", "BTC", "LTC"
        if crypto_currency.upper() not in ["USDT", "BTC", "LTC"]: raise IndexError("Invalid crypto")
        logger.info(f"User {user_id} selected crypto {crypto_currency} for buying item (HD Wallet flow).")
    except IndexError:
        logger.warning(f"Invalid callback data for pay_buy: {call.data} by user {user_id}")
        bot.answer_callback_query(call.id, "Error processing your selection.", show_alert=True)
        return

    product_id = get_user_state(user_id, 'buy_selected_product_id')
    amount_due_eur_float = get_user_state(user_id, 'buy_amount_due_eur') # This is what needs to be paid externally
    paid_from_balance_float = get_user_state(user_id, 'buy_paid_from_balance', 0.0) # Amount covered by balance
    total_cost_eur_float = get_user_state(user_id, 'buy_total_cost_eur') # Full item cost + service_fee

    if not all([product_id is not None, amount_due_eur_float is not None, total_cost_eur_float is not None]):
        logger.warning(f"Missing session data for pay_buy_crypto (HD Wallet) for user {user_id}.")
        error_text = "Your session seems to have expired or critical information is missing. Please restart the purchase."
        markup_error = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
        if original_message_id:
            send_or_edit_message(bot, chat_id, error_text, reply_markup=markup_error, existing_message_id=original_message_id, parse_mode="MarkdownV2")
        else:
            bot.send_message(chat_id, error_text, reply_markup=markup_error, parse_mode="MarkdownV2")
        clear_user_state(user_id)
        bot.answer_callback_query(call.id, "Session error. Please restart.", show_alert=True)
        return

    product_db_data = get_product_details_by_id(product_id)
    if not product_db_data:
        logger.error(f"Product details not found for product_id {product_id} during pay_buy_crypto (HD Wallet) for user {user_id}.")
        send_or_edit_message(bot, chat_id, "Error: Product details could not be fetched. Please try selecting the item again.", existing_message_id=original_message_id)
        bot.answer_callback_query(call.id, "Product data error.")
        return

    bot.answer_callback_query(call.id) # Acknowledge button press early
    if original_message_id:
        ack_msg = send_or_edit_message(bot, chat_id, "⏳ Generating your payment address...", existing_message_id=original_message_id, reply_markup=None)
    else:
        ack_msg = bot.send_message(chat_id, "⏳ Generating your payment address...")
    current_message_id_for_invoice = ack_msg.message_id if ack_msg else original_message_id


    transaction_notes_new = (f"User buying product ID {product_id} ({product_db_data['name']}). "
                         f"Total: {total_cost_eur_float:.2f} EUR. Paid from balance: {paid_from_balance_float:.2f} EUR. "
                         f"Due via {crypto_currency}: {amount_due_eur_float:.2f} EUR.")
    main_transaction_id = record_transaction( # Renamed from transaction_id to main_transaction_id for clarity
        user_id=user_id, product_id=product_id,
        type='purchase_crypto', eur_amount=total_cost_eur_float,
        payment_status='pending_address_generation', notes=transaction_notes_new,
        charge_id=None # No charge_id for HD Wallet
    )
    if not main_transaction_id:
        logger.error(f"HD Wallet: Failed to create transaction record for user {user_id}, product {product_id}.")
        send_or_edit_message(bot, chat_id, "Database error creating transaction. Please try again.", existing_message_id=current_message_id_for_invoice)
        return
    update_user_state(user_id, 'buy_transaction_id', main_transaction_id)


    coin_symbol_for_hd_wallet = crypto_currency
    display_coin_symbol = crypto_currency
    network_for_db = crypto_currency # Default for BTC, LTC

    if crypto_currency == "USDT":
        coin_symbol_for_hd_wallet = "TRX" # USDT (TRC20) uses TRX addresses
        network_for_db = "TRC20 (Tron)"
        # display_coin_symbol remains "USDT"

    try:
        next_idx = get_next_address_index(coin_symbol_for_hd_wallet)
    except Exception as e_idx:
        logger.exception(f"HD Wallet: Error getting next address index for {coin_symbol_for_hd_wallet} (user {user_id}, tx {main_transaction_id}): {e_idx}")
        send_or_edit_message(bot, chat_id, "Error generating payment address (index). Please try again later or contact support.", existing_message_id=current_message_id_for_invoice)
        update_transaction_status(main_transaction_id, 'error_address_generation')
        return

    unique_address = hd_wallet_utils.generate_address(coin_symbol_for_hd_wallet, next_idx)
    if not unique_address:
        logger.error(f"HD Wallet: Failed to generate address for {coin_symbol_for_hd_wallet}, index {next_idx} (user {user_id}, tx {main_transaction_id}).")
        send_or_edit_message(bot, chat_id, "Error generating payment address (HD). Please try again later or contact support.", existing_message_id=current_message_id_for_invoice)
        update_transaction_status(main_transaction_id, 'error_address_generation')
        return

    rate = exchange_rate_utils.get_current_exchange_rate("EUR", display_coin_symbol)
    if not rate:
        logger.error(f"HD Wallet: Could not get exchange rate for EUR to {display_coin_symbol} (user {user_id}, tx {main_transaction_id}).")
        send_or_edit_message(bot, chat_id, f"Could not retrieve exchange rate for {escape_md(display_coin_symbol)}. Please try again or contact support.", existing_message_id=current_message_id_for_invoice, parse_mode='MarkdownV2')
        update_transaction_status(main_transaction_id, 'error_exchange_rate')
        return

    precision_map = {"BTC": 8, "LTC": 8, "USDT": 6} # TODO: Move to config or coin_utils
    num_decimals = precision_map.get(display_coin_symbol, 8)
    amount_due_eur_decimal = Decimal(str(amount_due_eur_float))
    expected_crypto_amount_decimal_hr = (amount_due_eur_decimal / rate).quantize(Decimal('1e-' + str(num_decimals)), rounding=ROUND_UP)
    smallest_unit_multiplier = Decimal('1e-' + str(num_decimals))
    expected_crypto_amount_smallest_unit_str = str(int(expected_crypto_amount_decimal_hr * smallest_unit_multiplier))

    payment_window_minutes = getattr(config, 'PAYMENT_WINDOW_MINUTES', 60)
    expires_at_dt = datetime.datetime.utcnow() + datetime.timedelta(minutes=payment_window_minutes)

    update_success = update_main_transaction_for_hd_payment(
       main_transaction_id,
       status='awaiting_payment',
       crypto_amount=str(expected_crypto_amount_decimal_hr), # Store human-readable for now
       currency=display_coin_symbol
    )
    if not update_success:
        logger.error(f"HD Wallet: Failed to update main transaction {main_transaction_id} for user {user_id} (buy flow).")
        send_or_edit_message(bot, chat_id, "Database error updating transaction. Please try again.", existing_message_id=current_message_id_for_invoice)
        return

    db_coin_symbol_for_pending = "USDT_TRX" if crypto_currency == "USDT" else display_coin_symbol
    pending_payment_id = create_pending_payment(
       transaction_id=main_transaction_id,
       user_id=user_id,
       address=unique_address,
       coin_symbol=db_coin_symbol_for_pending,
       network=network_for_db,
       expected_crypto_amount=expected_crypto_amount_smallest_unit_str,
       expires_at=expires_at_dt,
       paid_from_balance_eur=paid_from_balance_float
    )
    if not pending_payment_id:
       logger.error(f"HD Wallet: Failed to create pending_crypto_payment for main_tx {main_transaction_id} (user {user_id}, buy flow).")
       update_transaction_status(main_transaction_id, 'error_creating_pending_payment')
       send_or_edit_message(bot, chat_id, "Error preparing payment record. Please try again or contact support.", existing_message_id=current_message_id_for_invoice)
       return

    qr_code_path = None
    try:
        qr_code_path = hd_wallet_utils.generate_qr_code_for_address(
           unique_address,
           str(expected_crypto_amount_decimal_hr),
           display_coin_symbol
        )
    except Exception as e_qr_gen:
        logger.error(f"HD Wallet (buy): QR code generation failed for {unique_address} (user {user_id}, tx {main_transaction_id}): {e_qr_gen}")

    product_name_escaped = escape_md(product_db_data['name'])
    invoice_text_md = (f"🧾 *INVOICE - Item Purchase*\n\n"
                       f"Item: *{product_name_escaped}*\n")
    if paid_from_balance_float > 0:
        invoice_text_md += f"Paid from balance: *{paid_from_balance_float:.2f} EUR*\n"
    invoice_text_md += (f"Amount Due (externally): *{amount_due_eur_float:.2f} EUR*\n\n"
                        f"🏦 *Payment Details*\n"
                        f"Currency: *{escape_md(display_coin_symbol)}*\n"
                        f"Network: *{escape_md(network_for_db)}*\n"
                        f"Address: `{escape_md(unique_address)}`\n\n"
                        f"*AMOUNT TO SEND:*\n`{escape_md(str(expected_crypto_amount_decimal_hr))} {escape_md(display_coin_symbol)}`\n\n")
    expires_at_formatted = escape_md(expires_at_dt.strftime('%Y-%m-%d %H:%M:%S UTC'))
    invoice_text_md += f"⏳ Expires: *{expires_at_formatted}*\n\n"
    invoice_text_md += "⚠️ Send the exact amount using the correct network. This address is for single use only."

    markup_invoice = types.InlineKeyboardMarkup(row_width=1)
    markup_invoice.add(types.InlineKeyboardButton("✅ Check Payment", callback_data=f"check_buy_payment_{main_transaction_id}"))
    markup_invoice.add(types.InlineKeyboardButton("🚫 Cancel Payment", callback_data=f"cancel_buy_payment_{main_transaction_id}"))
    markup_invoice.add(types.InlineKeyboardButton("⬅️ Change Payment Method", callback_data=f"select_item_{product_id}"))

    if current_message_id_for_invoice: # This was the "Generating address..." message
        try: delete_message(bot, chat_id, current_message_id_for_invoice)
        except Exception: pass

    sent_invoice_msg = None
    if qr_code_path and os.path.exists(qr_code_path):
        try:
            with open(qr_code_path, 'rb') as qr_photo:
                sent_invoice_msg = bot.send_photo(chat_id, photo=qr_photo, caption=invoice_text_md, reply_markup=markup_invoice, parse_mode="MarkdownV2")
        except Exception as e_qr:
            logger.error(f"Failed to send QR photo for buy item {main_transaction_id}: {e_qr}")
            sent_invoice_msg = bot.send_message(chat_id, invoice_text_md, reply_markup=markup_invoice, parse_mode="MarkdownV2")
        finally:
            if os.path.exists(qr_code_path):
                try: os.remove(qr_code_path)
                except Exception as e_rm: logger.error(f"Failed to remove QR code {qr_code_path}: {e_rm}")
    else:
        logger.warning(f"HD Wallet (buy): QR code not generated or not found for {unique_address} (user {user_id}, tx {main_transaction_id}). Sending text invoice.")
        sent_invoice_msg = bot.send_message(chat_id, invoice_text_md, reply_markup=markup_invoice, parse_mode="MarkdownV2")

    if sent_invoice_msg:
        update_user_state(user_id, 'last_bot_message_id', sent_invoice_msg.message_id)
    update_user_state(user_id, 'current_flow', 'buy_awaiting_hd_payment_confirmation')


# @bot.callback_query_handler(func=lambda call: call.data.startswith('check_buy_payment_')) # Commented out to break circular import
def handle_buy_check_payment_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    original_invoice_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    logger.info(f"User {user_id} checking buy payment status for callback: {call.data}")
    bot_instance = bot

    try:
        transaction_id_str = call.data.split('check_buy_payment_')[1]
        transaction_id = int(transaction_id_str)
    except (IndexError, ValueError):
        logger.warning(f"Invalid transaction ID in callback data for check_buy_payment: {call.data} for user {user_id}")
        bot_instance.answer_callback_query(call.id, "Error: Invalid transaction reference.", show_alert=True)
        return

    pending_payment_record = get_pending_payment_by_transaction_id(transaction_id)

    if not pending_payment_record:
        main_tx = get_transaction_details(transaction_id)
        status_msg = "Payment record not found or already processed."
        if main_tx: status_msg = f"Payment status: {escape_md(main_tx['payment_status'])}."
        bot_instance.answer_callback_query(call.id, status_msg, show_alert=True)
        if main_tx and main_tx['payment_status'] in ['completed', 'cancelled_by_user', 'expired_payment_window', 'error_finalizing_data']: # Terminal states
            new_markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
            if original_invoice_message_id and call.message.message_id == original_invoice_message_id:
                try:
                    if call.message.photo: bot_instance.edit_message_caption(caption=status_msg, chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=new_markup, parse_mode="MarkdownV2")
                    else: bot_instance.edit_message_text(text=status_msg, chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=new_markup, parse_mode="MarkdownV2")
                except: pass # Best effort
        return

    bot_instance.answer_callback_query(call.id)
    ack_msg = bot_instance.send_message(chat_id, "⏳ Checking payment status (on-demand)...")

    try:
        newly_confirmed, status_info = payment_monitor.check_specific_pending_payment(transaction_id)
        if ack_msg: delete_message(bot_instance, chat_id, ack_msg.message_id)

        if newly_confirmed and status_info == 'confirmed_unprocessed':
            logger.info(f"On-demand check for buy tx {transaction_id} (user {user_id}) resulted in new confirmation. Processing...")
            bot_instance.send_message(chat_id, "✅ Payment detected! Processing your purchase...")
            payment_monitor.process_confirmed_payments(bot_instance) # This will call finalize
        else:
            logger.info(f"On-demand check for buy tx {transaction_id} (user {user_id}): newly_confirmed={newly_confirmed}, status_info='{status_info}'")
            pending_payment_latest = get_pending_payment_by_transaction_id(transaction_id) # Refresh data

            current_invoice_text = call.message.caption if call.message.photo else call.message.text
            base_invoice_text = "\n".join([line for line in (current_invoice_text or "").split('\n') if not line.strip().startswith("Status:")])

            new_status_line = ""
            alert_message = ""
            show_alert_flag = True
            reply_markup_to_use = call.message.reply_markup
            product_id_for_back_button = get_user_state(user_id, 'buy_selected_product_id')

            if status_info == 'monitoring':
                confs = pending_payment_latest['confirmations'] if pending_payment_latest else 'N/A'
                new_status_line = f"Status: Still monitoring for sufficient confirmations. Current: {confs}."
                alert_message = f"Still monitoring. Confirmations: {confs}."
                show_alert_flag = False
            elif status_info == 'monitoring_updated':
                confs = pending_payment_latest['confirmations'] if pending_payment_latest else 'N/A'
                new_status_line = f"Status: Monitoring updated. Current confirmations: {confs}."
                alert_message = f"Monitoring updated. Confirmations: {confs}."
                show_alert_flag = False
            elif status_info == 'expired':
                new_status_line = f"Status: This payment request has expired."
                alert_message = "This payment request has expired."
                new_markup = types.InlineKeyboardMarkup(row_width=1)
                if product_id_for_back_button is not None:
                    new_markup.add(types.InlineKeyboardButton("⬅️ Try Different Payment Method", callback_data=f"select_item_{product_id_for_back_button}"))
                new_markup.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
                reply_markup_to_use = new_markup
            elif status_info == 'error_api':
                new_status_line = f"Status: Could not check status due to a temporary API error. Please try again in a moment."
                alert_message = "Could not check status due to an API error. Please try again."
            elif status_info == 'not_found':
                new_status_line = f"Status: Payment record not found."
                alert_message = "Payment record not found. This is unexpected."
                reply_markup_to_use = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
            elif status_info in ['processed', 'cancelled_by_user', 'error_finalizing', 'error_finalizing_data', 'error_monitoring_unsupported', 'error_processing_tx_missing', 'processed_tx_already_complete']:
                new_status_line = f"Status: Payment is in a final state: {escape_md(status_info)}."
                alert_message = f"Payment status: {status_info}."
                reply_markup_to_use = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
            else:
                new_status_line = f"Status: Current status: {escape_md(status_info)}."
                alert_message = f"Current status: {status_info}."
                if status_info != 'monitoring': # Non-monitoring, non-expired usually means terminal or error
                     reply_markup_to_use = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

            updated_text_for_invoice = f"{new_status_line}\n\n{base_invoice_text}".strip()
            if len(updated_text_for_invoice) > (1024 if call.message.photo else 4096): # Truncate
                updated_text_for_invoice = updated_text_for_invoice[:(1021 if call.message.photo else 4093)] + "..."

            try:
                if call.message.photo:
                    bot_instance.edit_message_caption(caption=updated_text_for_invoice, chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=reply_markup_to_use, parse_mode="MarkdownV2")
                else:
                    bot_instance.edit_message_text(text=updated_text_for_invoice, chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=reply_markup_to_use, parse_mode="MarkdownV2")
                bot_instance.answer_callback_query(call.id, alert_message, show_alert=show_alert_flag)
            except Exception as e_edit:
                logger.error(f"Error editing message {original_invoice_message_id} for on-demand buy check (tx {transaction_id}): {e_edit}")
                bot_instance.answer_callback_query(call.id, "Status updated, but message display failed to refresh.", show_alert=True)

    except Exception as e:
        logger.exception(f"Error in handle_check_buy_payment_callback (on-demand) for user {user_id}, tx {transaction_id_str}: {e}")
        if ack_msg: delete_message(bot_instance, chat_id, ack_msg.message_id)
        bot_instance.answer_callback_query(call.id, "An error occurred while checking payment status. Please try again.", show_alert=True)


# @bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_buy_payment_')) # Commented out to break circular import
def handle_cancel_buy_payment_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    original_invoice_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    logger.info(f"User {user_id} initiated cancel for buy payment: {call.data}")
    bot_instance = bot

    try:
        transaction_id_str = call.data.split('cancel_buy_payment_')[1]
        transaction_id = int(transaction_id_str)
    except (IndexError, ValueError):
        logger.warning(f"Invalid transaction ID in cancel_buy_payment callback: {call.data} for user {user_id}")
        bot_instance.answer_callback_query(call.id, "Error: Invalid transaction reference.", show_alert=True)
        return

    user_cancel_message = "Payment process cancelled by user."

    pending_payment = get_pending_payment_by_transaction_id(transaction_id)
    if pending_payment:
        if pending_payment['status'] == 'monitoring':
            if update_pending_payment_status(pending_payment['payment_id'], 'user_cancelled'):
                logger.info(f"HD Pending Payment {pending_payment['payment_id']} for buy TX_ID {transaction_id} marked as user_cancelled.")
                user_cancel_message = "Payment (HD Wallet) successfully cancelled."
            else:
                logger.error(f"Failed to update HD Pending Payment {pending_payment['payment_id']} for buy TX_ID {transaction_id} status to user_cancelled.")
                user_cancel_message = "Payment cancellation processed, but there was an issue updating pending record."
        else:
            logger.info(f"HD Pending Payment {pending_payment['payment_id']} for buy TX_ID {transaction_id} was not 'monitoring' (was {pending_payment['status']}). Main transaction will be cancelled.")
            user_cancel_message = f"Payment already in state '{pending_payment['status']}'. Marked as cancelled by you."
    else:
        logger.warning(f"No HD pending payment record found for buy TX_ID {transaction_id} upon cancellation. Main transaction will be marked cancelled.")

    if transaction_id:
        update_transaction_status(transaction_id, 'cancelled_by_user')
    else:
        logger.error(f"Cancel buy callback with no valid transaction_id from data: {call.data}")

    if original_invoice_message_id:
        try:
            delete_message(bot_instance, chat_id, original_invoice_message_id)
        except Exception as e_del:
            logger.error(f"Error deleting invoice message {original_invoice_message_id} on cancel for user {user_id}, buy tx {transaction_id}: {e_del}")

    user_cancel_message_final = user_cancel_message + "\nReturning to the main menu."
    markup_main_menu = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

    final_msg_sent = None
    try:
        final_msg_sent = bot_instance.send_message(chat_id, user_cancel_message_final, reply_markup=markup_main_menu, parse_mode="MarkdownV2")
    except Exception as e_send:
        logger.error(f"Error sending cancel confirmation (Markdown) for buy tx {transaction_id}: {e_send}. Sending plain text.")
        final_msg_sent = bot_instance.send_message(chat_id, user_cancel_message_final.replace('*','').replace('_','').replace('`','').replace('[','').replace(']','').replace('(','').replace(')',''), reply_markup=markup_main_menu)

    bot_instance.answer_callback_query(call.id, "Payment Cancelled.")

    clear_user_state(user_id)
    # Send a new main menu message, don't try to edit the (possibly deleted) invoice.
    welcome_text, markup = get_main_menu_text_and_markup()
    new_main_menu_msg = send_or_edit_message(
        bot_instance, chat_id, welcome_text,
        reply_markup=markup,
        existing_message_id=None, # Send a new message
        parse_mode=None
    )
    if new_main_menu_msg and hasattr(new_main_menu_msg, 'message_id'):
        update_user_state(user_id, 'last_bot_message_id', new_main_menu_msg.message_id)


# --- Payment Finalization Function (called by payment_monitor) ---
def finalize_successful_crypto_purchase(bot_instance, main_transaction_id: int, user_id: int,
                                        product_id: int, # Changed from product_id_str
                                        paid_from_balance_eur_str: str,
                                        received_crypto_amount_str: str, # For logging/verification
                                        coin_symbol: str, # For logging/verification
                                        blockchain_tx_id: str # For logging/verification
                                        ) -> bool:
    logger.info(f"Finalizing successful crypto purchase for user {user_id}, main_tx_id {main_transaction_id}, product_id {product_id}.")
    chat_id = user_id # Assuming direct message to user

    try:
        paid_from_balance_eur = Decimal(paid_from_balance_eur_str)
    except Exception as e_conv:
        logger.error(f"finalize_successful_crypto_purchase: Invalid paid_from_balance_eur_str '{paid_from_balance_eur_str}' for tx {main_transaction_id}. Error: {e_conv}")
        update_transaction_status(main_transaction_id, 'error_finalizing_data')
        return False

    try: # This try block should encompass the main finalization logic
        # 1. Adjust user balance if part of the payment was from balance
        if paid_from_balance_eur > Decimal('0.0'):
            user_current_data = get_or_create_user(user_id)
            if not user_current_data:
                logger.error(f"finalize_successful_crypto_purchase: Failed to get/create user {user_id} for tx {main_transaction_id} while adjusting balance.")
                update_transaction_status(main_transaction_id, 'error_finalizing_user_data')
                return False

            current_balance_decimal = Decimal(str(user_current_data['balance']))
            new_user_balance_decimal = current_balance_decimal - paid_from_balance_eur

            if not update_user_balance(user_id, float(new_user_balance_decimal), increment_transactions=False):
                logger.error(f"finalize_successful_crypto_purchase: Failed to update balance for user {user_id} (tx {main_transaction_id}) after partial balance payment.")
                update_transaction_status(main_transaction_id, 'error_finalizing_balance_update')
                return False

        # 2. Update main transaction status to 'completed'
        if not update_transaction_status(main_transaction_id, 'completed'):
            logger.warning(f"finalize_successful_crypto_purchase: Failed to update main transaction {main_transaction_id} status to 'completed'. Balance adjustment (if any) was done. User: {user_id}.")
            # Continue, as payment is confirmed.

        # 3. Increment user's overall transaction count for this purchase
        if not increment_user_transaction_count(user_id):
            logger.warning(f"finalize_successful_crypto_purchase: Failed to increment transaction count for user {user_id} (tx {main_transaction_id}). Main transaction status set to completed.")

        # 4. Fetch product details from DB
        product_db_data = get_product_details_by_id(product_id)
        if not product_db_data:
            logger.critical(f"finalize_successful_crypto_purchase: CRITICAL - Product details for product_id {product_id} not found after payment for tx {main_transaction_id}, user {user_id}.")
            bot_instance.send_message(chat_id, f"Payment confirmed for TXID {main_transaction_id}, but there was a CRITICAL error fetching product details. Please contact support immediately.")
            update_transaction_status(main_transaction_id, 'completed_item_data_error')
            return False

        # 5. Get item display details and actual instance path from filesystem
        item_display_details = file_system_utils.get_item_details(product_db_data['city'], product_db_data['name'])
        if not item_display_details or not item_display_details.get('actual_instance_path'):
            logger.critical(f"finalize_successful_crypto_purchase: CRITICAL - Fulfillment error for tx {main_transaction_id}, user {user_id}. Instance details/path missing for product: {product_db_data['name']}.")
            bot_instance.send_message(chat_id, f"Payment confirmed for {escape_md(product_db_data['name'])}, TXID {main_transaction_id}. However, the item instance is currently unavailable or details are missing. Please contact support.")
            update_transaction_status(main_transaction_id, 'completed_fulfillment_error')
            return False

        actual_instance_path = item_display_details['actual_instance_path']
        instance_folder_name = os.path.basename(actual_instance_path)

        # 6. Move the specific item instance to purchased folder
        move_success = file_system_utils.move_item_to_purchased(
            product_db_data['city'],
            product_db_data['name'],
            instance_folder_name
        )
        if not move_success:
            logger.error(f"finalize_successful_crypto_purchase: CRITICAL - Filesystem move FAILED for TXID {main_transaction_id}, product {product_id}, instance {instance_folder_name}, user {user_id}.")
            bot_instance.send_message(chat_id, f"Payment confirmed for {escape_md(product_db_data['name'])}, TXID {main_transaction_id}. There was an issue with item delivery. Please contact support.")
            update_transaction_status(main_transaction_id, 'completed_fs_move_error')
            return False

        logger.info(f"finalize_successful_crypto_purchase: Item instance '{instance_folder_name}' moved for tx {main_transaction_id}, user {user_id}.")

        # 7. Sync the product type with the database
        sync_item_from_fs_to_db(product_db_data['city'], product_db_data['name'], product_db_data['folder_path'])
        logger.info(f"finalize_successful_crypto_purchase: Filesystem sync triggered for product type {product_db_data['name']} in {product_db_data['city']} after purchase (tx {main_transaction_id}).")

        # 8. Send confirmation and delivery messages to user
        current_flow_state = get_user_state(user_id, 'current_flow')
        if current_flow_state and 'buy_' in current_flow_state:
            clear_user_state(user_id)
            logger.info(f"finalize_successful_crypto_purchase: Cleared user state for user {user_id} after successful purchase {main_transaction_id}.")

        last_bot_msg_id_before_clear = get_user_state(user_id, 'last_bot_message_id')
        if last_bot_msg_id_before_clear:
            try:
                 delete_message(bot_instance, chat_id, last_bot_msg_id_before_clear)
            except Exception as e_del:
                logger.warning(f"finalize_successful_crypto_purchase: Could not delete last bot message {last_bot_msg_id_before_clear} for user {user_id}, tx {main_transaction_id}: {e_del}")


        bot_instance.send_message(chat_id, f"✅ Payment confirmed for TXID {main_transaction_id}!")
        bot_instance.send_message(chat_id, f"Funds have been successfully processed for your purchase of *{escape_md(product_db_data['name'])}*\\.", parse_mode="MarkdownV2")

        delivery_text = f"Item Details:\n{escape_md(item_display_details['description'])}"
        delivery_markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

        new_msg_id_for_state = None
        delivery_images = item_display_details.get('image_paths', [])
        if delivery_images and isinstance(delivery_images, list) and len(delivery_images) > 0 and os.path.exists(delivery_images[0]):
            try:
                with open(delivery_images[0], 'rb') as photo:
                    sent_delivery_msg = bot_instance.send_photo(chat_id, photo, caption=delivery_text, reply_markup=delivery_markup, parse_mode="MarkdownV2")
                    new_msg_id_for_state = sent_delivery_msg.message_id
            except Exception as e_photo:
                logger.error(f"finalize_successful_crypto_purchase: Error sending delivery photo for product {product_id} (User {user_id}, TX {main_transaction_id}): {e_photo}")
                sent_delivery_msg = bot_instance.send_message(chat_id, delivery_text, reply_markup=delivery_markup, parse_mode="MarkdownV2")
                new_msg_id_for_state = sent_delivery_msg.message_id
        else:
            sent_delivery_msg = bot_instance.send_message(chat_id, delivery_text, reply_markup=delivery_markup, parse_mode="MarkdownV2")
            new_msg_id_for_state = sent_delivery_msg.message_id

        if new_msg_id_for_state:
            update_user_state(user_id, 'last_bot_message_id', new_msg_id_for_state)

        logger.info(f"finalize_successful_crypto_purchase: Successfully processed and delivered item for user {user_id}, tx {main_transaction_id}, product {product_id}.")
        return True

    except sqlite3.Error as e_sql: # More specific for database issues during finalization
        logger.exception(f"finalize_successful_crypto_purchase: SQLite error for user {user_id}, tx {main_transaction_id}: {e_sql}")
        bot_instance.send_message(chat_id, f"A database error occurred while finalizing your purchase (TXID {main_transaction_id}). Please contact support.")
        update_transaction_status(main_transaction_id, 'error_finalizing_db')
        return False
    except Exception as e:
        logger.exception(f"finalize_successful_crypto_purchase: Unexpected error for user {user_id}, tx {main_transaction_id}: {e}")
        bot_instance.send_message(chat_id, f"An unexpected error occurred while finalizing your purchase (TXID {main_transaction_id}). Please contact support.")
        update_transaction_status(main_transaction_id, 'error_finalizing_unexpected')
        return False

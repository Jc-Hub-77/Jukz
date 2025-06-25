# handlers/add_balance_handler.py
import logging
import os
import datetime
from decimal import Decimal, ROUND_UP

from telebot import types

from modules.db_utils import (
    get_or_create_user, update_user_balance, record_transaction,
    update_transaction_status, get_pending_payment_by_transaction_id,
    update_pending_payment_status, get_next_address_index,
    create_pending_payment, update_main_transaction_for_hd_payment,
    get_transaction_by_id, increment_user_transaction_count
)
from modules import hd_wallet_utils, exchange_rate_utils, payment_monitor
from modules.text_utils import escape_md
from modules.message_utils import send_or_edit_message, delete_message
import config
from handlers.main_menu_handler import get_main_menu_text_and_markup
import sqlite3 # For specific exception handling

logger = logging.getLogger(__name__)


def handle_add_balance_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    existing_message_id = call.message.message_id
    logger.info(f"User {user_id} initiated 'Add Balance' flow.")

    try:
        get_or_create_user(user_id) # Ensure user exists
        clear_user_state(user_id) # Clear any previous flow state
        update_user_state(user_id, 'current_flow', 'add_balance_awaiting_amount')

        prompt_text = "Please enter the EUR amount you wish to add to your balance (e\\.g\\., 20\\.00):"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main'))

        sent_message_id = send_or_edit_message(
            bot_instance, chat_id, escape_md(prompt_text),
            reply_markup=markup,
            existing_message_id=existing_message_id,
            parse_mode="MarkdownV2"
        )

        if sent_message_id:
            update_user_state(user_id, 'last_bot_message_id', sent_message_id)

    except Exception as e:
        logger.exception(f"Error in handle_add_balance_callback for user {user_id}: {e}")
        bot_instance.send_message(chat_id, "An error occurred. Please try returning to the main menu.",
                         reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main")))
    finally:
        bot_instance.answer_callback_query(call.id)


def handle_amount_input_for_add_balance(bot_instance, clear_user_state, get_user_state, update_user_state, message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    message_text = message.text
    logger.info(f"User {user_id} entered amount for add balance: {message_text}")

    existing_message_id = get_user_state(user_id, 'last_bot_message_id')

    try:
        float_amount_str = message_text.strip().replace(',', '.')
        requested_eur_decimal = Decimal(float_amount_str).quantize(Decimal('0.01'))

        if requested_eur_decimal <= Decimal('0.00'):
            raise ValueError("Amount must be positive.")
        if requested_eur_decimal > Decimal('5000.00'): # Max top-up
             raise ValueError("Maximum top-up amount is 5000 EUR.")

        try:
            # Access ADD_BALANCE_SERVICE_FEE_EUR from config directly
            service_fee_decimal = Decimal(str(config.ADD_BALANCE_SERVICE_FEE_EUR)).quantize(Decimal('0.01'))
        except (AttributeError, ValueError, TypeError):
            logger.critical(f"ADD_BALANCE_SERVICE_FEE_EUR ('{getattr(config, 'ADD_BALANCE_SERVICE_FEE_EUR', 'NOT SET')}') is not valid. Defaulting to 0.0.")
            service_fee_decimal = Decimal('0.00')

        total_due_eur_decimal = requested_eur_decimal + service_fee_decimal

        update_user_state(user_id, 'add_balance_requested_eur', float(requested_eur_decimal))
        update_user_state(user_id, 'add_balance_total_due_eur', float(total_due_eur_decimal))
        update_user_state(user_id, 'current_flow', 'add_balance_awaiting_payment_method')

        confirmation_text = (f"Amount to Add: *{requested_eur_decimal:.2f} EUR*\n"
                            f"Service Fee: *{service_fee_decimal:.2f} EUR*\n"
                            f"Total Due: *{total_due_eur_decimal:.2f} EUR*\n\n"
                            f"Please select your preferred payment method\\.")

        markup_select_payment = types.InlineKeyboardMarkup(row_width=1)
        markup_select_payment.add(types.InlineKeyboardButton("🪙 USDT (TRC20)", callback_data="pay_balance_USDT"))
        markup_select_payment.add(types.InlineKeyboardButton("🪙 BTC (Bitcoin)", callback_data="pay_balance_BTC"))
        markup_select_payment.add(types.InlineKeyboardButton("🪙 LTC (Litecoin)", callback_data="pay_balance_LTC"))
        markup_select_payment.add(types.InlineKeyboardButton("⬅️ Change Amount", callback_data="main_add_balance"))
        markup_select_payment.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

        sent_message_id = send_or_edit_message(
            bot_instance, chat_id, escape_md(confirmation_text),
            reply_markup=markup_select_payment,
            existing_message_id=existing_message_id,
            parse_mode="MarkdownV2"
        )
        if sent_message_id:
            update_user_state(user_id, 'last_bot_message_id', sent_message_id)

    except ValueError as e_val:
        logger.warning(f"User {user_id} entered invalid amount: {message_text}. Error: {e_val}")
        error_text = f"{escape_md(str(e_val))}\nPlease enter a valid positive amount (e\\.g\\., 10\\.00 or 25\\.50)\\."
        prompt_text_with_error = f"{error_text}\n\nPlease re-enter the EUR amount:"
        markup_retry = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
        sent_message_id_err = send_or_edit_message(
            bot_instance, chat_id, escape_md(prompt_text_with_error),
            reply_markup=markup_retry,
            existing_message_id=existing_message_id,
            parse_mode="MarkdownV2"
        )
        if sent_message_id_err:
            update_user_state(user_id, 'last_bot_message_id', sent_message_id_err)
    except Exception as e:
        logger.exception(f"Error in handle_amount_input_for_add_balance for user {user_id}: {e}")
        markup_back_to_main_err = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
        bot_instance.send_message(chat_id, "An unexpected error occurred. Please try again or return to the main menu.", reply_markup=markup_back_to_main_err)
        update_user_state(user_id, 'current_flow', None) # Reset flow


def handle_pay_balance_crypto_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    original_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    ack_msg = None

    try:
        crypto_currency_selected = call.data.split('pay_balance_')[1]
        if crypto_currency_selected.upper() not in ["USDT", "BTC", "LTC"]: raise IndexError("Invalid crypto")
        logger.info(f"User {user_id} selected {crypto_currency_selected} for adding balance (HD Wallet flow).")
    except IndexError:
        logger.warning(f"Error processing pay_balance_ callback for user {user_id}: {call.data}")
        bot_instance.answer_callback_query(call.id, "Error processing your selection.", show_alert=True)
        return

    requested_eur_float = get_user_state(user_id, 'add_balance_requested_eur')
    total_due_eur_float = get_user_state(user_id, 'add_balance_total_due_eur')

    if requested_eur_float is None or total_due_eur_float is None:
       logger.warning(f"Missing session data for pay_balance (HD Wallet) for user {user_id}.")
       error_text = "Your session seems to have expired or some data is missing. Please start over."
       markup_error = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
       if original_message_id:
           send_or_edit_message(bot_instance, chat_id, error_text, reply_markup=markup_error, existing_message_id=original_message_id, parse_mode="MarkdownV2")
       else:
           bot_instance.send_message(chat_id, error_text, reply_markup=markup_error, parse_mode="MarkdownV2")
       clear_user_state(user_id)
       bot_instance.answer_callback_query(call.id, "Error: Missing session data.", show_alert=True)
       return

    bot_instance.answer_callback_query(call.id)
    if original_message_id:
        ack_msg = send_or_edit_message(bot_instance, chat_id, escape_md("⏳ Generating your payment address..."), existing_message_id=original_message_id, reply_markup=None)
    else:
        ack_msg = bot_instance.send_message(chat_id, escape_md("⏳ Generating your payment address..."))
    current_message_id_for_invoice = ack_msg.message_id if ack_msg else original_message_id


    transaction_notes = f"User adding {requested_eur_float:.2f} EUR to balance. Total due: {total_due_eur_float:.2f} EUR via {crypto_currency_selected}."
    main_transaction_id = record_transaction(
        user_id=user_id, product_id=None, type='balance_top_up',
        eur_amount=total_due_eur_float,
        original_add_balance_amount=requested_eur_float, # Store the original amount user wanted to add
        payment_status='pending_address_generation',
        notes=transaction_notes,
        charge_id=None # No charge_id for HD Wallet
    )
    if not main_transaction_id:
        logger.error(f"HD Wallet: Failed to create transaction record for add balance, user {user_id}.")
        send_or_edit_message(bot_instance, chat_id, escape_md("Database error creating transaction. Please try again."), existing_message_id=current_message_id_for_invoice)
        return
    update_user_state(user_id, 'add_balance_transaction_id', main_transaction_id)

    coin_symbol_for_hd_wallet = crypto_currency_selected
    display_coin_symbol = crypto_currency_selected
    network_for_db = crypto_currency_selected

    if crypto_currency_selected == "USDT":
        coin_symbol_for_hd_wallet = "TRX"
        network_for_db = "TRC20 (Tron)"

    try:
        next_idx = get_next_address_index(coin_symbol_for_hd_wallet)
    except Exception as e_idx:
        logger.exception(f"HD Wallet: Error getting next address index for {coin_symbol_for_hd_wallet} (user {user_id}, tx {main_transaction_id}): {e_idx}")
        send_or_edit_message(bot_instance, chat_id, escape_md("Error generating payment address (index). Please try again later or contact support."), existing_message_id=current_message_id_for_invoice)
        update_transaction_status(main_transaction_id, 'error_address_generation')
        return

    unique_address = hd_wallet_utils.generate_address(coin_symbol_for_hd_wallet, next_idx)
    if not unique_address:
        logger.error(f"HD Wallet: Failed to generate address for {coin_symbol_for_hd_wallet}, index {next_idx} (user {user_id}, tx {main_transaction_id}).")
        send_or_edit_message(bot_instance, chat_id, escape_md("Error generating payment address (HD). Please try again later or contact support."), existing_message_id=current_message_id_for_invoice)
        update_transaction_status(main_transaction_id, 'error_address_generation')
        return

    rate = exchange_rate_utils.get_current_exchange_rate("EUR", display_coin_symbol)
    if not rate:
        logger.error(f"HD Wallet: Could not get exchange rate for EUR to {display_coin_symbol} (user {user_id}, tx {main_transaction_id}).")
        send_or_edit_message(bot_instance, chat_id, escape_md(f"Could not retrieve exchange rate for {escape_md(display_coin_symbol)}. Please try again or contact support."), existing_message_id=current_message_id_for_invoice, parse_mode='MarkdownV2')
        update_transaction_status(main_transaction_id, 'error_exchange_rate')
        return

    precision_map = {"BTC": 8, "LTC": 8, "USDT": 6}
    num_decimals = precision_map.get(display_coin_symbol, 8)
    total_due_eur_decimal = Decimal(str(total_due_eur_float))
    expected_crypto_amount_decimal_hr = (total_due_eur_decimal / rate).quantize(Decimal('1e-' + str(num_decimals)), rounding=ROUND_UP)
    smallest_unit_multiplier = Decimal('1e-' + str(num_decimals))
    expected_crypto_amount_smallest_unit_str = str(int(expected_crypto_amount_decimal_hr * smallest_unit_multiplier))

    payment_window_minutes = getattr(config, 'PAYMENT_WINDOW_MINUTES', 60)
    expires_at_dt = datetime.datetime.utcnow() + datetime.timedelta(minutes=payment_window_minutes)

    update_success = update_main_transaction_for_hd_payment(
       main_transaction_id,
       status='awaiting_payment',
       crypto_amount=str(expected_crypto_amount_decimal_hr),
       currency=display_coin_symbol
    )
    if not update_success:
        logger.error(f"HD Wallet: Failed to update main transaction {main_transaction_id} for add balance, user {user_id}.")
        send_or_edit_message(bot_instance, chat_id, escape_md("Database error updating transaction. Please try again."), existing_message_id=current_message_id_for_invoice)
        return

    db_coin_symbol_for_pending = "USDT_TRX" if crypto_currency_selected == "USDT" else display_coin_symbol
    pending_payment_id = create_pending_payment(
       transaction_id=main_transaction_id,
       user_id=user_id,
       address=unique_address,
       coin_symbol=db_coin_symbol_for_pending,
       network=network_for_db,
       expected_crypto_amount=expected_crypto_amount_smallest_unit_str,
       expires_at=expires_at_dt,
       paid_from_balance_eur=0.0
    )
    if not pending_payment_id:
       logger.error(f"HD Wallet: Failed to create pending_crypto_payment for add balance main_tx {main_transaction_id} (user {user_id}).")
       update_transaction_status(main_transaction_id, 'error_creating_pending_payment')
       send_or_edit_message(bot_instance, chat_id, escape_md("Error preparing payment record. Please try again or contact support."), existing_message_id=current_message_id_for_invoice)
       return

    qr_code_path = None
    try:
        qr_code_path = hd_wallet_utils.generate_qr_code_for_address(
           unique_address,
           str(expected_crypto_amount_decimal_hr),
           display_coin_symbol
        )
    except Exception as e_qr_gen:
        logger.error(f"HD Wallet (add balance): QR code generation failed for {unique_address} (user {user_id}, tx {main_transaction_id}): {e_qr_gen}")

    service_fee_display = total_due_eur_decimal - Decimal(str(requested_eur_float))
    invoice_text_md = (
        f"🧾 *INVOICE - Add Balance*\n\n"
        f"Amount to Add: *{Decimal(str(requested_eur_float)):.2f} EUR*\n"
        f"Service Fee: *{service_fee_display:.2f} EUR*\n"
        f"Total Due: *{total_due_eur_decimal:.2f} EUR*\n\n"
        f"🏦 *Payment Details*\n"
        f"Currency: *{escape_md(display_coin_symbol)}*\n"
        f"Network: *{escape_md(network_for_db)}*\n"
        f"Address: `{escape_md(unique_address)}`\n\n"
        f"*AMOUNT TO SEND:*\n`{escape_md(str(expected_crypto_amount_decimal_hr))} {escape_md(display_coin_symbol)}`\n\n"
        f"⏳ Expires: *{escape_md(expires_at_dt.strftime('%Y-%m-%d %H:%M:%S UTC'))}*\n\n"
        f"⚠️ Send the exact amount using the correct network. This address is for single use only."
    )

    markup_invoice = types.InlineKeyboardMarkup(row_width=1)
    markup_invoice.add(types.InlineKeyboardButton("✅ Check Payment", callback_data=f"check_bal_payment_{main_transaction_id}"))
    markup_invoice.add(types.InlineKeyboardButton("🚫 Cancel Payment", callback_data=f"cancel_bal_payment_{main_transaction_id}"))
    markup_invoice.add(types.InlineKeyboardButton("⬅️ Change Amount / Method", callback_data="main_add_balance"))

    if current_message_id_for_invoice:
         try: delete_message(bot_instance, chat_id, current_message_id_for_invoice)
         except Exception: pass

    sent_invoice_msg = None
    if qr_code_path and os.path.exists(qr_code_path):
        try:
            with open(qr_code_path, 'rb') as qr_photo:
                sent_invoice_msg = bot_instance.send_photo(chat_id, photo=qr_photo, caption=escape_md(invoice_text_md), reply_markup=markup_invoice, parse_mode="MarkdownV2")
        except Exception as e_qr_send:
            logger.error(f"HD Wallet (add balance): Failed to send QR code photo for {unique_address} (user {user_id}, tx {main_transaction_id}): {e_qr_send}. Sending text only.")
            sent_invoice_msg = bot_instance.send_message(chat_id, escape_md(invoice_text_md), reply_markup=markup_invoice, parse_mode="MarkdownV2")
        finally:
            if os.path.exists(qr_code_path):
                try: os.remove(qr_code_path)
                except Exception as e_rm_qr: logger.error(f"Failed to remove QR code file {qr_code_path}: {e_rm_qr}")
    else:
        logger.warning(f"HD Wallet (add balance): QR code not generated or not found for {unique_address} (user {user_id}, tx {main_transaction_id}). Sending text invoice.")
        sent_invoice_msg = bot_instance.send_message(chat_id, escape_md(invoice_text_md), reply_markup=markup_invoice, parse_mode="MarkdownV2")

    if sent_invoice_msg:
        update_user_state(user_id, 'last_bot_message_id', sent_invoice_msg.message_id)
    update_user_state(user_id, 'current_flow', 'add_balance_awaiting_hd_payment_confirmation')


def handle_check_add_balance_payment_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    ack_msg = None
    original_invoice_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    logger.info(f"User {user_id} checking add balance payment status for callback data: {call.data}")

    try:
        transaction_id_str = call.data.split('check_bal_payment_')[1]
        transaction_id = int(transaction_id_str)
    except (IndexError, ValueError):
        logger.warning(f"Invalid transaction ID in callback data for check_bal_payment: {call.data} for user {user_id}")
        bot_instance.answer_callback_query(call.id, "Error: Invalid transaction reference.", show_alert=True)
        return

    pending_payment_record = get_pending_payment_by_transaction_id(transaction_id)

    if not pending_payment_record:
        main_tx = get_transaction_by_id(transaction_id) # Corrected function call
        status_msg = "Payment record not found or already processed."
        if main_tx: status_msg = f"Payment status: {escape_md(main_tx['payment_status'])}."
        bot_instance.answer_callback_query(call.id, status_msg, show_alert=True)
        if main_tx and main_tx['payment_status'] in ['completed', 'cancelled_by_user', 'expired_payment_window', 'error_finalizing_data']:
            new_markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
    if original_invoice_message_id and call.message.message_id == original_invoice_message_id:
        try:
            if call.message.photo: bot_instance.edit_message_caption(caption=escape_md(status_msg), chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=new_markup, parse_mode="MarkdownV2")
            else: bot_instance.edit_message_text(text=escape_md(status_msg), chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=new_markup, parse_mode="MarkdownV2")
        except Exception as e_edit_final: logger.error(f"Error editing final state message {original_invoice_message_id} for user {user_id}, tx {transaction_id}: {e_edit_final}")
        return

    bot_instance.answer_callback_query(call.id)
    ack_msg = bot_instance.send_message(chat_id, escape_md("⏳ Checking payment status (on-demand)..."))

    try:
        newly_confirmed, status_info = payment_monitor.check_specific_pending_payment(transaction_id)
        if ack_msg: delete_message(bot_instance, chat_id, ack_msg.message_id)

        if newly_confirmed and status_info == 'confirmed_unprocessed':
            logger.info(f"On-demand check for add balance tx {transaction_id} (user {user_id}) resulted in new confirmation. Processing...")
            bot_instance.send_message(chat_id, escape_md("✅ Payment detected! Processing your balance update..."))
            payment_monitor.process_confirmed_payments(bot_instance)
        else:
            logger.info(f"On-demand check for add balance tx {transaction_id} (user {user_id}): newly_confirmed={newly_confirmed}, status_info='{status_info}'")
            pending_payment_latest = get_pending_payment_by_transaction_id(transaction_id)

            current_invoice_text = call.message.caption if call.message.photo else call.message.text
            base_invoice_text = "\n".join([line for line in (current_invoice_text or "").split('\n') if not line.strip().startswith("Status:")])

            new_status_line = ""
            alert_message = ""
            show_alert_flag = True
            reply_markup_to_use = call.message.reply_markup

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
                new_markup.add(types.InlineKeyboardButton("⬅️ Try Again", callback_data="main_add_balance"))
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
                if status_info != 'monitoring':
                     reply_markup_to_use = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

            updated_text_for_invoice = f"{new_status_line}\n\n{base_invoice_text}".strip()
            if len(updated_text_for_invoice) > (1024 if call.message.photo else 4096):
                updated_text_for_invoice = updated_text_for_invoice[:(1021 if call.message.photo else 4093)] + "..."

            try:
                if call.message.photo:
                    bot_instance.edit_message_caption(caption=escape_md(updated_text_for_invoice), chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=reply_markup_to_use, parse_mode="MarkdownV2")
                else:
                    bot_instance.edit_message_text(text=escape_md(updated_text_for_invoice), chat_id=chat_id, message_id=original_invoice_message_id, reply_markup=reply_markup_to_use, parse_mode="MarkdownV2")
                bot_instance.answer_callback_query(call.id, alert_message, show_alert=show_alert_flag)
            except Exception as e_edit:
                logger.error(f"Error editing message {original_invoice_message_id} for on-demand add balance check (tx {transaction_id}): {e_edit}")
                bot_instance.answer_callback_query(call.id, "Status updated, but message display failed to refresh.", show_alert=True)

    except Exception as e:
        logger.exception(f"Error in handle_check_add_balance_payment_callback (on-demand) for user {user_id}, tx {transaction_id_str}: {e}")
        if ack_msg and hasattr(ack_msg, 'message_id'): delete_message(bot_instance, chat_id, ack_msg.message_id)
        bot_instance.answer_callback_query(call.id, "An error occurred while checking payment status. Please try again.", show_alert=True)


def handle_cancel_add_balance_payment_callback(bot_instance, clear_user_state, get_user_state, update_user_state, call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    original_invoice_message_id = get_user_state(user_id, 'last_bot_message_id') or call.message.message_id
    logger.info(f"User {user_id} initiated cancel for add balance payment: {call.data}")

    try:
        transaction_id_str = call.data.split('cancel_bal_payment_')[1]
        transaction_id = int(transaction_id_str)
    except (IndexError, ValueError):
        logger.warning(f"Invalid transaction ID in cancel_bal_payment callback: {call.data} for user {user_id}")
        bot_instance.answer_callback_query(call.id, "Error: Invalid transaction reference.", show_alert=True)
        return

    user_cancel_message = "Payment process cancelled by user."

    pending_payment = get_pending_payment_by_transaction_id(transaction_id)
    if pending_payment:
        if pending_payment['status'] == 'monitoring':
            if update_pending_payment_status(pending_payment['payment_id'], 'user_cancelled'):
                logger.info(f"HD Pending Payment {pending_payment['payment_id']} for add balance TX_ID {transaction_id} marked as user_cancelled.")
                user_cancel_message = "Payment (HD Wallet) successfully cancelled."
            else:
                logger.error(f"Failed to update HD Pending Payment {pending_payment['payment_id']} for add balance TX_ID {transaction_id} status to user_cancelled.")
                user_cancel_message = "Payment cancellation processed, but there was an issue updating pending record."
        else:
            logger.info(f"HD Pending Payment {pending_payment['payment_id']} for add balance TX_ID {transaction_id} was not 'monitoring' (was {pending_payment['status']}). Main transaction will be cancelled.")
            user_cancel_message = f"Payment already in state '{pending_payment['status']}'. Marked as cancelled by you."
    else:
        logger.warning(f"No HD pending payment record found for add balance TX_ID {transaction_id} upon user cancellation. Main transaction will be marked cancelled.")

    if transaction_id:
        update_transaction_status(transaction_id, 'cancelled_by_user')
    else:
        logger.error(f"Cancel add balance callback triggered with no valid transaction_id from data: {call.data}")

    if original_invoice_message_id:
        try:
            delete_message(bot_instance, chat_id, original_invoice_message_id)
        except Exception as e_del:
            logger.error(f"Error deleting invoice message {original_invoice_message_id} on cancel for add balance, user {user_id}, tx {transaction_id}: {e_del}")

    user_cancel_message_final = user_cancel_message + "\nReturning to the main menu."
    markup_main_menu = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

    final_msg_sent = None
    try:
        final_msg_sent = bot_instance.send_message(chat_id, escape_md(user_cancel_message_final), reply_markup=markup_main_menu, parse_mode="MarkdownV2")
    except Exception as e_send:
        logger.error(f"Error sending cancel confirmation (Markdown) for add balance tx {transaction_id}: {e_send}. Sending plain text.")
        final_msg_sent = bot_instance.send_message(chat_id, user_cancel_message_final.replace('*','').replace('_','').replace('`','').replace('[','').replace(']','').replace('(','').replace(')',''), reply_markup=markup_main_menu)

    bot_instance.answer_callback_query(call.id, "Payment Cancelled.")

    clear_user_state(user_id)
    welcome_text, markup = get_main_menu_text_and_markup()
    new_main_menu_msg_id = send_or_edit_message(
        bot_instance, chat_id, welcome_text,
        reply_markup=markup,
        existing_message_id=None, # Send as a new message
        parse_mode=None
    )
    if new_main_menu_msg_id:
        update_user_state(user_id, 'last_bot_message_id', new_main_menu_msg_id)


# --- Payment Finalization Function (called by payment_monitor) ---
def finalize_successful_top_up(bot_instance, main_transaction_id: int, user_id: int,
                               original_add_balance_amount_str: str, # From main_transaction.original_add_balance_amount
                               received_crypto_amount_str: str,
                               coin_symbol: str,
                               blockchain_tx_id: str
                               ) -> bool:
    logger.info(f"Finalizing successful top-up for user {user_id}, main_tx_id {main_transaction_id}. Amount: {original_add_balance_amount_str}")
    chat_id = user_id

    try:
        try:
            original_add_balance_amount_decimal = Decimal(original_add_balance_amount_str).quantize(Decimal('0.01'))
        except Exception as e_dec:
            logger.error(f"finalize_successful_top_up: Invalid amount format '{original_add_balance_amount_str}' for tx {main_transaction_id}. Error: {e_dec}")
            update_transaction_status(main_transaction_id, 'error_finalizing_data')
            return False

        user_data = get_or_create_user(user_id)
        if not user_data:
            logger.error(f"finalize_successful_top_up: Failed to get/create user {user_id} for tx {main_transaction_id}.")
            update_transaction_status(main_transaction_id, 'error_finalizing_user_data')
            return False

        current_balance_decimal = Decimal(str(user_data['balance'])).quantize(Decimal('0.01'))
        new_balance_decimal = current_balance_decimal + original_add_balance_amount_decimal

        if not update_user_balance(user_id, float(new_balance_decimal), increment_transactions=True): # Main transaction already created, this increments user's total count
            logger.error(f"finalize_successful_top_up: Failed to update balance for user {user_id}, tx {main_transaction_id}.")
            update_transaction_status(main_transaction_id, 'error_finalizing_balance_update')
            return False

        if not update_transaction_status(main_transaction_id, 'completed'):
            logger.warning(f"finalize_successful_top_up: Failed to update main transaction {main_transaction_id} status to completed, but balance was updated for user {user_id}.")

        success_text = (f"✅ Payment confirmed for Transaction ID {main_transaction_id}\\!\n"
                        f"Your balance has been updated by *{original_add_balance_amount_decimal:.2f} EUR*\\.\n\n"
                        f"New balance: *{new_balance_decimal:.2f} EUR*")

        markup_main_menu = types.InlineKeyboardMarkup()
        markup_main_menu.add(types.InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))

        current_flow_state = get_user_state(user_id, 'current_flow')
        if current_flow_state and 'add_balance' in current_flow_state:
            clear_user_state(user_id)
            logger.info(f"finalize_successful_top_up: Cleared user state for user {user_id} after successful top-up {main_transaction_id}.")

        last_bot_msg_id_before_clear = get_user_state(user_id, 'last_bot_message_id')
        if last_bot_msg_id_before_clear:
            try:
                 delete_message(bot_instance, chat_id, last_bot_msg_id_before_clear)
                 logger.debug(f"finalize_successful_top_up: Deleted last bot message {last_bot_msg_id_before_clear} for user {user_id}.")
            except Exception as e_del_msg:
                logger.warning(f"finalize_successful_top_up: Could not delete last bot message for user {user_id}, tx {main_transaction_id}: {e_del_msg}")

        sent_msg = bot_instance.send_message(chat_id, escape_md(success_text), reply_markup=markup_main_menu, parse_mode="MarkdownV2")
        update_user_state(user_id, 'last_bot_message_id', sent_msg.message_id) # Store the new message ID
        logger.info(f"finalize_successful_top_up: Successfully processed top-up for user {user_id}, tx {main_transaction_id}. New balance: {new_balance_decimal:.2f} EUR.")
        return True

    except sqlite3.Error as e_sql:
        logger.exception(f"finalize_successful_top_up: SQLite error for user {user_id}, tx {main_transaction_id}: {e_sql}")
        update_transaction_status(main_transaction_id, 'error_finalizing_db')
        return False
    except Exception as e:
        logger.exception(f"finalize_successful_top_up: Unexpected error for user {user_id}, tx {main_transaction_id}: {e}")
        update_transaction_status(main_transaction_id, 'error_finalizing_unexpected')
        return False

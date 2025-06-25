import telebot
import os
import config
import sqlite3
from threading import Thread
import logging
import time # For scheduler
from modules import db_utils
from modules import payment_monitor # Import the new payment monitor
from modules.utils import update_user_state, get_user_state, clear_user_state
from modules import text_utils # Import text_utils

# Basic logging configuration
logging.basicConfig(
    level=logging.INFO,
    filename='bot_activity.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filemode='a'
)
logger = logging.getLogger(__name__)

# Bot token and admin ID
BOT_TOKEN = config.BOT_TOKEN
ADMIN_ID = config.ADMIN_ID

# --- Configuration ---
SERVICE_FEE_EUR = getattr(config, 'SERVICE_FEE_EUR', 0.50) # Default if not in config
ADD_BALANCE_SERVICE_FEE_EUR = getattr(config, 'ADD_BALANCE_SERVICE_FEE_EUR', 0.25) # Default if not in config


# Create bot instance
# Explicitly set threaded=False to see if it affects callback query handling
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# Initialize database
from modules.db_utils import initialize_database, initial_sync_filesystem_to_db
logger.info("Initializing database...")
initialize_database()
logger.info("Performing initial filesystem to DB sync...")
initial_sync_filesystem_to_db()
logger.info("Initial sync complete.")

# Validate HD Wallet Seed Phrase
from modules.hd_wallet_utils import validate_seed_phrase
if not validate_seed_phrase():
    logger.critical("CRITICAL: HD Wallet seed phrase is invalid or not configured properly. Payment functionalities will FAIL. Please check config.py and ensure SEED_PHRASE is a valid BIP39 mnemonic.")
    # Depending on desired behavior, you might want to exit or prevent the bot from fully starting here.
    # For now, it will log critically and continue, but payments will not work.
else:
    logger.info("HD Wallet seed phrase validated successfully.")

# Import handlers (keeping imports even if not directly used by decorators here)
logger.info("Importing handlers...")
from handlers import account_handler, buy_flow_handler, support_handler, admin_handler
from handlers.main_menu_handler import handle_start, handle_back_to_main_menu_callback
from handlers.add_balance_handler import (
    handle_add_balance_callback, handle_amount_input_for_add_balance,
    handle_pay_balance_crypto_callback, handle_check_add_balance_payment_callback,
    handle_cancel_add_balance_payment_callback
)
logger.info("Handlers imported.")

# Register handlers after bot instance is created
logger.info("Registering handlers...")

# Main Menu Handlers
@bot.message_handler(commands=['start'])
def start_command_wrapper(message):
    handle_start(bot, clear_user_state, get_user_state, update_user_state, message)

@bot.callback_query_handler(func=lambda call: call.data == 'back_to_main')
def back_to_main_callback_wrapper(call):
    handle_back_to_main_menu_callback(bot, clear_user_state, get_user_state, update_user_state, call)

# Other Main Menu Button Handlers (placeholders - need to confirm actual handler functions)
# Assuming handlers exist in respective imported modules and accept (bot, ..., call)
@bot.callback_query_handler(func=lambda call: call.data == 'buy_initiate')
def buy_initiate_callback_wrapper(call):
    # Need to find the actual handler function in handlers.buy_flow_handler
    # For now, a placeholder or a simple response
    logger.info(f"Buy initiate callback received from user {call.from_user.id}")
    bot.answer_callback_query(call.id, "Buy button clicked (handler not fully implemented yet)")
    # Example: handlers.buy_flow_handler.handle_buy_initiate(bot, ..., call)

@bot.callback_query_handler(func=lambda call: call.data == 'main_add_balance')
def add_balance_callback_wrapper(call):
    handle_add_balance_callback(bot, clear_user_state, get_user_state, update_user_state, call)

@bot.callback_query_handler(func=lambda call: call.data == 'main_account')
def account_callback_wrapper(call):
    # Need to find the actual handler function in handlers.account_handler
    logger.info(f"Account callback received from user {call.from_user.id}")
    bot.answer_callback_query(call.id, "Account button clicked (handler not fully implemented yet)")
    # Example: handlers.account_handler.handle_account(bot, ..., call)

@bot.callback_query_handler(func=lambda call: call.data == 'support_initiate')
def support_callback_wrapper(call):
    # Need to find the actual handler function in handlers.support_handler
    logger.info(f"Support initiate callback received from user {call.from_user.id}")
    bot.answer_callback_query(call.id, "Support button clicked (handler not fully implemented yet)")
    # Example: handlers.support_handler.handle_support_initiate(bot, ..., call)


# Custom update listener to log all incoming updates
def handle_updates(updates):
    for update in updates:
        logger.info(f"Received update: {update}")
        # You could add logic here to manually process updates if needed
        # For now, just logging to see what updates are received

# --- Scheduled Tasks ---
def scheduled_ticket_expiration_check():
    logger.info("Scheduler: Ticket expiration check thread started.")
    # Use getattr for config values with defaults
    init_delay = getattr(config, 'SCHEDULER_INIT_DELAY_TICKET_EXPIRY_SECONDS', 10)
    interval = getattr(config, 'SCHEDULER_INTERVAL_TICKET_EXPIRY_SECONDS', 3600)
    logger.info(f"Ticket Expiry: Initial delay {init_delay}s, Interval {interval}s")
    time.sleep(init_delay)
    while True:
        current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        logger.info(f"Scheduler: Running ticket expiration check at {current_time_str} UTC...")
        try:
            expired_ticket_details_list = db_utils.expire_old_tickets()
            if expired_ticket_details_list:
                logger.info(f"Scheduler: Found {len(expired_ticket_details_list)} tickets to auto-expire.")
                for ticket_info in expired_ticket_details_list:
                    try:
                        user_id_to_notify = ticket_info['user_id']
                        ticket_id_expired = ticket_info['ticket_id']
                        bot.send_message(user_id_to_notify,
                                         f"Your support ticket #{ticket_id_expired} has been automatically closed due to 24 hours of inactivity. "
                                         f"If you still need help, please open a new ticket by sending another message in the support channel.")
                        logger.info(f"Scheduler: Notified user {user_id_to_notify} about auto-expired ticket {ticket_id_expired}.")
                        if config.ADMIN_ID and str(config.ADMIN_ID).strip():
                            try:
                                admin_id_int = int(config.ADMIN_ID)
                                bot.send_message(admin_id_int, f"Ticket #{ticket_id_expired} (User {user_id_to_notify}) was auto-expired due to inactivity.")
                                logger.info(f"Scheduler: Notified admin {admin_id_int} about auto-expired ticket {ticket_id_expired}.")
                            except ValueError: logger.error(f"Scheduler: ADMIN_ID '{config.ADMIN_ID}' is not valid int for auto-expiry notice of ticket #{ticket_id_expired}.")
                            except Exception as e_admin: logger.error(f"Scheduler: Failed to send auto-expiration notice for ticket #{ticket_id_expired} to ADMIN_ID {config.ADMIN_ID}: {e_admin}")
                    except Exception as e_notify: logger.error(f"Scheduler: Error notifying user {user_id_to_notify} about auto-expired ticket #{ticket_id_expired}: {e_notify}")
            else:
                logger.info(f"Scheduler: No tickets for auto-expiration at {current_time_str} UTC.")
        except Exception as e_task: logger.exception(f"Scheduler: Critical error in ticket expiration task: {e_task}")
        time.sleep(interval)

def scheduled_item_sync():
    logger.info("Scheduler: Item availability sync thread started.")
    init_delay = getattr(config, 'SCHEDULER_INIT_DELAY_ITEM_SYNC_SECONDS', 20)
    interval = getattr(config, 'SCHEDULER_INTERVAL_ITEM_SYNC_SECONDS', 3600) # Default 1 hour
    logger.info(f"Item Sync: Initial delay {init_delay}s, Interval {interval}s")
    time.sleep(init_delay)
    while True:
        current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        logger.info(f"Scheduler: Running item availability sync at {current_time_str} UTC...")
        try:
            if hasattr(db_utils, 'periodic_filesystem_to_db_sync'):
                sync_summary = db_utils.periodic_filesystem_to_db_sync()
                logger.info(f"Scheduler: Item sync finished. Summary: {sync_summary}")
            else:
                logger.error("Scheduler: periodic_filesystem_to_db_sync function not found in db_utils.")
                time.sleep(3600 * 24) # Sleep long if function missing
                continue
        except Exception as e: logger.exception(f"Scheduler: Critical error in item availability sync task: {e}")
        time.sleep(interval)

# --- HD Wallet Payment Monitoring Tasks ---
def scheduled_check_pending_crypto_payments():
    logger.info("Scheduler: Pending crypto payment check thread started.")
    init_delay = getattr(config, 'SCHEDULER_INIT_DELAY_PAYMENT_CHECK_SECONDS', 30)
    interval = getattr(config, 'SCHEDULER_INTERVAL_PAYMENT_CHECK_SECONDS', 120) # Default 2 minutes
    if interval < 60: logger.warning(f"Payment check interval {interval}s is very frequent. Consider increasing.")
    logger.info(f"Pending Payment Check: Initial delay {init_delay}s, Interval {interval}s")
    time.sleep(init_delay)
    while True:
        logger.info("Scheduler: Running pending crypto payment check...")
        try:
            payment_monitor.check_pending_payments()
        except Exception as e:
            logger.exception("Scheduler: Critical error in check_pending_payments task.")
        time.sleep(interval)

def scheduled_process_confirmed_crypto_payments():
    logger.info("Scheduler: Process confirmed crypto payment thread started.")
    init_delay = getattr(config, 'SCHEDULER_INIT_DELAY_PROCESS_CONFIRMED_SECONDS', 15)
    interval = getattr(config, 'SCHEDULER_INTERVAL_PROCESS_CONFIRMED_SECONDS', 60) # Default 1 minute
    logger.info(f"Process Confirmed Payments: Initial delay {init_delay}s, Interval {interval}s")
    time.sleep(init_delay)
    while True:
        logger.info("Scheduler: Running process confirmed crypto payments...")
        try:
            payment_monitor.process_confirmed_payments(bot)
        except Exception as e:
            logger.exception("Scheduler: Critical error in process_confirmed_payments task.")
        time.sleep(interval)

def scheduled_expire_stale_crypto_payments():
    logger.info("Scheduler: Expire stale crypto payment thread started.")
    init_delay = getattr(config, 'SCHEDULER_INIT_DELAY_EXPIRE_PAYMENTS_SECONDS', 60)
    interval = getattr(config, 'SCHEDULER_INTERVAL_EXPIRE_PAYMENTS_SECONDS', 300) # Default 5 minutes
    logger.info(f"Expire Stale Payments: Initial delay {init_delay}s, Interval {interval}s")
    time.sleep(init_delay)
    while True:
        logger.info("Scheduler: Running expire stale crypto payments...")
        try:
            payment_monitor.expire_stale_monitoring_payments(bot)
        except Exception as e:
            logger.exception("Scheduler: Critical error in expire_stale_monitoring_payments task.")
        time.sleep(interval)

# Main function
def start_bot():
    logger.info("Bot starting...")

    # Existing scheduled tasks
    logger.info("Starting scheduled ticket expiration check thread...")
    expiration_thread = Thread(target=scheduled_ticket_expiration_check, daemon=True)
    expiration_thread.start()

    logger.info("Scheduler: Starting item availability sync thread...")
    item_sync_thread = Thread(target=scheduled_item_sync, daemon=True)
    item_sync_thread.start()

    # New HD Wallet payment monitoring tasks
    logger.info("Starting scheduled pending crypto payment check thread...")
    pending_crypto_check_thread = Thread(target=scheduled_check_pending_crypto_payments, daemon=True)
    pending_crypto_check_thread.start()

    logger.info("Starting scheduled process confirmed crypto payment thread...")
    process_confirmed_crypto_thread = Thread(target=scheduled_process_confirmed_crypto_payments, daemon=True)
    process_confirmed_crypto_thread.start()

    logger.info("Starting scheduled expire stale crypto payment thread...")
    expire_stale_crypto_thread = Thread(target=scheduled_expire_stale_crypto_payments, daemon=True)
    expire_stale_crypto_thread.start()

    logger.info("Starting Telegram bot polling...")
    bot.delete_webhook() # Ensure no webhook is active before polling
    try:
        # Use a custom update listener to log all incoming updates
        bot.set_update_listener(handle_updates)
        bot.infinity_polling(timeout=10) # Poll for all update types
    except Exception as e_poll:
        logger.critical(f"Bot polling failed critically: {e_poll}", exc_info=True)
    finally:
        logger.info("Bot polling stopped.")

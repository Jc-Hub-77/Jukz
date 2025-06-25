import sqlite3
import json
import config
import os
import datetime
import logging

logger = logging.getLogger(__name__)

try:
    from modules.utils import clear_user_state
except ImportError:
    logger.warning("Could not import clear_user_state from bot. May indicate circular dependency or bot not running in main context.")
    def clear_user_state(user_id):
        logger.info(f"Dummy clear_user_state called for {user_id} due to import issue.")
        pass

DATABASE_NAME = config.DATABASE_NAME

def get_db_connection():
    db_dir = os.path.dirname(DATABASE_NAME)
    logger.info(f"Attempting to connect to database at: {DATABASE_NAME}")
    logger.info(f"Database directory is: {db_dir}")
    logger.info(f"Database directory exists: {os.path.exists(db_dir)}")
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info(f"Created database directory: {db_dir}")
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_database():
    conn = get_db_connection()
    cursor = conn.cursor()
    logger.info("Initializing database schema for HD wallet integration...")

    conn.execute('BEGIN')
    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                transaction_count INTEGER DEFAULT 0
            )
        ''')
        logger.debug("Users table ensured.")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                price REAL NOT NULL,
                image_paths TEXT,
                is_available BOOLEAN DEFAULT TRUE,
                folder_path TEXT UNIQUE NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        logger.debug("Products table ensured.")

        logger.info("Processing 'transactions' table schema for HD wallet.")
        cursor.execute("DROP TABLE IF EXISTS transactions_old_for_hd_migration_temp")
        transactions_table_exists = False
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions';")
        if cursor.fetchone():
            transactions_table_exists = True

        if transactions_table_exists:
            try:
                cursor.execute("PRAGMA table_info(transactions)")
                columns_info = cursor.fetchall()
                column_names = [info['name'] for info in columns_info]
                if 'charge_id' in column_names or 'payment_address' in column_names:
                    cursor.execute("ALTER TABLE transactions RENAME TO transactions_old_for_hd_migration_temp")
                    logger.info("Renamed existing 'transactions' table to 'transactions_old_for_hd_migration_temp'. Manual data review/migration may be needed.")
                else:
                    logger.info("'transactions' table already seems to have the new schema or is empty. No rename needed.")
            except sqlite3.OperationalError as e:
                logger.warning(f"Could not rename 'transactions' table (it might not exist or another issue occurred): {e}")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER,
                type TEXT NOT NULL,
                eur_amount REAL NOT NULL,
                crypto_amount TEXT,
                currency TEXT,
                payment_status TEXT DEFAULT 'pending' NOT NULL,
                original_add_balance_amount REAL,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (product_id) REFERENCES products (product_id) ON DELETE SET NULL
            )
        ''')
        logger.debug("Transactions table ensured (new schema: charge_id, payment_address removed).")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hd_address_indices (
                coin_symbol TEXT PRIMARY KEY,
                last_used_index INTEGER DEFAULT -1 NOT NULL
            )
        ''')
        logger.debug("hd_address_indices table ensured.")
        initial_coins = ['BTC', 'LTC', 'TRX']
        for coin in initial_coins:
            cursor.execute("INSERT OR IGNORE INTO hd_address_indices (coin_symbol, last_used_index) VALUES (?, -1)", (coin,))
        logger.info(f"Seeded hd_address_indices with: {', '.join(initial_coins)} (if not already present).")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_crypto_payments (
                payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                address TEXT UNIQUE NOT NULL,
                coin_symbol TEXT NOT NULL,
                network TEXT,
                expected_crypto_amount TEXT NOT NULL,
                received_crypto_amount TEXT,
                status TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                last_checked_at DATETIME,
                expires_at DATETIME NOT NULL,
                blockchain_tx_id TEXT,
                confirmations INTEGER DEFAULT 0 NOT NULL,
                paid_from_balance_eur REAL DEFAULT 0.0 NOT NULL,
                FOREIGN KEY (transaction_id) REFERENCES transactions (transaction_id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        logger.debug("pending_crypto_payments table ensured.")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_payments_status ON pending_crypto_payments (status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_payments_address ON pending_crypto_payments (address);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_payments_transaction_id ON pending_crypto_payments (transaction_id);")
        logger.debug("Indexes for pending_crypto_payments ensured.")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                status TEXT DEFAULT 'open' NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_message_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                messages_json TEXT,
                admin_chat_id INTEGER,
                admin_ticket_view_message_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        logger.debug("Support tickets table ensured.")

        conn.commit()
        logger.info("Database schema initialization and HD wallet table setup complete.")
    except sqlite3.Error as e:
        logger.exception("Error during database schema initialization for HD wallet integration. Rolling back.")
        conn.rollback()
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    initialize_database()
    logger.info(f"Database '{DATABASE_NAME}' initialized successfully via direct script run.")

def get_or_create_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    if user is None:
        logger.info(f"User {user_id} not found, creating new user.")
        cursor.execute("INSERT INTO users (user_id, balance, transaction_count) VALUES (?, 0.0, 0)", (user_id,))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
    conn.close()
    return user

def clear_user_process(user_id):
    logger.info(f"Clearing process state for user ID {user_id} via clear_user_state.")
    clear_user_state(user_id)
    logger.debug(f"User state for {user_id} should have been cleared.")
    pass

# --- HD Wallet Specific Functions ---
def get_next_address_index(coin_symbol: str) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO hd_address_indices (coin_symbol, last_used_index) VALUES (?, -1)", (coin_symbol,))
        conn.execute('BEGIN IMMEDIATE')
        cursor.execute("SELECT last_used_index FROM hd_address_indices WHERE coin_symbol = ?", (coin_symbol,))
        row = cursor.fetchone()
        if row is None:
            logger.critical(f"HD Wallet Index: Coin symbol {coin_symbol} still not found after INSERT OR IGNORE.")
            conn.rollback()
            raise sqlite3.OperationalError(f"Could not find or initialize index for coin {coin_symbol}. Critical error.")
        current_index = row['last_used_index']
        new_index = current_index + 1
        cursor.execute("UPDATE hd_address_indices SET last_used_index = ? WHERE coin_symbol = ?", (new_index, coin_symbol))
        conn.commit()
        logger.info(f"HD Wallet Index: Next index for {coin_symbol} is {new_index} (old was {current_index}).")
        return new_index
    except sqlite3.Error as e:
        logger.exception(f"HD Wallet Index: Database error for {coin_symbol}: {e}")
        if conn:
            try: conn.rollback()
            except sqlite3.Error as rb_err:
                logger.exception(f"HD Wallet Index: Error during rollback for {coin_symbol}: {rb_err}")
        raise
    finally:
        if conn: conn.close()

# --- Pending Crypto Payments CRUD ---
def create_pending_payment(transaction_id: int, user_id: int, address: str, coin_symbol: str,
                           network: str | None, expected_crypto_amount: str, expires_at: datetime.datetime,
                           paid_from_balance_eur: float = 0.0, status: str = 'monitoring') -> int | None:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_iso = datetime.datetime.utcnow().isoformat()
    expires_at_iso = expires_at.isoformat()
    try:
        cursor.execute("""
            INSERT INTO pending_crypto_payments
            (transaction_id, user_id, address, coin_symbol, network, expected_crypto_amount, paid_from_balance_eur, status, created_at, last_checked_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (transaction_id, user_id, address, coin_symbol, network, expected_crypto_amount, paid_from_balance_eur, status, now_iso, now_iso, expires_at_iso))
        payment_id = cursor.lastrowid
        conn.commit()
        logger.info(f"Created pending payment record ID {payment_id} for main tx {transaction_id}, address {address}, paid_from_balance_eur: {paid_from_balance_eur}.")
        return payment_id
    except sqlite3.Error as e:
        logger.exception(f"Failed to create pending payment for main tx {transaction_id}, address {address}: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()

def get_pending_payments_to_monitor(limit: int = 100) -> list[sqlite3.Row]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT * FROM pending_crypto_payments
            WHERE status = 'monitoring' AND datetime('now', 'utc') < expires_at
            ORDER BY last_checked_at ASC NULLS FIRST, created_at ASC
            LIMIT ?
        """, (limit,))
        payments = cursor.fetchall()
        logger.debug(f"Fetched {len(payments)} pending payments to monitor.")
        return payments
    except sqlite3.Error as e:
        logger.exception(f"Failed to fetch pending payments to monitor: {e}")
        return []
    finally:
        conn.close()

def update_pending_payment_check_details(payment_id: int, confirmations: int, received_amount: str | None = None, blockchain_tx_id: str | None = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_iso = datetime.datetime.utcnow().isoformat()
    try:
        if received_amount is not None and blockchain_tx_id is not None:
            cursor.execute("""
                UPDATE pending_crypto_payments
                SET last_checked_at = ?, confirmations = ?, received_crypto_amount = ?, blockchain_tx_id = ?
                WHERE payment_id = ?
            """, (now_iso, confirmations, received_amount, blockchain_tx_id, payment_id))
        else:
            cursor.execute("""
                UPDATE pending_crypto_payments
                SET last_checked_at = ?, confirmations = ?
                WHERE payment_id = ?
            """, (now_iso, confirmations, payment_id))
        conn.commit()
        logger.info(f"Updated check details for pending payment ID {payment_id}. Confirmations: {confirmations}.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"Failed to update check details for pending payment ID {payment_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def update_pending_payment_status(payment_id: int, new_status: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_iso = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("""
            UPDATE pending_crypto_payments
            SET status = ?, last_checked_at = ?
            WHERE payment_id = ?
        """, (new_status, now_iso, payment_id))
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"Updated status for pending payment ID {payment_id} to {new_status}.")
        else:
            logger.warning(f"No pending payment found with ID {payment_id} to update status to {new_status}.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"Failed to update status for pending payment ID {payment_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def get_confirmed_unprocessed_payments(limit: int = 100) -> list[sqlite3.Row]:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT * FROM pending_crypto_payments
            WHERE status = 'confirmed_unprocessed'
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,))
        payments = cursor.fetchall()
        logger.debug(f"Fetched {len(payments)} confirmed_unprocessed payments.")
        return payments
    except sqlite3.Error as e:
        logger.exception(f"Failed to fetch confirmed_unprocessed payments: {e}")
        return []
    finally:
        conn.close()

def get_pending_payment_by_transaction_id(transaction_id: int) -> sqlite3.Row | None:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM pending_crypto_payments WHERE transaction_id = ?", (transaction_id,))
        payment = cursor.fetchone()
        return payment
    except sqlite3.Error as e:
        logger.exception(f"Failed to fetch pending payment by transaction_id {transaction_id}: {e}")
        return None
    finally:
        conn.close()

def get_pending_payment_by_address(address: str) -> sqlite3.Row | None:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM pending_crypto_payments WHERE address = ?", (address,))
        payment = cursor.fetchone()
        return payment
    except sqlite3.Error as e:
        logger.exception(f"Failed to fetch pending payment by address {address}: {e}")
        return None
    finally:
        conn.close()

def get_stale_monitoring_payments(limit: int = 100) -> list[sqlite3.Row]:
    """Fetches 'monitoring' payments that have passed their 'expires_at' time."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Using datetime('now', 'utc') for SQLite to compare with stored ISO8601 strings
        # Ensure expires_at is stored in a format comparable by SQLite's datetime functions
        cursor.execute("""
            SELECT payment_id, user_id, transaction_id, address
            FROM pending_crypto_payments
            WHERE status = 'monitoring' AND datetime('now', 'utc') >= expires_at
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,))
        payments = cursor.fetchall()
        logger.debug(f"Fetched {len(payments)} stale monitoring payments.")
        return payments
    except sqlite3.Error as e:
        logger.exception(f"Failed to fetch stale monitoring payments: {e}")
        return []
    finally:
        conn.close()

# --- General Product and Transaction Functions ---
def get_cities_with_available_items():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT city FROM products WHERE is_available = TRUE ORDER BY city ASC")
    rows = cursor.fetchall()
    conn.close()
    return [row['city'] for row in rows]

def get_available_items_in_city(city_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT product_id, name
        FROM products
        WHERE city = ? AND is_available = TRUE
        ORDER BY name ASC
    """, (city_name,))
    items = [{'product_id': row['product_id'], 'name': row['name']} for row in cursor.fetchall()]
    conn.close()
    return items

def get_product_details_by_id(product_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
    product = cursor.fetchone()
    conn.close()
    return product

def update_user_balance(user_id, new_balance, increment_transactions=True):
    conn = get_db_connection()
    cursor = conn.cursor()
    if increment_transactions:
        cursor.execute("""
            UPDATE users
            SET balance = ?, transaction_count = transaction_count + 1
            WHERE user_id = ?
        """, (new_balance, user_id))
    else:
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
    conn.commit()
    conn.close()
    logger.info(f"User {user_id} balance updated to {new_balance:.2f}. Transactions incremented: {increment_transactions}")

def record_transaction(user_id, product_id, type, eur_amount,  # Removed charge_id
                       crypto_amount=None, currency=None, payment_status='pending',
                       original_add_balance_amount=None, notes=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO transactions
                (user_id, product_id, type, eur_amount, crypto_amount, currency,
                 payment_status, original_add_balance_amount, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (user_id, product_id, type, eur_amount, crypto_amount, currency,
              payment_status, original_add_balance_amount, notes))
        transaction_id = cursor.lastrowid
        conn.commit()
        logger.info(f"Transaction recorded: ID {transaction_id} for user {user_id}, type {type}, status {payment_status}")
        return transaction_id
    except sqlite3.Error as e:
        logger.exception(f"Failed to record transaction for user {user_id}, type {type}: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()

def get_transaction_by_id(transaction_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,))
    transaction = cursor.fetchone()
    conn.close()
    return transaction

def update_transaction_status(transaction_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE transactions
            SET payment_status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
        """, (status, transaction_id))
        conn.commit()
        if cursor.rowcount == 0:
            logger.warning(f"update_transaction_status did not update any row for TXID {transaction_id}.")
        else:
            logger.info(f"Transaction {transaction_id} status updated to {status}.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"Failed to update transaction status for TXID {transaction_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_open_ticket_for_user(user_id):
    logger.debug(f"Checking for open ticket for user_id: {user_id}")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM support_tickets WHERE user_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    ticket = cursor.fetchone()
    conn.close()
    if ticket: logger.debug(f"Open ticket for user {user_id}: {ticket['ticket_id']}")
    else: logger.debug(f"No open ticket for user {user_id}")
    return ticket

def create_new_ticket(user_id, initial_message_text, user_tg_message_id=None):
    logger.info(f"Creating new ticket for user {user_id}. Initial message snippet: {initial_message_text[:50]}")
    current_time_iso = datetime.datetime.utcnow().isoformat()
    message_obj = {
        "sender": "user", "text": initial_message_text, "timestamp": current_time_iso,
    }
    if user_tg_message_id is not None:
         message_obj["user_tg_message_id"] = user_tg_message_id
    messages_json_str = json.dumps([message_obj])

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO support_tickets (user_id, status, created_at, last_message_at, messages_json)
               VALUES (?, 'open', ?, ?, ?)""",
            (user_id, current_time_iso, current_time_iso, messages_json_str)
        )
        ticket_id = cursor.lastrowid
        conn.commit()
        logger.info(f"New ticket {ticket_id} created for user {user_id}.")
        return ticket_id
    except sqlite3.Error as e:
        logger.exception(f"SQLite error creating new ticket for user {user_id}: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()

def add_message_to_ticket(ticket_id, sender_type, message_text, user_tg_message_id=None, admin_tg_message_id=None):
    logger.info(f"Adding message to ticket {ticket_id}. Sender: {sender_type}, Text snippet: {message_text[:50]}")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT messages_json FROM support_tickets WHERE ticket_id = ?", (ticket_id,))
        row = cursor.fetchone()
        if row is None:
            logger.error(f"Ticket {ticket_id} not found when trying to add message.")
            return False

        messages_list = json.loads(row['messages_json']) if row['messages_json'] else []
        current_time_iso_msg = datetime.datetime.utcnow().isoformat()
        new_message_obj = {"sender": sender_type, "text": message_text, "timestamp": current_time_iso_msg}
        if user_tg_message_id: new_message_obj["user_tg_message_id"] = user_tg_message_id
        if admin_tg_message_id: new_message_obj["admin_tg_message_id"] = admin_tg_message_id
        messages_list.append(new_message_obj)

        current_time_iso_ticket_update = datetime.datetime.utcnow().isoformat()
        cursor.execute(
            "UPDATE support_tickets SET messages_json = ?, last_message_at = ? WHERE ticket_id = ?",
            (json.dumps(messages_list), current_time_iso_ticket_update, ticket_id)
        )
        conn.commit()
        success = cursor.rowcount > 0
        if success: logger.debug(f"Message added to ticket {ticket_id} and committed.")
        else: logger.warning(f"Failed to add message to ticket {ticket_id} (rowcount 0 after update).")
        return success
    except sqlite3.Error as e:
        logger.exception(f"SQLite error adding message to ticket {ticket_id}: {e}")
        conn.rollback()
        return False
    except json.JSONDecodeError as e_json:
        logger.exception(f"JSON error processing messages for ticket {ticket_id}: {e_json}")
        return False
    finally:
        conn.close()

def get_all_open_tickets_admin():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticket_id, user_id, messages_json, last_message_at, status FROM support_tickets WHERE status = 'open' ORDER BY last_message_at ASC")
    tickets = cursor.fetchall()
    conn.close()
    logger.debug(f"Fetched {len(tickets)} open tickets for admin.")
    return tickets

def get_ticket_details_by_id(ticket_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM support_tickets WHERE ticket_id = ?", (ticket_id,))
    ticket = cursor.fetchone()
    conn.close()
    return ticket

def update_ticket_status(ticket_id, new_status):
    conn = get_db_connection()
    cursor = conn.cursor()
    current_time_iso = datetime.datetime.utcnow().isoformat()
    try:
        cursor.execute("UPDATE support_tickets SET status = ?, last_message_at = ? WHERE ticket_id = ?", (new_status, current_time_iso, ticket_id))
        conn.commit()
        updated_rows = cursor.rowcount
        if updated_rows > 0: logger.info(f"Ticket {ticket_id} status updated to {new_status}.")
        else: logger.warning(f"No ticket found with ID {ticket_id} to update status to {new_status}.")
        return updated_rows > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error updating status for ticket {ticket_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def update_admin_ticket_view_message_id(ticket_id, message_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE support_tickets SET admin_ticket_view_message_id = ? WHERE ticket_id = ?", (message_id, ticket_id))
        conn.commit()
        if cursor.rowcount > 0: logger.debug(f"Admin view message ID {message_id} stored for ticket {ticket_id}.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error updating admin_ticket_view_message_id for ticket {ticket_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def get_all_products_admin():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT product_id, city, name, price, is_available FROM products ORDER BY city ASC, name ASC")
    products = cursor.fetchall()
    conn.close()
    logger.debug(f"Fetched {len(products)} products for admin list.")
    return products

def expire_old_tickets():
    conn = get_db_connection()
    cursor = conn.cursor()
    twenty_four_hours_ago_iso = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat()
    expired_details = []
    try:
        cursor.execute("SELECT ticket_id, user_id FROM support_tickets WHERE status = 'open' AND last_message_at < ?", (twenty_four_hours_ago_iso,))
        tickets_to_expire = cursor.fetchall()
        if tickets_to_expire:
            logger.info(f"Found {len(tickets_to_expire)} tickets to auto-expire.")
            closure_time = datetime.datetime.utcnow().isoformat()
            for ticket in tickets_to_expire:
                cursor.execute("UPDATE support_tickets SET status = 'auto_expired', last_message_at = ? WHERE ticket_id = ?", (closure_time, ticket['ticket_id']))
                expired_details.append({'ticket_id': ticket['ticket_id'], 'user_id': ticket['user_id']})
            conn.commit()
    except sqlite3.Error as e:
        logger.exception(f"SQLite error in expire_old_tickets: {e}")
        conn.rollback()
    finally:
        conn.close()
    return expired_details

def periodic_filesystem_to_db_sync():
    logger.info(f"Periodic_sync: Starting periodic filesystem to DB sync at {datetime.datetime.utcnow().isoformat()} UTC...")
    from modules import file_system_utils
    actions_summary = {"marked_unavailable_folder_gone": 0, "marked_unavailable_no_instances": 0, "newly_added": 0, "errors": 0, "reactivated": 0, "details_updated": 0}
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT product_id, folder_path, is_available FROM products")
        db_products_info = {row['folder_path']: {'id': row['product_id'], 'is_available': bool(row['is_available'])} for row in cursor.fetchall()}
    except sqlite3.Error as e:
        logger.exception(f"ERROR:Periodic_sync: DB error fetching products: {e}")
        actions_summary["errors"] += 1
        conn.close()
        return actions_summary

    fs_product_folder_paths = set()
    try:
        cities = file_system_utils.get_cities()
        for city in cities:
            product_type_names = file_system_utils.get_items_in_city(city)
            for pt_name in product_type_names:
                fs_product_folder_paths.add(os.path.join(config.ITEMS_BASE_DIR, city, pt_name))
    except Exception as e:
        logger.exception(f"ERROR:Periodic_sync: Filesystem scan error: {e}")
        actions_summary["errors"] += 1

    for product_folder_path in fs_product_folder_paths:
        relative_path = os.path.relpath(product_folder_path, config.ITEMS_BASE_DIR)
        parts = relative_path.split(os.sep)
        if len(parts) != 2:
             logger.warning(f"WARNING:Periodic_sync: Could not derive city/product_type from path: {product_folder_path} (parts: {parts})")
             continue
        city_name, product_type_name = parts[0], parts[1]

        was_db_available = db_products_info.get(product_folder_path, {}).get('is_available')
        sync_item_from_fs_to_db(city_name, product_type_name, product_folder_path, default_price=0.0)

        cursor.execute("SELECT is_available FROM products WHERE folder_path = ?", (product_folder_path,))
        updated_product_row = cursor.fetchone()
        if not updated_product_row:
            actions_summary["errors"] += 1
            logger.error(f"ERROR:Periodic_sync: Product disappeared from DB after sync attempt: {product_folder_path}")
            continue
        current_fs_is_available = updated_product_row['is_available']

        if product_folder_path not in db_products_info:
            actions_summary["newly_added"] += 1
            logger.info(f"Periodic_sync: Newly added: {product_folder_path}, Available: {current_fs_is_available}")
        else:
            if current_fs_is_available and not was_db_available:
                actions_summary["reactivated"] += 1
                logger.info(f"Periodic_sync: Reactivated: {product_folder_path}")
            elif not current_fs_is_available and was_db_available:
                actions_summary["marked_unavailable_no_instances"] += 1
                logger.info(f"Periodic_sync: Marked unavailable (no instances): {product_folder_path}")
            elif current_fs_is_available and was_db_available:
                actions_summary["details_updated"] += 1

    db_only_paths = set(db_products_info.keys()) - fs_product_folder_paths
    for path_to_mark_gone in db_only_paths:
        if db_products_info[path_to_mark_gone]['is_available']:
            if mark_item_as_unavailable_in_db(path_to_mark_gone):
                actions_summary["marked_unavailable_folder_gone"] += 1
                logger.info(f"Periodic_sync: Marked unavailable (folder gone): {path_to_mark_gone}")
            else:
                actions_summary["errors"] += 1; logger.error(f"ERROR:Periodic_sync: Failed to mark unavailable (folder gone): {path_to_mark_gone}")
    conn.close()
    logger.info(f"Periodic_sync: Sync completed. Summary: {actions_summary}")
    return actions_summary

def update_product_availability(product_id, new_status_bool):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE products SET is_available = ?, updated_at = CURRENT_TIMESTAMP WHERE product_id = ?", (new_status_bool, product_id))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Availability for product {product_id} updated to {new_status_bool}.")
        else: logger.warning(f"Product {product_id} not found during availability update.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error updating availability for product {product_id}: {e}")
        conn.rollback(); return False
    finally: conn.close()

def update_product_name(product_id, new_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE products SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE product_id = ?", (new_name, product_id))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Name for product {product_id} updated to '{new_name}'.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error updating name for product {product_id}: {e}")
        conn.rollback(); return False
    finally: conn.close()

def update_product_description(product_id, new_description):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE products SET description = ?, updated_at = CURRENT_TIMESTAMP WHERE product_id = ?", (new_description, product_id))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Description for product {product_id} updated.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error updating description for product {product_id}: {e}")
        conn.rollback(); return False
    finally: conn.close()

def update_product_price(product_id, new_price):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE products SET price = ?, updated_at = CURRENT_TIMESTAMP WHERE product_id = ?", (new_price, product_id))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Price for product {product_id} updated to {new_price:.2f}.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error updating price for product {product_id}: {e}")
        conn.rollback(); return False
    finally: conn.close()

def update_product_image_paths(product_id, new_image_paths_json_string):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE products SET image_paths = ?, updated_at = CURRENT_TIMESTAMP WHERE product_id = ?", (new_image_paths_json_string, product_id))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Image paths for product {product_id} updated.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error updating image_paths for product {product_id}: {e}")
        conn.rollback(); return False
    finally: conn.close()

def delete_product_by_id(product_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Product {product_id} deleted successfully from database.")
        else: logger.warning(f"No product found with ID {product_id} to delete from database.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"SQLite error deleting product {product_id}: {e}")
        conn.rollback(); return False
    finally: conn.close()

from modules import file_system_utils

def sync_item_from_fs_to_db(city_name: str, product_type_name: str, product_folder_path: str, default_price: float = 0.0):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        instances = file_system_utils.get_product_instances(product_folder_path)
        is_available_on_fs = len(instances) > 0
        fs_display_details = file_system_utils.get_item_details(city_name, product_type_name)
        description_for_db = fs_display_details.get('description', "No description available.") if fs_display_details else "No description available."
        image_paths_list_for_db = fs_display_details.get('image_paths', []) if fs_display_details else []
        image_paths_json_for_db = json.dumps(image_paths_list_for_db)

        cursor.execute("SELECT product_id, price FROM products WHERE folder_path = ?", (product_folder_path,))
        product_in_db = cursor.fetchone()

        if product_in_db:
            cursor.execute("""
                UPDATE products SET city = ?, name = ?, description = ?, image_paths = ?, is_available = ?, updated_at = CURRENT_TIMESTAMP
                WHERE folder_path = ?""",
                (city_name, product_type_name, description_for_db, image_paths_json_for_db, is_available_on_fs, product_folder_path))
            logger.info(f"SYNC_FS_DB: Updated product type: {product_type_name} in {city_name}. Available: {is_available_on_fs}.")
        else:
            cursor.execute("""
                INSERT INTO products (city, name, description, price, image_paths, is_available, folder_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (city_name, product_type_name, description_for_db, default_price, image_paths_json_for_db, is_available_on_fs, product_folder_path))
            logger.info(f"SYNC_FS_DB: Inserted new product type: {product_type_name} in {city_name}. Price: {default_price:.2f}. Available: {is_available_on_fs}.")
        conn.commit()
    except sqlite3.Error as e:
        logger.exception(f"SYNC_FS_DB: SQLite error for product {product_folder_path}: {e}")
        conn.rollback()
    except Exception as e_gen: # Catch other potential errors like FS issues if not caught by file_system_utils
        logger.exception(f"SYNC_FS_DB: General error for product {product_folder_path}: {e_gen}")
    finally:
        conn.close()


def mark_item_as_unavailable_in_db(folder_path: str) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE products SET is_available = FALSE, updated_at = CURRENT_TIMESTAMP WHERE folder_path = ?", (folder_path,))
        conn.commit()
        if cursor.rowcount > 0: logger.info(f"Marked product with folder_path '{folder_path}' as unavailable.")
        else: logger.warning(f"No product found with folder_path '{folder_path}' to mark as unavailable.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"Error marking item as unavailable in DB for {folder_path}: {e}")
        conn.rollback(); return False
    finally: conn.close()

def increment_user_transaction_count(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET transaction_count = transaction_count + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"Incremented transaction count for user {user_id}.")
        else:
            logger.warning(f"Attempted to increment transaction count, but user {user_id} not found.")
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.exception(f"DB error incrementing transaction count for user {user_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn: conn.close()

def update_main_transaction_for_hd_payment(transaction_id: int, status: str, crypto_amount: str, currency: str) -> bool:
    """
    Updates an existing main transaction record with crypto payment details for HD wallet payments.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE transactions
            SET payment_status = ?,
                crypto_amount = ?,
                currency = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
        """, (status, crypto_amount, currency, transaction_id))
        conn.commit()
        if cursor.rowcount == 0:
            logger.warning(f"update_main_transaction_for_hd_payment: No transaction found with ID {transaction_id} to update.")
            return False
        else:
            logger.info(f"Main transaction {transaction_id} updated for HD payment. Status: {status}, Crypto: {crypto_amount} {currency}.")
            return True
    except sqlite3.Error as e:
        logger.exception(f"Failed to update main transaction {transaction_id} for HD payment: {e}")
        conn.rollback()
        return False
    finally:
        if conn: conn.close()

def initial_sync_filesystem_to_db():
    logger.info("Starting initial filesystem to DB sync...")
    db_conn_temp = get_db_connection()
    cursor_temp = db_conn_temp.cursor()
    try:
        cursor_temp.execute("SELECT folder_path FROM products")
        db_all_product_folder_paths = {row['folder_path'] for row in cursor_temp.fetchall()}
    except sqlite3.Error as e:
        logger.exception("Initial_sync: DB error fetching all product paths.")
        db_conn_temp.close()
        return # Cannot proceed without this
    db_conn_temp.close()

    fs_current_product_folder_paths = set()
    try:
        cities = file_system_utils.get_cities()
        for city in cities:
            product_type_names = file_system_utils.get_items_in_city(city)
            for product_type_name in product_type_names:
                product_folder_path = os.path.join(config.ITEMS_BASE_DIR, city, product_type_name)
                fs_current_product_folder_paths.add(product_folder_path)
                logger.debug(f"INITIAL_SYNC: Processing FS item: Path '{product_folder_path}'")
                sync_item_from_fs_to_db(city_name=city, product_type_name=product_type_name, product_folder_path=product_folder_path, default_price=0.0)
    except Exception as e_fs_scan: # Catch potential errors during FS scan or initial sync calls
        logger.exception(f"INITIAL_SYNC: Error during filesystem scan or item sync: {e_fs_scan}")

    paths_in_db_but_not_in_fs = db_all_product_folder_paths - fs_current_product_folder_paths
    logger.info(f"INITIAL_SYNC: Found {len(paths_in_db_but_not_in_fs)} paths in DB but not in FS to mark unavailable.")
    for product_folder_path_to_mark_unavailable in paths_in_db_but_not_in_fs:
        logger.info(f"INITIAL_SYNC: Marking as unavailable (folder gone): {product_folder_path_to_mark_unavailable}")
        mark_item_as_unavailable_in_db(product_folder_path_to_mark_unavailable)

    logger.info("Initial filesystem to DB sync completed.")


def get_user_transaction_history(user_id: int, limit: int = 5, offset: int = 0) -> list[sqlite3.Row]:
    """
    Fetches a paginated transaction history for a given user.
    Joins with products table to get product names for purchases.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # The COALESCE(p.name, 'N/A') handles cases where product_id is NULL (e.g., balance top-up)
        # or if a product was somehow deleted but transaction remains.
        query = """
            SELECT
                t.transaction_id,
                t.type,
                t.eur_amount,
                t.crypto_amount,
                t.currency,
                t.payment_status,
                t.notes,
                t.created_at,
                t.original_add_balance_amount,
                p.name AS product_name
            FROM transactions t
            LEFT JOIN products p ON t.product_id = p.product_id
            WHERE t.user_id = ?
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
        """
        cursor.execute(query, (user_id, limit, offset))
        transactions = cursor.fetchall()
        logger.debug(f"Fetched {len(transactions)} transactions for user {user_id} with limit {limit}, offset {offset}.")
        return transactions
    except sqlite3.Error as e:
        logger.exception(f"Failed to fetch transaction history for user {user_id}: {e}")
        return []
    finally:
        if conn:
            conn.close()

# --- Admin Item Management DB Functions ---

def add_product_type(city: str, name: str, price: float, folder_path: str, description: str | None = None, image_paths_json: str | None = None, initial_quantity: int = 0) -> int | None:
    """
    Adds a new product type to the 'products' table.
    The quantity is determined by the number of instances found by sync_item_from_fs_to_db.
    is_available will also be set by the sync based on instance availability.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # is_available and quantity will be updated by sync_item_from_fs_to_db
        # For initial insert, we can assume is_available=False until sync confirms instances.
        # Or, if an instance was just created, initial_quantity could be 1.
        # Let's rely on sync_item_from_fs_to_db to set correct availability and derive quantity.
        # The 'description' and 'image_paths' in products table are from the *oldest instance*.
        # These will also be populated by sync_item_from_fs_to_db.
        cursor.execute("""
            INSERT INTO products (city, name, price, folder_path, description, image_paths, is_available, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (city, name, price, folder_path, description, image_paths_json, (initial_quantity > 0))) # True if initial quantity > 0
        product_id = cursor.lastrowid
        conn.commit()
        logger.info(f"Product type '{name}' in city '{city}' added to DB with ID {product_id}. Path: {folder_path}. Price: {price:.2f}. Initial Quantity: {initial_quantity}")

        # Immediately sync this new product to update its details from FS (like description from instance)
        if product_id:
            sync_item_from_fs_to_db(city, name, folder_path, default_price=price)
            logger.info(f"Initial sync performed for newly added product ID {product_id}.")

        return product_id
    except sqlite3.IntegrityError as e_int:
        logger.error(f"DB IntegrityError adding product '{name}' in '{city}' (folder_path: {folder_path}): {e_int}. Likely duplicate folder_path.")
        conn.rollback()
        return None
    except sqlite3.Error as e:
        logger.exception(f"DB error adding product type '{name}' in '{city}': {e}")
        conn.rollback()
        return None
    finally:
        if conn: conn.close()

def update_product_details(product_id: int, name: str | None = None, price: float | None = None, city: str | None = None) -> bool:
    """Updates name, price, or city for a given product_id."""
    conn = get_db_connection()
    cursor = conn.cursor()
    fields_to_update = []
    params = []

    if name is not None:
        fields_to_update.append("name = ?")
        params.append(name)
    if price is not None:
        fields_to_update.append("price = ?")
        params.append(price)
    if city is not None:
        fields_to_update.append("city = ?")
        params.append(city)

    if not fields_to_update:
        logger.warning("update_product_details called with no fields to update.")
        return False

    fields_to_update.append("updated_at = CURRENT_TIMESTAMP")
    query = f"UPDATE products SET {', '.join(fields_to_update)} WHERE product_id = ?"
    params.append(product_id)

    try:
        cursor.execute(query, tuple(params))
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"Product {product_id} details updated. Fields: {fields_to_update[:-1]}")
            return True
        logger.warning(f"Product {product_id} not found for update or no changes made.")
        return False
    except sqlite3.Error as e:
        logger.exception(f"DB error updating product {product_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn: conn.close()


def delete_product_type_db_record(product_id: int) -> bool:
    """
    Deletes a product type record from the database.
    This should be called AFTER the corresponding folder is deleted from the filesystem.
    Transactions referencing this product_id will have product_id set to NULL due to FOREIGN KEY ON DELETE SET NULL.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM products WHERE product_id = ?", (product_id,))
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"Product record with ID {product_id} deleted from database.")
            return True
        logger.warning(f"No product record found with ID {product_id} to delete.")
        return False
    except sqlite3.Error as e:
        logger.exception(f"DB error deleting product record {product_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn: conn.close()

# get_all_products_admin is already defined and seems suitable.
# update_product_availability is also already defined.
# sync_item_from_fs_to_db will handle quantity updates based on actual instances.
# A direct quantity update function might be risky if it desyncs with FS.
# For deleting, the flow would be:
# 1. Admin confirms delete.
# 2. Call file_system_utils.delete_item_folder_by_path (for product type or instance).
# 3. If successful, call db_utils.delete_product_type_db_record (if deleting whole type)
#    OR trigger a sync_item_from_fs_to_db for the parent product type (if deleting an instance).
#    The sync function will then update quantity/availability or remove the product if no instances left.

def get_all_users_admin(limit: int = 10, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
    """
    Fetches a paginated list of all users for admin view.
    Returns a list of user rows and the total count of users.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    total_users = 0
    try:
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        query = """
            SELECT user_id, balance, transaction_count
            FROM users
            ORDER BY user_id ASC
            LIMIT ? OFFSET ?
        """
        cursor.execute(query, (limit, offset))
        users = cursor.fetchall()
        logger.debug(f"Fetched {len(users)} users for admin view. Total users: {total_users}.")
        return users, total_users
    except sqlite3.Error as e:
        logger.exception(f"Failed to fetch all users for admin: {e}")
        return [], 0
    finally:
        if conn:
            conn.close()

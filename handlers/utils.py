import datetime
from modules import text_utils

TX_HISTORY_PAGE_SIZE = 5

def format_transaction_history_display(transactions: list) -> str:
    """Formats a list of transaction rows for display."""
    if not transactions:
        return "\n\nNo transaction history found\\."

    history_lines = ["\n\n📜 *Recent Transactions*"]
    for tx in transactions:
        try:
            created_at_dt = datetime.datetime.fromisoformat(tx['created_at'])
            date_str = text_utils.escape_md(created_at_dt.strftime("%Y-%m-%d %H:%M"))
        except (ValueError, TypeError):
            date_str = text_utils.escape_md(str(tx['created_at']))


        tx_type_display = str(tx['type']).replace('_', ' ').title()
        if tx_type_display == "Balance Top Up":
            tx_type_display = "Balance Added"
        elif tx_type_display == "Purchase Balance":
            tx_type_display = "Item Purchase (Balance)"
        elif tx_type_display == "Purchase Crypto":
            tx_type_display = "Item Purchase (Crypto)"

        tx_type_display_escaped = text_utils.escape_md(tx_type_display)
        details = f"{date_str} \\- *{tx_type_display_escaped}*"

        amount_to_display = tx['eur_amount']
        sign = ""

        if tx['type'] == 'balance_top_up':
            if tx['payment_status'] == 'completed':
                sign = "\\+"
                amount_to_display = tx['original_add_balance_amount'] if tx['original_add_balance_amount'] is not None else tx['eur_amount']
            else:
                sign = ""
        elif 'purchase' in tx['type']:
            sign = "\\-"

        amount_to_display_abs = abs(amount_to_display if amount_to_display is not None else 0.0)
        details += f": {sign}{amount_to_display_abs:.2f} EUR"

        if tx['product_name'] and tx['product_name'] != 'N/A':
            details += f" \\(_Item: {text_utils.escape_md(tx['product_name'])}_\\)"

        status_escaped = text_utils.escape_md(str(tx['payment_status']).replace('_', ' ').title())
        details += f" \\| Status: _{status_escaped}_"

        history_lines.append(details)

    if not history_lines[1:]:
        return "\n\nNo transaction history found for this page\\."
    return "\n".join(history_lines)

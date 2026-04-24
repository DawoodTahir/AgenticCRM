import re
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from agents.lead_agent import ask
from logger import get_logger

load_dotenv()

log = get_logger("bot")

ALLOWED_USER_IDS = []  # Leave empty to allow anyone, or add your Telegram user ID

SAFE_REPLY = "I can only help with CRM and sales questions for Goldenberry Farms."

# --- Layer 2: Input guard — blocks injection BEFORE gpt-4o sees it ---
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)",
    r"you\s+are\s+now",
    r"pretend\s+(you\s+are|to\s+be)",
    r"act\s+as\s+(a|an)",
    r"new\s+instructions",
    r"system\s*prompt",
    r"reveal\s+(your|the)\s+(prompt|instructions|rules)",
    r"what\s+(are|is)\s+your\s+(instructions|prompt|rules|system)",
    r"(run|execute|write)\s+(sql|code|query|command)",
    r"drop\s+table",
    r"delete\s+from",
    r"INSERT\s+INTO",
    r"UPDATE\s+.*\s+SET",
]

def is_injection(text: str) -> bool:
    text_lower = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


# --- Layer 3: Output guard — strips leaked internals BEFORE user sees it ---
SENSITIVE_TERMS = [
    "contact_embeddings", "lead_notes", "sync_state", "monday_item_id",
    "content_hash", "monday_lead_id", "raw_column_values", "psycopg2",
    "SELECT ", "INSERT ", "UPDATE ", "DELETE ", "FROM contacts",
    "FROM leads", "cur.execute", "conn.cursor",
]

def sanitize_output(text: str) -> str:
    for term in SENSITIVE_TERMS:
        if term.lower() in text.lower():
            log.warning("output_blocked", leaked_term=term)
            return SAFE_REPLY
    return text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your CRM assistant for Goldenberry Farms.\n\n"
        "Ask me anything about your leads, e.g.:\n"
        "• Which leads are LATE?\n"
        "• Who should I follow up with today?\n"
        "• Tell me about Ben Hartman\n"
        "• Give me a pipeline summary"
    )


def parse_contact_buttons(answer: str):
    """Check if the agent response has multiple contact matches. Return buttons if so."""
    if "Multiple contacts found" not in answer:
        return None

    # Parse lines like "- Ali Khan (ali@company.com)"
    matches = re.findall(r"^- (.+?)(?:\s*\(.*?\))?$", answer, re.MULTILINE)
    if len(matches) < 2:
        return None

    buttons = []
    for name in matches:
        name = name.strip()
        buttons.append([InlineKeyboardButton(name, callback_data=f"contact:{name}")])

    return InlineKeyboardMarkup(buttons)


async def handle_contact_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle when user taps a contact button."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("contact:"):
        return

    selected_name = data.replace("contact:", "")
    thread_id = str(query.message.chat_id)

    await query.message.reply_text(f"Looking up {selected_name}...")

    try:
        answer = ask(f"I mean {selected_name}. Give me all info about them.", thread_id=thread_id)
        if len(answer) <= 4096:
            await query.message.reply_text(answer)
        else:
            for i in range(0, len(answer), 4096):
                await query.message.reply_text(answer[i:i+4096])
    except Exception as e:
        await query.message.reply_text(f"Error: {str(e)}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Optional: restrict to specific users
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("Unauthorized.")
        return

    question = update.message.text
    thread_id = str(update.effective_chat.id)

    log.info("user_input", user_id=user_id, thread_id=thread_id, question=question)

    # Layer 2 — block injection before it reaches the agent
    if is_injection(question):
        log.warning("injection_blocked", user_id=user_id, question=question)
        await update.message.reply_text(SAFE_REPLY)
        return

    await update.message.reply_text("Thinking...")

    try:
        answer = ask(question, thread_id=thread_id)

        # Layer 3 — filter output before it reaches the user
        answer = sanitize_output(answer)

        log.info("bot_output", thread_id=thread_id, answer=answer[:500])

        # Check if agent returned multiple contact matches — show buttons
        keyboard = parse_contact_buttons(answer)
        if keyboard:
            await update.message.reply_text(answer, reply_markup=keyboard)
            return

        # Telegram has 4096 char limit — split if needed
        if len(answer) <= 4096:
            await update.message.reply_text(answer)
        else:
            for i in range(0, len(answer), 4096):
                await update.message.reply_text(answer[i:i+4096])
    except Exception as e:
        log.error("bot_error", thread_id=thread_id, error=str(e))
        await update.message.reply_text(f"Error: {str(e)}")


def run_bot():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_contact_selection))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Render sets RENDER=true and PORT automatically
    # Use webhook on Render, polling locally
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    port = int(os.environ.get("PORT", 8443))

    if render_url:
        print(f"Bot starting in WEBHOOK mode on port {port}...")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{render_url}/{token}",
        )
    else:
        print("Bot starting in POLLING mode (local dev)...")
        app.run_polling()


if __name__ == "__main__":
    run_bot()



import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"AI PDF Helper Bot is running")

    def log_message(self, format, *args):
        return


def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📄 PDF Tools", callback_data="pdf"),
            InlineKeyboardButton("🤖 AI Tools", callback_data="ai"),
        ],
        [
            InlineKeyboardButton("⭐ Premium", callback_data="premium"),
            InlineKeyboardButton("ℹ️ Help", callback_data="help"),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🤖 AI PDF Helper မှ ကြိုဆိုပါတယ်!\n\n"
        "PDF နဲ့ File တွေကို လွယ်လွယ်ကူကူ ပြုပြင်နိုင်ပြီး\n"
        "AI Tools တွေကိုလည်း အသုံးပြုနိုင်ပါတယ်။\n\n"
        "အောက်က Menu ကနေ ရွေးချယ်ပါ 👇",
        reply_markup=reply_markup,
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if query.data == "pdf":
        await query.edit_message_text(
            "📄 PDF Tools\n\n"
            "မကြာခင် ရရှိမယ့် Features:\n\n"
            "• PDF → Text\n"
            "• Image → PDF\n"
            "• PDF Merge\n"
            "• PDF Split\n"
            "• PDF Compress"
        )

    elif query.data == "ai":
        await query.edit_message_text(
            "🤖 AI Tools\n\n"
            "မကြာခင် ရရှိမယ့် Features:\n\n"
            "• AI Summarize\n"
            "• AI Translate\n"
            "• AI Writing\n"
            "• PDF AI Chat"
        )

    elif query.data == "premium":
        await query.edit_message_text(
            "⭐ Premium\n\n"
            "Premium Features မကြာခင် ရရှိပါမယ်။\n\n"
            "• More file limits\n"
            "• Advanced AI\n"
            "• Faster processing\n"
            "• Premium PDF tools"
        )

    elif query.data == "help":
        await query.edit_message_text(
            "ℹ️ Help\n\n"
            "/start — Main Menu\n\n"
            "PDF/File feature တွေကို အသုံးပြုဖို့\n"
            "PDF Tools ကိုရွေးပါ။"
        )


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN မတွေ့ပါ")

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()

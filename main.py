import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


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
    await update.message.reply_text(
        "🤖 AI PDF Helper\n\n"
        "📄 PDF / File Tools\n"
        "🧠 AI Tools\n"
        "⭐ Premium\n\n"
        "Bot is online!"
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

    print("Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()

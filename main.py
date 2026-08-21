import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pypdf import PdfReader


BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))


# =========================
# Render Health Check
# =========================

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


# =========================
# /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton(
                "📄 PDF Tools",
                callback_data="pdf"
            ),
            InlineKeyboardButton(
                "🤖 AI Tools",
                callback_data="ai"
            ),
        ],
        [
            InlineKeyboardButton(
                "⭐ Premium",
                callback_data="premium"
            ),
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help"
            ),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🤖 AI PDF Helper မှ ကြိုဆိုပါတယ်!\n\n"
        "PDF နဲ့ File တွေကို လွယ်လွယ်ကူကူ ပြုပြင်နိုင်ပါတယ်။\n\n"
        "အောက်က Menu ကနေ ရွေးချယ်ပါ 👇",
        reply_markup=reply_markup
    )


# =========================
# Button Handler
# =========================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if query.data == "pdf":

        keyboard = [
            [
                InlineKeyboardButton(
                    "📄 PDF → Text",
                    callback_data="pdf_to_text"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="back"
                )
            ],
        ]

        await query.edit_message_text(
            "📄 PDF Tools\n\n"
            "အောက်က Tool ကိုရွေးပါ 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "pdf_to_text":

        await query.edit_message_text(
            "📄 PDF → Text\n\n"
            "PDF ဖိုင်တစ်ခု ပို့ပေးပါ။\n\n"
            "PDF ထဲက စာသားတွေကို ထုတ်ပြီး TXT ဖိုင်အဖြစ် ပြန်ပေးပါမယ်။"
        )

        context.user_data["waiting_for_pdf"] = True

    elif query.data == "ai":

        await query.edit_message_text(
            "🤖 AI Tools\n\n"
            "AI Features မကြာခင် ထည့်ပေးပါမယ်။"
        )

    elif query.data == "premium":

        await query.edit_message_text(
            "⭐ Premium\n\n"
            "Premium system မကြာခင် ထည့်ပေးပါမယ်။"
        )

    elif query.data == "help":

        await query.edit_message_text(
            "ℹ️ Help\n\n"
            "/start — Main Menu\n\n"
            "📄 PDF Tools မှာ PDF → Text ကို စမ်းနိုင်ပါတယ်။"
        )

    elif query.data == "back":

        await start(update, context)


# =========================
# PDF Handler
# =========================

async def handle_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get("waiting_for_pdf"):
        return

    document = update.message.document

    if not document:
        return

    if not document.file_name.lower().endswith(".pdf"):

        await update.message.reply_text(
            "❌ PDF ဖိုင်ပဲ ပို့ပေးပါ။"
        )

        return

    await update.message.reply_text(
        "⏳ PDF ကို ဖတ်နေပါတယ်..."
    )

    try:

        file = await context.bot.get_file(
            document.file_id
        )

        input_path = f"/tmp/{document.file_name}"

        await file.download_to_drive(
            input_path
        )

        reader = PdfReader(input_path)

        text = ""

        for page in reader.pages:

            page_text = page.extract_text()

            if page_text:
                text += page_text + "\n"

        if not text.strip():

            await update.message.reply_text(
                "❌ ဒီ PDF ထဲမှာ Extract လုပ်လို့ရတဲ့ စာသားမတွေ့ပါ။\n\n"
                "Scanned/Image PDF ဖြစ်နိုင်ပါတယ်။"
            )

            os.remove(input_path)

            return

        output_path = f"/tmp/{document.file_name}.txt"

        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(text)

        await update.message.reply_document(
            document=output_path,
            caption="✅ PDF → Text ပြီးပါပြီ။"
        )

        os.remove(input_path)
        os.remove(output_path)

        context.user_data["waiting_for_pdf"] = False

    except Exception as e:

        print("PDF ERROR:", e)

        await update.message.reply_text(
            "❌ PDF processing မအောင်မြင်ပါ။\n"
            "နောက်တစ်ကြိမ် ပြန်စမ်းကြည့်ပါ။"
        )


# =========================
# Main
# =========================

def main():

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN မတွေ့ပါ"
        )

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Document.PDF,
            handle_pdf
        )
    )

    print("Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()

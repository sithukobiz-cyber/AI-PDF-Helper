import os
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import fitz
from pypdf import PdfReader

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

OCR_URL = "https://api.ocr.space/parse/image"


# =========================
# Render Health Check
# =========================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            b"AI PDF Helper Bot is running"
        )

    def log_message(self, format, *args):
        return


def start_web_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    server.serve_forever()


# =========================
# Main Menu
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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

    await update.message.reply_text(
        "🤖 AI PDF Helper မှ ကြိုဆိုပါတယ်!\n\n"
        "📄 PDF / File Tools\n"
        "🧠 AI Tools\n"
        "⭐ Premium\n\n"
        "အောက်က Menu ကိုရွေးပါ 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
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
            "PDF → Text ကိုရွေးပါ 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "pdf_to_text":

        context.user_data["waiting_for_pdf"] = True

        await query.edit_message_text(
            "📄 PDF → Text\n\n"
            "PDF ဖိုင်တစ်ခု ပို့ပေးပါ။\n\n"
            "စာသားပါတဲ့ PDF ဆိုရင် ပုံမှန် Extract လုပ်မယ်။\n"
            "Scanned PDF ဆိုရင် OCR သုံးပြီး မြန်မာစာပါ ဖတ်ပေးမယ်။"
        )

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
            "📄 PDF Tools → PDF → Text"
        )

    elif query.data == "back":

        await query.message.delete()

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Main Menu ကို ပြန်သွားဖို့ /start ကိုနှိပ်ပါ။"
        )


# =========================
# Normal PDF Text Extraction
# =========================

def extract_pdf_text(pdf_path):

    try:

        reader = PdfReader(pdf_path)

        text = ""

        for page in reader.pages:

            page_text = page.extract_text()

            if page_text:
                text += page_text + "\n"

        return text.strip()

    except Exception as e:

        print("PDF extraction error:", e)

        return ""


# =========================
# OCR
# =========================

def ocr_image(image_path):

    if not OCR_API_KEY:
        raise Exception(
            "OCR_API_KEY မတွေ့ပါ"
        )

    with open(
        image_path,
        "rb"
    ) as image_file:

        response = requests.post(
            OCR_URL,
            files={
                "file": image_file
            },
            data={
                "apikey": OCR_API_KEY,
                "language": "eng",
                "OCREngine": "3",
                "isOverlayRequired": "false",
            },
            timeout=120
        )

    if response.status_code != 200:

        raise Exception(
            f"OCR API HTTP {response.status_code}"
        )

    result = response.json()

    if result.get("IsErroredOnProcessing"):

        error_message = result.get(
            "ErrorMessage",
            "Unknown OCR error"
        )

        raise Exception(
            str(error_message)
        )

    parsed_results = result.get(
        "ParsedResults",
        []
    )

    text = ""

    for item in parsed_results:

        parsed_text = item.get(
            "ParsedText",
            ""
        )

        if parsed_text:
            text += parsed_text + "\n"

    return text.strip()


# =========================
# PDF OCR Processing
# =========================

def process_pdf_with_ocr(pdf_path):

    pdf = fitz.open(pdf_path)

    all_text = []

    try:

        for page_number in range(len(pdf)):

            print(
                f"OCR page {page_number + 1}"
            )

            page = pdf[page_number]

            pix = page.get_pixmap(
                matrix=fitz.Matrix(1.5, 1.5),
                alpha=False
            )

            image_path = tempfile.mktemp(
                suffix=".png"
            )

            pix.save(image_path)

            try:

                text = ocr_image(
                    image_path
                )

                if text:
                    all_text.append(
                        f"\n--- Page {page_number + 1} ---\n"
                    )

                    all_text.append(text)

            finally:

                if os.path.exists(
                    image_path
                ):
                    os.remove(image_path)

    finally:

        pdf.close()

    return "\n".join(all_text).strip()


# =========================
# PDF Handler
# =========================

async def handle_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get(
        "waiting_for_pdf"
    ):
        return

    document = update.message.document

    if not document:
        return

    filename = document.file_name or ""

    if not filename.lower().endswith(".pdf"):

        await update.message.reply_text(
            "❌ PDF ဖိုင်ပဲ ပို့ပေးပါ။"
        )

        return

    processing_message = (
        await update.message.reply_text(
            "⏳ PDF ကို စစ်နေပါတယ်..."
        )
    )

    input_path = None
    output_path = None

    try:

        telegram_file = (
            await context.bot.get_file(
                document.file_id
            )
        )

        input_path = tempfile.mktemp(
            suffix=".pdf"
        )

        await telegram_file.download_to_drive(
            input_path
        )

        # First try normal text extraction
        text = extract_pdf_text(
            input_path
        )

        # If almost no useful text exists,
        # use OCR
        if len(text.strip()) < 100:

            await processing_message.edit_text(
                "🔎 Scanned PDF ဖြစ်နိုင်ပါတယ်။\n"
                "🧠 OCR နဲ့ စာဖတ်နေပါတယ်..."
            )

            text = process_pdf_with_ocr(
                input_path
            )

        if not text.strip():

            await processing_message.edit_text(
                "❌ PDF ထဲက စာကို မဖတ်နိုင်ပါ။"
            )

            return

        output_path = tempfile.mktemp(
            suffix=".txt"
        )

        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as output_file:

            output_file.write(text)

        await processing_message.delete()

        await update.message.reply_document(
            document=output_path,
            caption="✅ PDF → Text ပြီးပါပြီ။"
        )

        context.user_data[
            "waiting_for_pdf"
        ] = False

    except Exception as e:

        print(
            "PDF PROCESSING ERROR:",
            repr(e)
        )

        try:

            await processing_message.edit_text(
                "❌ PDF processing မအောင်မြင်ပါ။\n\n"
                f"Error: {str(e)[:500]}"
            )

        except Exception:
            pass

    finally:

        if input_path and os.path.exists(
            input_path
        ):
            os.remove(input_path)

        if output_path and os.path.exists(
            output_path
        ):
            os.remove(output_path)


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

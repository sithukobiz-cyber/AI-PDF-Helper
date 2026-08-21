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


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

OCR_URL = "https://api.ocr.space/parse/image"


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()

        self.wfile.write(
            b"AI PDF Helper Bot is running"
        )

    def log_message(self, format, *args):
        return


def start_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(
        f"Health server running on port {PORT}"
    )

    server.serve_forever()


# =========================================================
# MAIN MENU
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup([
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
    ])


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "🤖 AI PDF Helper မှ ကြိုဆိုပါတယ်!\n\n"

        "📄 PDF / File Tools\n"
        "🧠 AI Tools\n"
        "⭐ Premium\n\n"

        "အောက်က Menu ကိုရွေးပါ 👇",

        reply_markup=main_keyboard()
    )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


    # -----------------------------------------------------
    # PDF TOOLS
    # -----------------------------------------------------

    if query.data == "pdf":

        keyboard = InlineKeyboardMarkup([

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
            ]

        ])


        await query.edit_message_text(

            "📄 PDF Tools\n\n"

            "PDF → Text ကိုရွေးပါ 👇",

            reply_markup=keyboard
        )


    # -----------------------------------------------------
    # PDF → TEXT
    # -----------------------------------------------------

    elif query.data == "pdf_to_text":

        context.user_data[
            "waiting_for_pdf"
        ] = True


        await query.edit_message_text(

            "📄 PDF → Text\n\n"

            "PDF ဖိုင်တစ်ခု ပို့ပေးပါ။\n\n"

            "📄 Normal PDF\n"
            "→ Text Extract လုပ်မယ်\n\n"

            "🖼️ Scanned PDF\n"
            "→ OCR နဲ့ စာဖတ်မယ်\n\n"

            "🇲🇲 မြန်မာစာ OCR ပါ အသုံးပြုပါမယ်။"
        )


    # -----------------------------------------------------
    # AI
    # -----------------------------------------------------

    elif query.data == "ai":

        await query.edit_message_text(

            "🤖 AI Tools\n\n"

            "AI Features မကြာခင် ထည့်ပေးပါမယ်။"
        )


    # -----------------------------------------------------
    # PREMIUM
    # -----------------------------------------------------

    elif query.data == "premium":

        await query.edit_message_text(

            "⭐ Premium\n\n"

            "Premium Features မကြာခင် ထည့်ပေးပါမယ်။"
        )


    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    elif query.data == "help":

        await query.edit_message_text(

            "ℹ️ Help\n\n"

            "/start — Main Menu\n\n"

            "📄 PDF Tools\n"
            "→ PDF → Text\n\n"

            "Scanned PDF တွေကို OCR နဲ့ "
            "စာသားပြောင်းပေးနိုင်ပါတယ်။"
        )


    # -----------------------------------------------------
    # BACK
    # -----------------------------------------------------

    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=main_keyboard()
        )


# =========================================================
# NORMAL PDF TEXT EXTRACTION
# =========================================================

def extract_pdf_text(pdf_path):

    try:

        reader = PdfReader(pdf_path)

        text_parts = []


        for page_number, page in enumerate(
            reader.pages,
            start=1
        ):

            print(
                f"Extracting page {page_number}"
            )


            page_text = page.extract_text()


            if page_text:

                text_parts.append(
                    page_text
                )


        text = "\n".join(
            text_parts
        ).strip()


        print(
            "Normal text length:",
            len(text)
        )


        return text


    except Exception as e:

        print(
            "PDF extraction error:",
            repr(e)
        )

        return ""


# =========================================================
# OCR IMAGE
# =========================================================

def ocr_image(image_path):

    if not OCR_API_KEY:

        raise Exception(
            "OCR_API_KEY မတွေ့ပါ။ "
            "Render Environment Variables ကိုစစ်ပါ။"
        )


    print(
        "Sending image to OCR.Space..."
    )


    with open(
        image_path,
        "rb"
    ) as image_file:

        response = requests.post(

            OCR_URL,

            headers={
                "apikey": OCR_API_KEY
            },

            files={
                "file": (
                    "page.png",
                    image_file,
                    "image/png"
                )
            },

            data={

                "language": "mya",

                "OCREngine": "3",

                "isOverlayRequired": "false",

                "detectOrientation": "true",

                "scale": "true",

            },

            timeout=180
        )


    print(
        "OCR HTTP status:",
        response.status_code
    )


    if response.status_code != 200:

        raise Exception(
            f"OCR API HTTP {response.status_code}"
        )


    result = response.json()


    if result.get(
        "IsErroredOnProcessing"
    ):

        error_message = result.get(
            "ErrorMessage",
            "Unknown OCR error"
        )


        if isinstance(
            error_message,
            list
        ):

            error_message = " ".join(
                str(x)
                for x in error_message
            )


        raise Exception(
            str(error_message)
        )


    parsed_results = result.get(
        "ParsedResults",
        []
    )


    text_parts = []


    for item in parsed_results:

        parsed_text = item.get(
            "ParsedText",
            ""
        )


        if parsed_text:

            text_parts.append(
                parsed_text
            )


    text = "\n".join(
        text_parts
    ).strip()


    print(
        "OCR text length:",
        len(text)
    )


    return text


# =========================================================
# PDF OCR
# =========================================================

def process_pdf_with_ocr(pdf_path):

    pdf = fitz.open(
        pdf_path
    )

    all_text = []


    try:

        total_pages = len(pdf)


        print(
            f"OCR starting: {total_pages} page(s)"
        )


        for page_number in range(
            total_pages
        ):

            print(
                f"OCR page "
                f"{page_number + 1}/"
                f"{total_pages}"
            )


            page = pdf[
                page_number
            ]


            pix = page.get_pixmap(

                matrix=fitz.Matrix(
                    1.5,
                    1.5
                ),

                alpha=False
            )


            image_path = tempfile.mktemp(
                suffix=".png"
            )


            try:

                pix.save(
                    image_path
                )


                text = ocr_image(
                    image_path
                )


                if text:

                    all_text.append(

                        f"\n--- Page "
                        f"{page_number + 1} ---\n"
                    )

                    all_text.append(
                        text
                    )


            finally:

                if os.path.exists(
                    image_path
                ):

                    os.remove(
                        image_path
                    )


    finally:

        pdf.close()


    result = "\n".join(
        all_text
    ).strip()


    print(
        "Total OCR text length:",
        len(result)
    )


    return result


# =========================================================
# PDF HANDLER
# =========================================================

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


    filename = (
        document.file_name
        or ""
    )


    if not filename.lower().endswith(
        ".pdf"
    ):

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

        # -------------------------------------------------
        # Download
        # -------------------------------------------------

        print(
            "Downloading PDF..."
        )


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


        print(
            "PDF downloaded."
        )


        # -------------------------------------------------
        # Normal extraction
        # -------------------------------------------------

        text = extract_pdf_text(
            input_path
        )


        # -------------------------------------------------
        # OCR fallback
        # -------------------------------------------------

        if len(
            text.strip()
        ) < 100:


            await processing_message.edit_text(

                "🔎 Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"

                "🧠 Myanmar OCR နဲ့ "
                "စာဖတ်နေပါတယ်...\n\n"

                "⏳ ခဏစောင့်ပါ။"
            )


            text = process_pdf_with_ocr(
                input_path
            )


        # -------------------------------------------------
        # No text
        # -------------------------------------------------

        if not text.strip():

            await processing_message.edit_text(

                "❌ PDF ထဲက စာကို "
                "ဖတ်မရပါ။"
            )

            return


        # -------------------------------------------------
        # Create TXT
        # -------------------------------------------------

        output_path = tempfile.mktemp(
            suffix=".txt"
        )


        with open(

            output_path,

            "w",

            encoding="utf-8"

        ) as output_file:

            output_file.write(
                text
            )


        try:

            await processing_message.delete()

        except Exception:

            pass


        # -------------------------------------------------
        # Send TXT
        # -------------------------------------------------

        await update.message.reply_document(

            document=output_path,

            caption=(
                "✅ PDF → Text ပြီးပါပြီ။\n"
                "🇲🇲 Myanmar OCR ပါဝင်ပါတယ်။"
            )
        )


        context.user_data[
            "waiting_for_pdf"
        ] = False


        print(
            "PDF processing completed."
        )


    except Exception as e:

        print(
            "PDF PROCESSING ERROR:",
            repr(e)
        )


        try:

            await processing_message.edit_text(

                "❌ PDF processing "
                "မအောင်မြင်ပါ။\n\n"

                f"Error: {str(e)[:800]}"
            )

        except Exception:

            pass


    finally:

        if (
            input_path
            and os.path.exists(
                input_path
            )
        ):

            os.remove(
                input_path
            )


        if (
            output_path
            and os.path.exists(
                output_path
            )
        ):

            os.remove(
                output_path
            )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "BOT ERROR:",
        repr(context.error)
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN မတွေ့ပါ။"
        )


    print(
        "Starting Render health server..."
    )


    # -----------------------------------------------------
    # Health server
    # -----------------------------------------------------

    health_thread = threading.Thread(

        target=start_health_server,

        daemon=True
    )

    health_thread.start()


    # -----------------------------------------------------
    # Telegram application
    # -----------------------------------------------------

    app = (

        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )


    # -----------------------------------------------------
    # Handlers
    # -----------------------------------------------------

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


    app.add_error_handler(
        error_handler
    )


    print(
        "Bot is running..."
    )


    # -----------------------------------------------------
    # POLLING
    # -----------------------------------------------------

    app.run_polling(

        drop_pending_updates=True
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()

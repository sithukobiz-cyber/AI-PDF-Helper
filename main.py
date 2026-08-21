import os
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import fitz
from PIL import Image
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


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

OCR_URL = "https://api.ocr.space/parse/image"

# OCR retry settings
OCR_QUALITIES = [75, 60, 45, 30]

# Maximum image dimension before OCR
MAX_IMAGE_DIMENSION = 2800


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

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

    try:

        server = HTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler
        )

        print(
            f"Health server running on port {PORT}"
        )

        server.serve_forever()

    except Exception as e:

        print(
            "Health server error:",
            repr(e)
        )


# ============================================================
# MAIN MENU
# ============================================================

def get_main_keyboard():

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


# ============================================================
# START COMMAND
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data[
        "waiting_for_pdf"
    ] = False

    await update.message.reply_text(

        "🤖 AI PDF Helper မှ ကြိုဆိုပါတယ်!\n\n"

        "📄 PDF / File Tools\n"
        "🧠 AI Tools\n"
        "⭐ Premium\n\n"

        "အောက်က Menu ကိုရွေးပါ 👇",

        reply_markup=get_main_keyboard()
    )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


    # --------------------------------------------------------
    # PDF MENU
    # --------------------------------------------------------

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
            ],

        ])

        await query.edit_message_text(

            "📄 PDF Tools\n\n"

            "PDF → Text ကိုရွေးပါ 👇",

            reply_markup=keyboard
        )


    # --------------------------------------------------------
    # PDF TO TEXT
    # --------------------------------------------------------

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
            "→ Myanmar OCR နဲ့ ဖတ်မယ်\n\n"

            "🇲🇲 မြန်မာစာ OCR ပါ အသုံးပြုပါမယ်။"
        )


    # --------------------------------------------------------
    # AI MENU
    # --------------------------------------------------------

    elif query.data == "ai":

        await query.edit_message_text(

            "🤖 AI Tools\n\n"

            "AI Features မကြာခင် ထည့်ပေးပါမယ်။"
        )


    # --------------------------------------------------------
    # PREMIUM MENU
    # --------------------------------------------------------

    elif query.data == "premium":

        await query.edit_message_text(

            "⭐ Premium\n\n"

            "Premium system မကြာခင် ထည့်ပေးပါမယ်။"
        )


    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    elif query.data == "help":

        await query.edit_message_text(

            "ℹ️ Help\n\n"

            "/start — Main Menu\n\n"

            "📄 PDF Tools\n"
            "→ PDF → Text\n\n"

            "Scanned PDF တွေကို "
            "Myanmar OCR နဲ့ ဖတ်ပေးနိုင်ပါတယ်။"
        )


    # --------------------------------------------------------
    # BACK
    # --------------------------------------------------------

    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=get_main_keyboard()
        )


# ============================================================
# NORMAL PDF TEXT EXTRACTION
# ============================================================

def extract_pdf_text(pdf_path):

    try:

        reader = PdfReader(pdf_path)

        text_parts = []

        total_pages = len(
            reader.pages
        )

        print(
            f"PDF pages: {total_pages}"
        )

        for page_number, page in enumerate(
            reader.pages,
            start=1
        ):

            print(
                f"Extracting page {page_number}"
            )

            try:

                page_text = (
                    page.extract_text()
                )

                if page_text:

                    text_parts.append(
                        page_text
                    )

            except Exception as e:

                print(
                    f"Page {page_number} "
                    f"extract error:",
                    repr(e)
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


# ============================================================
# CREATE COMPRESSED OCR IMAGE
# ============================================================

def create_ocr_image(
    page,
    output_path,
    quality
):

    # Render page at 2x
    pix = page.get_pixmap(
        matrix=fitz.Matrix(
            2.0,
            2.0
        ),
        alpha=False
    )

    temporary_png = tempfile.mktemp(
        suffix=".png"
    )

    try:

        pix.save(
            temporary_png
        )

        image = Image.open(
            temporary_png
        )

        image = image.convert(
            "RGB"
        )

        width, height = image.size

        largest_dimension = max(
            width,
            height
        )

        if (
            largest_dimension
            > MAX_IMAGE_DIMENSION
        ):

            scale = (
                MAX_IMAGE_DIMENSION
                /
                largest_dimension
            )

            new_width = int(
                width * scale
            )

            new_height = int(
                height * scale
            )

            image = image.resize(
                (
                    new_width,
                    new_height
                ),
                Image.Resampling.LANCZOS
            )

        image.save(
            output_path,
            format="JPEG",
            quality=quality,
            optimize=True
        )

    finally:

        if os.path.exists(
            temporary_png
        ):

            os.remove(
                temporary_png
            )


# ============================================================
# OCR API
# ============================================================

def send_image_to_ocr(
    image_path
):

    if not OCR_API_KEY:

        raise Exception(
            "OCR_API_KEY မတွေ့ပါ။"
        )

    print(
        "Sending image to OCR.Space..."
    )

    try:

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
                        "page.jpg",
                        image_file,
                        "image/jpeg"
                    )
                },

                data={

                    # Myanmar language
                    "language": "mya",

                    # OCR Engine 3
                    "OCREngine": "3",

                    "isOverlayRequired":
                        "false",

                    "detectOrientation":
                        "true",

                    "scale":
                        "true",

                },

                timeout=180
            )

    except requests.RequestException as e:

        raise Exception(
            f"OCR network error: {e}"
        )

    print(
        "OCR HTTP status:",
        response.status_code
    )

    # Return HTTP status and response
    return response.status_code, response


# ============================================================
# OCR ONE PAGE WITH 413 RETRY
# ============================================================

def ocr_page_with_retry(
    page,
    page_number
):

    last_error = None

    for quality in OCR_QUALITIES:

        image_path = tempfile.mktemp(
            suffix=".jpg"
        )

        try:

            print(
                f"Creating OCR image "
                f"page {page_number}, "
                f"quality={quality}"
            )

            create_ocr_image(
                page,
                image_path,
                quality
            )

            file_size = os.path.getsize(
                image_path
            )

            print(
                f"OCR image size: "
                f"{file_size / 1024:.1f} KB"
            )

            status_code, response = (
                send_image_to_ocr(
                    image_path
                )
            )

            # ------------------------------------------------
            # 413
            # ------------------------------------------------

            if status_code == 413:

                print(
                    f"HTTP 413 on page "
                    f"{page_number}. "
                    f"Retrying with lower "
                    f"quality..."
                )

                last_error = (
                    "OCR API HTTP 413"
                )

                continue


            # ------------------------------------------------
            # Other HTTP errors
            # ------------------------------------------------

            if status_code != 200:

                last_error = (
                    f"OCR API HTTP "
                    f"{status_code}"
                )

                print(
                    last_error
                )

                break


            # ------------------------------------------------
            # JSON response
            # ------------------------------------------------

            try:

                result = response.json()

            except Exception:

                last_error = (
                    "OCR API JSON response "
                    "မဖတ်နိုင်ပါ။"
                )

                break


            # ------------------------------------------------
            # OCR processing error
            # ------------------------------------------------

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

                last_error = str(
                    error_message
                )

                print(
                    "OCR processing error:",
                    last_error
                )

                break


            # ------------------------------------------------
            # Parse results
            # ------------------------------------------------

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
                f"OCR text length: "
                f"{len(text)}"
            )

            return text, None


        except Exception as e:

            last_error = str(e)

            print(
                f"OCR page {page_number} "
                f"error:",
                repr(e)
            )

            break


        finally:

            if os.path.exists(
                image_path
            ):

                os.remove(
                    image_path
                )


    # --------------------------------------------------------
    # All retries failed
    # --------------------------------------------------------

    return "", last_error or (
        "Unknown OCR error"
    )


# ============================================================
# PROCESS PDF WITH OCR
# ============================================================

def process_pdf_with_ocr(
    pdf_path
):

    pdf = fitz.open(
        pdf_path
    )

    all_text = []

    failed_pages = []

    try:

        total_pages = len(
            pdf
        )

        print(
            f"OCR starting: "
            f"{total_pages} page(s)"
        )

        for page_index in range(
            total_pages
        ):

            page_number = (
                page_index + 1
            )

            print(
                f"OCR page "
                f"{page_number}/"
                f"{total_pages}"
            )

            page = pdf[
                page_index
            ]

            text, error = (
                ocr_page_with_retry(
                    page,
                    page_number
                )
            )

            if text:

                all_text.append(

                    f"\n--- Page "
                    f"{page_number} ---\n"
                )

                all_text.append(
                    text
                )

            else:

                print(
                    f"OCR failed on "
                    f"page {page_number}:",
                    error
                )

                failed_pages.append(
                    page_number
                )

                # Do NOT stop entire PDF
                all_text.append(

                    f"\n--- Page "
                    f"{page_number} ---\n"
                    f"[OCR FAILED: "
                    f"{error}]\n"
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

    if failed_pages:

        print(
            "Failed pages:",
            failed_pages
        )

    return result, failed_pages


# ============================================================
# SEND LONG TEXT AS FILE
# ============================================================

def create_text_file(
    text
):

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

    return output_path


# ============================================================
# PDF HANDLER
# ============================================================

async def handle_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get(
        "waiting_for_pdf"
    ):

        return

    document = (
        update.message.document
    )

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

            "⏳ PDF ကို download "
            "လုပ်နေပါတယ်..."
        )
    )


    input_path = None
    output_path = None


    try:

        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

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


        # ----------------------------------------------------
        # Normal extraction
        # ----------------------------------------------------

        await processing_message.edit_text(

            "📄 PDF စာသားကို "
            "အရင် Extract လုပ်နေပါတယ်..."
        )


        text = extract_pdf_text(
            input_path
        )


        # ----------------------------------------------------
        # OCR fallback
        # ----------------------------------------------------

        if len(
            text.strip()
        ) < 100:

            await processing_message.edit_text(

                "🔎 Scanned PDF "
                "ဖြစ်နိုင်ပါတယ်။\n\n"

                "🇲🇲 Myanmar OCR "
                "နဲ့ စာဖတ်နေပါတယ်...\n\n"

                "⏳ Page တစ်မျက်နှာချင်းစီ "
                "လုပ်နေပါတယ်။"
            )


            text, failed_pages = (
                process_pdf_with_ocr(
                    input_path
                )
            )

        else:

            failed_pages = []


        # ----------------------------------------------------
        # Empty result
        # ----------------------------------------------------

        if not text.strip():

            await processing_message.edit_text(

                "❌ PDF ထဲက စာကို "
                "မဖတ်နိုင်ပါ။"
            )

            return


        # ----------------------------------------------------
        # Create TXT
        # ----------------------------------------------------

        output_path = create_text_file(
            text
        )


        # ----------------------------------------------------
        # Processing message
        # ----------------------------------------------------

        try:

            await processing_message.delete()

        except Exception:

            pass


        # ----------------------------------------------------
        # Caption
        # ----------------------------------------------------

        if failed_pages:

            failed_text = ", ".join(
                str(x)
                for x in failed_pages
            )

            caption = (

                "⚠️ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 Myanmar OCR အသုံးပြုထားပါတယ်။\n\n"

                f"⚠️ OCR မအောင်မြင်သော page: "
                f"{failed_text}"
            )

        else:

            caption = (

                "✅ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 Myanmar OCR အသုံးပြုထားပါတယ်။"
            )


        # ----------------------------------------------------
        # Send TXT
        # ----------------------------------------------------

        await update.message.reply_document(

            document=output_path,

            caption=caption
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

                f"Error:\n"
                f"{str(e)[:800]}"
            )

        except Exception:

            pass


    finally:

        # ----------------------------------------------------
        # Cleanup PDF
        # ----------------------------------------------------

        if (
            input_path
            and os.path.exists(
                input_path
            )
        ):

            try:

                os.remove(
                    input_path
                )

            except Exception:

                pass


        # ----------------------------------------------------
        # Cleanup TXT
        # ----------------------------------------------------

        if (
            output_path
            and os.path.exists(
                output_path
            )
        ):

            try:

                os.remove(
                    output_path
                )

            except Exception:

                pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "Telegram bot error:",
        repr(context.error)
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "Bot application starting..."
    )


    # --------------------------------------------------------
    # Check BOT TOKEN
    # --------------------------------------------------------

    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN မတွေ့ပါ။"
        )


    # --------------------------------------------------------
    # OCR key warning
    # --------------------------------------------------------

    if not OCR_API_KEY:

        print(
            "WARNING: "
            "OCR_API_KEY မတွေ့ပါ။"
        )


    # --------------------------------------------------------
    # Render health server
    # --------------------------------------------------------

    health_thread = threading.Thread(

        target=start_health_server,

        daemon=True
    )

    health_thread.start()


    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    app = (

        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )


    # --------------------------------------------------------
    # Handlers
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # IMPORTANT:
    # Polling only.
    # Do NOT use run_webhook().
    # --------------------------------------------------------

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

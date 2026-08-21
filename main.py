import os
import io
import asyncio
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
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

OCR_URL = "https://api.ocr.space/parse/image"

# 413 ဖြစ်ရင် ဒီအဆင့်အတိုင်း image ကို သေးသွားမယ်
OCR_SETTINGS = [
    (2400, 75),
    (2200, 65),
    (2000, 55),
    (1800, 45),
    (1600, 35),
]

OCR_TIMEOUT = 120

# PDF တစ်ခုကို user တစ်ယောက်က တစ်ကြိမ်ပဲ process
# လုပ်နေစေဖို့
PROCESSING_USERS = set()

PROCESSING_LOCK = threading.Lock()


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


# ============================================================
# /START
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

        reply_markup=main_keyboard()
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

            "🇲🇲 မြန်မာစာ OCR အသုံးပြုပါမယ်။"
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

            "📄 PDF Tools\n"
            "→ PDF → Text"
        )


    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=main_keyboard()
        )


# ============================================================
# NORMAL PDF TEXT EXTRACTION
# ============================================================

def extract_pdf_text(pdf_path):

    try:

        reader = PdfReader(pdf_path)

        parts = []

        for index, page in enumerate(
            reader.pages,
            start=1
        ):

            print(
                f"Extracting page {index}"
            )

            try:

                text = page.extract_text()

                if text:

                    parts.append(
                        text
                    )

            except Exception as e:

                print(
                    f"Page {index} "
                    f"extract error:",
                    repr(e)
                )

        result = "\n".join(
            parts
        ).strip()

        print(
            "Normal text length:",
            len(result)
        )

        return result

    except Exception as e:

        print(
            "PDF extraction error:",
            repr(e)
        )

        return ""


# ============================================================
# CREATE OCR IMAGE
# ============================================================

def create_ocr_image(
    page,
    max_dimension,
    quality
):

    # 2x rendering
    pix = page.get_pixmap(
        matrix=fitz.Matrix(
            2.0,
            2.0
        ),
        alpha=False
    )

    image = Image.frombytes(
        "RGB",
        [
            pix.width,
            pix.height
        ],
        pix.samples
    )

    width, height = image.size

    largest = max(
        width,
        height
    )

    if largest > max_dimension:

        scale = (
            max_dimension
            /
            largest
        )

        new_size = (

            int(width * scale),

            int(height * scale)
        )

        image = image.resize(
            new_size,
            Image.Resampling.LANCZOS
        )

    buffer = io.BytesIO()

    image.save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=True
    )

    buffer.seek(0)

    return buffer


# ============================================================
# OCR REQUEST
# ============================================================

def send_ocr_request(
    image_buffer
):

    image_buffer.seek(0)

    response = requests.post(

        OCR_URL,

        headers={
            "apikey": OCR_API_KEY
        },

        files={

            "file": (
                "page.jpg",
                image_buffer,
                "image/jpeg"
            )
        },

        data={

            "language": "mya",

            "OCREngine": "3",

            "isOverlayRequired":
                "false",

            "detectOrientation":
                "true",

            "scale":
                "true",
        },

        timeout=OCR_TIMEOUT
    )

    return response


# ============================================================
# OCR ONE PAGE
# ============================================================

def ocr_page(
    page,
    page_number
):

    if not OCR_API_KEY:

        return (
            "",
            "OCR_API_KEY မတွေ့ပါ"
        )


    last_error = (
        "Unknown OCR error"
    )


    for attempt, (
        max_dimension,
        quality
    ) in enumerate(
        OCR_SETTINGS,
        start=1
    ):

        print(
            f"Page {page_number}: "
            f"OCR attempt {attempt} "
            f"(size={max_dimension}, "
            f"quality={quality})"
        )


        try:

            image_buffer = (
                create_ocr_image(
                    page,
                    max_dimension,
                    quality
                )
            )

            image_size = (
                len(
                    image_buffer.getvalue()
                )
            )

            print(
                f"Page {page_number}: "
                f"image size="
                f"{image_size / 1024:.1f} KB"
            )


            response = (
                send_ocr_request(
                    image_buffer
                )
            )


            print(
                f"Page {page_number}: "
                f"OCR HTTP "
                f"{response.status_code}"
            )


            # ------------------------------------------------
            # 413
            # ------------------------------------------------

            if response.status_code == 413:

                last_error = (
                    "HTTP 413"
                )

                print(
                    f"Page {page_number}: "
                    "413 - reducing image "
                    "size and retrying..."
                )

                continue


            # ------------------------------------------------
            # Other HTTP errors
            # ------------------------------------------------

            if response.status_code != 200:

                last_error = (
                    f"HTTP "
                    f"{response.status_code}"
                )

                print(
                    f"Page {page_number}: "
                    f"{last_error}"
                )

                # API error ဖြစ်ရင် retry
                # မလုပ်ဘဲ နောက် attempt
                # သွားမယ်
                continue


            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                result = response.json()

            except Exception as e:

                last_error = (
                    "Invalid JSON response"
                )

                print(
                    f"Page {page_number}: "
                    f"{last_error}:",
                    repr(e)
                )

                continue


            # ------------------------------------------------
            # OCR processing error
            # ------------------------------------------------

            if result.get(
                "IsErroredOnProcessing"
            ):

                message = result.get(
                    "ErrorMessage",
                    "OCR processing error"
                )

                if isinstance(
                    message,
                    list
                ):

                    message = " ".join(
                        str(x)
                        for x in message
                    )

                last_error = str(
                    message
                )

                print(
                    f"Page {page_number}: "
                    f"OCR error: "
                    f"{last_error}"
                )

                continue


            # ------------------------------------------------
            # Extract result
            # ------------------------------------------------

            parsed_results = (
                result.get(
                    "ParsedResults",
                    []
                )
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
                f"Page {page_number}: "
                f"OCR text length="
                f"{len(text)}"
            )


            if text:

                return (
                    text,
                    None
                )


            last_error = (
                "OCR returned empty text"
            )


        except requests.Timeout:

            last_error = (
                "OCR request timeout"
            )

            print(
                f"Page {page_number}: "
                "OCR timeout"
            )

        except requests.RequestException as e:

            last_error = (
                f"OCR network error: {e}"
            )

            print(
                f"Page {page_number}: "
                f"{last_error}"
            )

        except Exception as e:

            last_error = str(e)

            print(
                f"Page {page_number}: "
                "unexpected error:",
                repr(e)
            )


    return (
        "",
        last_error
    )


# ============================================================
# OCR PDF
# ============================================================

def process_pdf_ocr(
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


        # ----------------------------------------------------
        # FIRST PASS
        # ----------------------------------------------------

        for index in range(
            total_pages
        ):

            page_number = (
                index + 1
            )

            print(
                f"OCR page "
                f"{page_number}/"
                f"{total_pages}"
            )

            page = pdf[index]

            text, error = ocr_page(
                page,
                page_number
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
                    f"Page "
                    f"{page_number} "
                    f"FAILED:",
                    error
                )

                failed_pages.append(
                    page_number
                )

                all_text.append(

                    f"\n--- Page "
                    f"{page_number} ---\n"

                    f"[OCR FAILED]\n"
                )


        # ----------------------------------------------------
        # SECOND PASS
        # Retry failed pages
        # ----------------------------------------------------

        if failed_pages:

            print(
                "Starting second pass "
                "for failed pages:",
                failed_pages
            )


            retry_results = {}


            for page_number in (
                failed_pages
            ):

                print(
                    f"Retrying failed "
                    f"page {page_number}"
                )

                page = pdf[
                    page_number - 1
                ]

                text, error = ocr_page(
                    page,
                    page_number
                )


                if text:

                    retry_results[
                        page_number
                    ] = text

                else:

                    print(
                        f"Retry failed "
                        f"page {page_number}:",
                        error
                    )


            # ------------------------------------------------
            # Replace failed page results
            # ------------------------------------------------

            if retry_results:

                new_text = []

                for index in range(
                    total_pages
                ):

                    page_number = (
                        index + 1
                    )

                    if (
                        page_number
                        in retry_results
                    ):

                        new_text.append(

                            f"\n--- Page "
                            f"{page_number} ---\n"
                        )

                        new_text.append(
                            retry_results[
                                page_number
                            ]
                        )

                    else:

                        # Existing first-pass
                        # result
                        pass


                # First-pass text already
                # contains successful pages.
                #
                # Append retry results separately
                # so no text gets lost.

                for page_number, text in (
                    retry_results.items()
                ):

                    all_text.append(

                        f"\n--- Page "
                        f"{page_number} "
                        f"(Retry Result) ---\n"
                    )

                    all_text.append(
                        text
                    )


                # Successful retry pages
                # should no longer be listed
                # as failed.

                failed_pages = [
                    page
                    for page in failed_pages
                    if page
                    not in retry_results
                ]


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
            "Final failed pages:",
            failed_pages
        )

    else:

        print(
            "All OCR pages completed."
        )


    return (
        result,
        failed_pages
    )


# ============================================================
# BACKGROUND PDF PROCESSOR
# ============================================================

def process_pdf_background(
    pdf_path
):

    try:

        return process_pdf_ocr(
            pdf_path
        )

    finally:

        if os.path.exists(
            pdf_path
        ):

            try:

                os.remove(
                    pdf_path
                )

            except Exception:

                pass


# ============================================================
# PDF HANDLER
# ============================================================

async def handle_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = (
        update.effective_user.id
    )

    if not context.user_data.get(
        "waiting_for_pdf"
    ):

        return


    # --------------------------------------------------------
    # Prevent same user from
    # starting multiple jobs
    # --------------------------------------------------------

    with PROCESSING_LOCK:

        if user_id in PROCESSING_USERS:

            await update.message.reply_text(

                "⏳ သင့် PDF ကို "
                "လုပ်နေပြီးသားပါ။\n\n"

                "ပြီးအောင် စောင့်ပေးပါ။"
            )

            return

        PROCESSING_USERS.add(
            user_id
        )


    document = (
        update.message.document
    )

    if not document:

        with PROCESSING_LOCK:

            PROCESSING_USERS.discard(
                user_id
            )

        return


    filename = (
        document.file_name
        or ""
    )


    if not filename.lower().endswith(
        ".pdf"
    ):

        with PROCESSING_LOCK:

            PROCESSING_USERS.discard(
                user_id
            )

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


    try:

        # ----------------------------------------------------
        # Download PDF
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
            f"PDF downloaded for "
            f"user {user_id}"
        )


        # ----------------------------------------------------
        # Normal extraction
        # ----------------------------------------------------

        await processing_message.edit_text(

            "📄 PDF စာသားကို "
            "အရင် Extract လုပ်နေပါတယ်..."
        )


        normal_text = (
            extract_pdf_text(
                input_path
            )
        )


        # ----------------------------------------------------
        # If normal PDF has text
        # ----------------------------------------------------

        if len(
            normal_text.strip()
        ) >= 100:

            output_path = (
                tempfile.mktemp(
                    suffix=".txt"
                )
            )

            with open(
                output_path,
                "w",
                encoding="utf-8"
            ) as f:

                f.write(
                    normal_text
                )


            try:

                await processing_message.delete()

            except Exception:

                pass


            await update.message.reply_document(

                document=output_path,

                caption=(
                    "✅ PDF → Text ပြီးပါပြီ။\n\n"
                    "📄 Normal PDF extraction "
                    "အသုံးပြုထားပါတယ်။"
                )
            )


            os.remove(
                output_path
            )

            context.user_data[
                "waiting_for_pdf"
            ] = False

            return


        # ----------------------------------------------------
        # OCR
        # ----------------------------------------------------

        pdf = fitz.open(
            input_path
        )

        total_pages = len(
            pdf
        )

        pdf.close()


        await processing_message.edit_text(

            "🔎 Scanned PDF "
            "ဖြစ်နိုင်ပါတယ်။\n\n"

            "🇲🇲 Myanmar OCR "
            "နဲ့ စာဖတ်နေပါတယ်...\n\n"

            f"📄 Pages: "
            f"{total_pages}\n\n"

            "⏳ Page တစ်မျက်နှာချင်းစီ "
            "လုပ်နေပါတယ်။"
        )


        # ----------------------------------------------------
        # IMPORTANT:
        # OCR is synchronous.
        #
        # Run it in a background thread
        # so /start can continue responding.
        # ----------------------------------------------------

        result, failed_pages = (
            await asyncio.to_thread(
                process_pdf_background,
                input_path
            )
        )


        input_path = None


        if not result.strip():

            await processing_message.edit_text(

                "❌ PDF ထဲက စာကို "
                "မဖတ်နိုင်ပါ။"
            )

            return


        # ----------------------------------------------------
        # Create TXT
        # ----------------------------------------------------

        output_path = tempfile.mktemp(
            suffix=".txt"
        )


        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(
                result
            )


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

                "🇲🇲 Myanmar OCR "
                "အသုံးပြုထားပါတယ်။\n\n"

                f"⚠️ OCR မအောင်မြင်သော page: "
                f"{failed_text}"
            )

        else:

            caption = (

                "✅ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 Myanmar OCR "
                "အသုံးပြုထားပါတယ်။"
            )


        await update.message.reply_document(

            document=output_path,

            caption=caption
        )


        try:

            os.remove(
                output_path
            )

        except Exception:

            pass


        context.user_data[
            "waiting_for_pdf"
        ] = False


        print(
            f"PDF processing completed "
            f"for user {user_id}"
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


        with PROCESSING_LOCK:

            PROCESSING_USERS.discard(
                user_id
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "Telegram error:",
        repr(context.error)
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "Bot application starting..."
    )


    if not BOT_TOKEN:

        raise ValueError(
            "BOT_TOKEN မတွေ့ပါ။"
        )


    if not OCR_API_KEY:

        print(
            "WARNING: "
            "OCR_API_KEY မတွေ့ပါ။"
        )


    # --------------------------------------------------------
    # Render health server
    # --------------------------------------------------------

    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()


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
    # POLLING
    #
    # Do NOT use run_webhook()
    # --------------------------------------------------------

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

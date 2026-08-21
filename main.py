import os
import io
import time
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

OCR_TIMEOUT = 120

# OCR image settings.
# 413 ဖြစ်ရင် အောက်က setting တွေအတိုင်း image ကို
# တဖြည်းဖြည်းသေးပြီး retry လုပ်မယ်။
OCR_SETTINGS = [
    (2200, 70),
    (2000, 60),
    (1800, 50),
    (1600, 42),
    (1400, 35),
]

# 429 ဖြစ်ရင် ဒီအချိန်တွေ စောင့်မယ်
RATE_LIMIT_DELAYS = [
    10,
    20,
    40,
    80,
]

# Page တစ်ခုကို အများဆုံး OCR attempt
MAX_OCR_ATTEMPTS = 5

# User တစ်ယောက်က တစ်ချိန်တည်း PDF တစ်ခုသာ process
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
# START
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
            "📄 PDF Tools → PDF → Text"
        )

    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=main_keyboard()
        )


# ============================================================
# NORMAL PDF EXTRACTION
# ============================================================

def extract_pdf_text(pdf_path):

    try:

        reader = PdfReader(pdf_path)

        parts = []

        for page_number, page in enumerate(
            reader.pages,
            start=1
        ):

            print(
                f"Extracting page {page_number}"
            )

            try:

                text = page.extract_text()

                if text:
                    parts.append(text)

            except Exception as e:

                print(
                    f"Page {page_number} "
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

    pix = page.get_pixmap(
        matrix=fitz.Matrix(
            2.0,
            2.0
        ),
        alpha=False
    )

    image = Image.frombytes(
        "RGB",
        (pix.width, pix.height),
        pix.samples
    )

    width, height = image.size

    largest = max(
        width,
        height
    )

    if largest > max_dimension:

        scale = (
            max_dimension /
            largest
        )

        image = image.resize(

            (
                max(
                    1,
                    int(width * scale)
                ),
                max(
                    1,
                    int(height * scale)
                ),
            ),

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

    last_error = "Unknown OCR error"

    setting_index = 0

    rate_limit_count = 0

    attempt = 0

    while attempt < MAX_OCR_ATTEMPTS:

        attempt += 1

        max_dimension, quality = OCR_SETTINGS[
            min(
                setting_index,
                len(OCR_SETTINGS) - 1
            )
        ]

        print(
            f"OCR page {page_number}: "
            f"attempt={attempt}, "
            f"size={max_dimension}, "
            f"quality={quality}"
        )

        try:

            image_buffer = create_ocr_image(
                page,
                max_dimension,
                quality
            )

            image_size = len(
                image_buffer.getvalue()
            )

            print(
                f"OCR page {page_number}: "
                f"image={image_size / 1024:.1f} KB"
            )

            response = send_ocr_request(
                image_buffer
            )

            status = response.status_code

            print(
                f"OCR page {page_number}: "
                f"HTTP {status}"
            )

            # ------------------------------------------------
            # 200
            # ------------------------------------------------

            if status == 200:

                try:

                    result = response.json()

                except Exception as e:

                    last_error = (
                        "Invalid JSON response"
                    )

                    print(
                        f"Page {page_number}:",
                        repr(e)
                    )

                    continue

                if result.get(
                    "IsErroredOnProcessing"
                ):

                    error_message = result.get(
                        "ErrorMessage",
                        "OCR processing error"
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
                        f"Page {page_number}: "
                        f"OCR error: "
                        f"{last_error}"
                    )

                    # OCR error ဖြစ်ရင်
                    # image quality လျှော့ပြီး retry
                    setting_index += 1

                    continue

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
                    f"OCR page {page_number}: "
                    f"text length={len(text)}"
                )

                if text:

                    return (
                        text,
                        None
                    )

                last_error = (
                    "OCR returned empty text"
                )

                setting_index += 1

                continue

            # ------------------------------------------------
            # 413
            # ------------------------------------------------

            if status == 413:

                last_error = "OCR API HTTP 413"

                print(
                    f"Page {page_number}: "
                    "413 detected. "
                    "Reducing image size..."
                )

                setting_index += 1

                if setting_index >= len(
                    OCR_SETTINGS
                ):

                    setting_index = (
                        len(OCR_SETTINGS) - 1
                    )

                continue

            # ------------------------------------------------
            # 429
            # ------------------------------------------------

            if status == 429:

                last_error = "OCR API HTTP 429"

                rate_limit_count += 1

                # Retry-After ရှိရင် သုံးမယ်
                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:

                    try:

                        delay = int(
                            retry_after
                        )

                    except Exception:

                        delay = (
                            RATE_LIMIT_DELAYS[
                                min(
                                    rate_limit_count - 1,
                                    len(
                                        RATE_LIMIT_DELAYS
                                    ) - 1
                                )
                            ]
                        )

                else:

                    delay = (
                        RATE_LIMIT_DELAYS[
                            min(
                                rate_limit_count - 1,
                                len(
                                    RATE_LIMIT_DELAYS
                                ) - 1
                            )
                        ]
                    )

                print(
                    f"Page {page_number}: "
                    f"429 rate limited. "
                    f"Waiting {delay}s..."
                )

                time.sleep(
                    delay
                )

                continue

            # ------------------------------------------------
            # Other HTTP errors
            # ------------------------------------------------

            last_error = (
                f"OCR API HTTP {status}"
            )

            print(
                f"Page {page_number}: "
                f"{last_error}"
            )

            # Server error တွေမှာ
            # အနည်းငယ်စောင့်ပြီး retry
            if status >= 500:

                time.sleep(5)

            else:

                setting_index += 1

        except requests.Timeout:

            last_error = (
                "OCR request timeout"
            )

            print(
                f"Page {page_number}: "
                "timeout. Retrying..."
            )

            time.sleep(3)

        except requests.RequestException as e:

            last_error = (
                f"OCR network error: {e}"
            )

            print(
                f"Page {page_number}: "
                f"{last_error}"
            )

            time.sleep(3)

        except Exception as e:

            last_error = str(e)

            print(
                f"Page {page_number}: "
                f"unexpected error:",
                repr(e)
            )

            time.sleep(2)

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

    total_pages = len(
        pdf
    )

    results = {}

    failed_pages = []

    try:

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

            page_number = index + 1

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

                results[
                    page_number
                ] = text

            else:

                print(
                    f"OCR failed on "
                    f"page {page_number}:",
                    error
                )

                failed_pages.append(
                    page_number
                )

            # Request အရမ်းမြန်မသွားအောင်
            # small delay
            time.sleep(1)

        # ----------------------------------------------------
        # SECOND PASS
        # ----------------------------------------------------

        if failed_pages:

            print(
                "Starting failed-page retry..."
            )

            original_failed = list(
                failed_pages
            )

            retry_failed = []

            for page_number in original_failed:

                print(
                    f"Retrying page "
                    f"{page_number}"
                )

                # Retry မလုပ်ခင်
                # နည်းနည်းစောင့်
                time.sleep(3)

                page = pdf[
                    page_number - 1
                ]

                text, error = ocr_page(
                    page,
                    page_number
                )

                if text:

                    results[
                        page_number
                    ] = text

                    print(
                        f"Retry success: "
                        f"page {page_number}"
                    )

                else:

                    print(
                        f"Retry failed: "
                        f"page {page_number}:",
                        error
                    )

                    retry_failed.append(
                        page_number
                    )

            failed_pages = retry_failed

    finally:

        pdf.close()

    # --------------------------------------------------------
    # BUILD TEXT IN CORRECT PAGE ORDER
    # --------------------------------------------------------

    output_parts = []

    for page_number in range(
        1,
        total_pages + 1
    ):

        output_parts.append(
            f"\n--- Page "
            f"{page_number} ---\n"
        )

        if page_number in results:

            output_parts.append(
                results[
                    page_number
                ]
            )

        else:

            output_parts.append(
                "[OCR FAILED]"
            )

    final_text = "\n".join(
        output_parts
    ).strip()

    print(
        "Total OCR text length:",
        len(final_text)
    )

    if failed_pages:

        print(
            "Failed pages:",
            failed_pages
        )

    else:

        print(
            "All OCR pages completed."
        )

    return (
        final_text,
        failed_pages
    )


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
        document.file_name or ""
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
    output_path = None

    try:

        # ----------------------------------------------------
        # DOWNLOAD
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

        # ----------------------------------------------------
        # NORMAL EXTRACTION
        # ----------------------------------------------------

        await processing_message.edit_text(

            "📄 PDF စာသားကို "
            "အရင် Extract လုပ်နေပါတယ်..."
        )

        normal_text = extract_pdf_text(
            input_path
        )

        # ----------------------------------------------------
        # NORMAL PDF
        # ----------------------------------------------------

        if len(
            normal_text.strip()
        ) >= 100:

            output_path = tempfile.mktemp(
                suffix=".txt"
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

            output_path = None

            context.user_data[
                "waiting_for_pdf"
            ] = False

            return

        # ----------------------------------------------------
        # CHECK PAGE COUNT
        # ----------------------------------------------------

        pdf = fitz.open(
            input_path
        )

        total_pages = len(
            pdf
        )

        pdf.close()

        # ----------------------------------------------------
        # OCR
        # ----------------------------------------------------

        await processing_message.edit_text(

            "🔎 Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"

            "🇲🇲 Myanmar OCR "
            "နဲ့ စာဖတ်နေပါတယ်...\n\n"

            f"📄 Pages: {total_pages}\n\n"

            "⏳ Rate-limit safe OCR "
            "အသုံးပြုနေပါတယ်။"
        )

        # OCR ကို background thread
        # ထဲမှာ run
        result, failed_pages = (
            await asyncio.to_thread(
                process_pdf_ocr,
                input_path
            )
        )

        # ----------------------------------------------------
        # INPUT NO LONGER NEEDED
        # ----------------------------------------------------

        try:

            os.remove(
                input_path
            )

        except Exception:

            pass

        input_path = None

        if not result.strip():

            await processing_message.edit_text(

                "❌ PDF ထဲက စာကို "
                "မဖတ်နိုင်ပါ။"
            )

            return

        # ----------------------------------------------------
        # CREATE TXT
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
        # CAPTION
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

        output_path = None

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

    # Render health server
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # Telegram
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

    app.add_error_handler(
        error_handler
    )

    print(
        "Bot is running..."
    )

    # IMPORTANT:
    # Polling only. Do NOT use run_webhook().
    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

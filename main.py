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

# OCR image settings
# 413 ဖြစ်ရင် အဆင့်ဆင့် လျှော့ပြီး retry
OCR_SETTINGS = [
    (2200, 70),
    (2000, 60),
    (1800, 50),
    (1600, 45),
    (1400, 40),
]

# 429 ဖြစ်ရင် retry
OCR_429_RETRIES = 4

# Request ကြား delay
OCR_REQUEST_DELAY = 1.5

# User တစ်ယောက်ကို တစ်ချိန်တည်း PDF တစ်ခု
PROCESSING_USERS = set()
PROCESSING_LOCK = threading.Lock()


# ============================================================
# HEALTH SERVER FOR RENDER
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

        "PDF → Text ပြောင်းချင်ရင်\n"
        "📄 PDF Tools ကိုနှိပ်ပါ။",

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
            "→ Text ကိုအရင်စစ်မယ်\n\n"

            "🇲🇲 Myanmar PDF encoding "
            "မမှန်ရင် OCR ပြန်လုပ်မယ်\n\n"

            "🖼️ Scanned PDF\n"
            "→ Myanmar OCR အသုံးပြုမယ်။"
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
            "→ PDF → Text\n\n"

            "🇲🇲 Myanmar PDF တွေအတွက်\n"
            "encoding စစ်ပြီး OCR fallback "
            "အသုံးပြုပါမယ်။"
        )

    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=main_keyboard()
        )


# ============================================================
# MYANMAR TEXT QUALITY CHECK
# ============================================================

def count_myanmar_chars(text):

    count = 0

    for ch in text:

        code = ord(ch)

        if (
            0x1000 <= code <= 0x109F
        ):

            count += 1

    return count


def count_suspicious_myanmar_patterns(text):

    patterns = [

        "ေြ",
        "ြေ",
        "်ေ",
        "ေျ",
        "ြ်",
        "ဴ",
        "ဵ",
        "၀",
        "၁",
        "၂",
        "၃",
        "၄",
        "၅",
        "၆",
        "၇",
        "၈",
        "၉",

    ]

    score = 0

    for pattern in patterns:

        score += text.count(pattern)

    return score


def is_myanmar_text_bad(text):

    if not text:

        return True

    stripped = text.strip()

    if len(stripped) < 50:

        return True

    myanmar_count = count_myanmar_chars(
        stripped
    )

    suspicious = (
        count_suspicious_myanmar_patterns(
            stripped
        )
    )

    # မြန်မာစာပါဝင်ပြီး
    # encoding ပျက်နိုင်တဲ့ pattern
    # အများကြီးရှိရင် reject
    if myanmar_count >= 20:

        if suspicious >= 8:

            print(
                "Myanmar text quality: BAD"
            )

            print(
                "Myanmar chars:",
                myanmar_count
            )

            print(
                "Suspicious:",
                suspicious
            )

            return True

    # မြန်မာစာနည်းလွန်းရင် OCR
    if (
        myanmar_count == 0
        and len(stripped) > 100
    ):

        return True

    print(
        "Myanmar text quality: "
        "ACCEPTABLE"
    )

    return False


# ============================================================
# NORMAL PDF EXTRACTION
# ============================================================

def extract_pdf_text(pdf_path):

    try:

        reader = PdfReader(
            pdf_path
        )

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
            f"OCR attempt {attempt}/"
            f"{len(OCR_SETTINGS)}"
        )

        try:

            image_buffer = (
                create_ocr_image(
                    page,
                    max_dimension,
                    quality
                )
            )

            image_size = len(
                image_buffer.getvalue()
            )

            print(
                f"Page {page_number}: "
                f"image "
                f"{image_size / 1024:.1f} KB"
            )

            # ------------------------------------------------
            # 429 retry
            # ------------------------------------------------

            for retry in range(
                OCR_429_RETRIES
            ):

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

                if (
                    response.status_code
                    == 429
                ):

                    wait_time = (
                        8 * (retry + 1)
                    )

                    print(
                        f"Page "
                        f"{page_number}: "
                        f"HTTP 429. "
                        f"Waiting "
                        f"{wait_time}s..."
                    )

                    time.sleep(
                        wait_time
                    )

                    image_buffer.seek(
                        0
                    )

                    continue

                break


            # ------------------------------------------------
            # 413
            # ------------------------------------------------

            if response.status_code == 413:

                last_error = (
                    "OCR API HTTP 413"
                )

                print(
                    f"Page "
                    f"{page_number}: "
                    "413 → reducing "
                    "image size"
                )

                continue


            # ------------------------------------------------
            # 429
            # ------------------------------------------------

            if response.status_code == 429:

                last_error = (
                    "OCR API HTTP 429"
                )

                print(
                    f"Page "
                    f"{page_number}: "
                    "429 after retries"
                )

                # နောက် image size
                # သို့သွား
                continue


            # ------------------------------------------------
            # Other HTTP errors
            # ------------------------------------------------

            if response.status_code != 200:

                last_error = (
                    f"OCR API HTTP "
                    f"{response.status_code}"
                )

                continue


            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                result = response.json()

            except Exception as e:

                last_error = (
                    "Invalid OCR JSON"
                )

                print(
                    repr(e)
                )

                continue


            # ------------------------------------------------
            # OCR API error
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
                    f"Page "
                    f"{page_number}: "
                    f"{last_error}"
                )

                continue


            # ------------------------------------------------
            # Extract OCR text
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
                f"Page "
                f"{page_number}: "
                f"OCR text length "
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
                f"Page "
                f"{page_number}: "
                "timeout"
            )

        except requests.RequestException as e:

            last_error = (
                f"OCR network error: {e}"
            )

            print(
                repr(e)
            )

        except Exception as e:

            last_error = str(e)

            print(
                f"Page "
                f"{page_number}: "
                "unexpected error:",
                repr(e)
            )

        # API ကို အရမ်းမြန်မြန်
        # မပစ်ရန်
        time.sleep(
            OCR_REQUEST_DELAY
        )

    return (
        "",
        last_error
    )


# ============================================================
# OCR PDF
# ============================================================

def process_pdf_ocr(
    pdf_path,
    progress_callback=None
):

    pdf = fitz.open(
        pdf_path
    )

    all_pages = {}

    failed_pages = []

    try:

        total_pages = len(
            pdf
        )

        print(
            f"OCR starting: "
            f"{total_pages} pages"
        )

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

            if progress_callback:

                progress_callback(
                    page_number,
                    total_pages
                )

            page = pdf[index]

            text, error = ocr_page(
                page,
                page_number
            )

            if text:

                all_pages[
                    page_number
                ] = text

            else:

                failed_pages.append(
                    page_number
                )

                all_pages[
                    page_number
                ] = (
                    "[OCR FAILED]"
                )

                print(
                    f"OCR failed "
                    f"page {page_number}: "
                    f"{error}"
                )

    finally:

        pdf.close()


    # --------------------------------------------------------
    # Build text in correct page order
    # --------------------------------------------------------

    output_parts = []

    for page_number in sorted(
        all_pages.keys()
    ):

        output_parts.append(

            f"\n--- Page "
            f"{page_number} ---\n"
        )

        output_parts.append(
            all_pages[
                page_number
            ]
        )

        output_parts.append("\n")


    result = "".join(
        output_parts
    ).strip()


    print(
        "Total OCR text length:",
        len(result)
    )

    print(
        "Failed pages:",
        failed_pages
    )

    return (
        result,
        failed_pages
    )


# ============================================================
# BACKGROUND PROCESS
# ============================================================

def process_pdf_background(
    pdf_path
):

    return process_pdf_ocr(
        pdf_path
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

            "⏳ PDF ကို "
            "စစ်ဆေးနေပါတယ်..."
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
            f"PDF downloaded: "
            f"{filename}"
        )


        # ----------------------------------------------------
        # NORMAL EXTRACTION
        # ----------------------------------------------------

        await processing_message.edit_text(

            "📄 PDF စာသားကို "
            "အရင်စစ်နေပါတယ်..."
        )


        normal_text = (
            await asyncio.to_thread(
                extract_pdf_text,
                input_path
            )
        )


        # ----------------------------------------------------
        # IMPORTANT:
        # Check Myanmar text quality
        # ----------------------------------------------------

        normal_bad = (
            is_myanmar_text_bad(
                normal_text
            )
        )


        # ----------------------------------------------------
        # NORMAL PDF ACCEPT
        # ----------------------------------------------------

        if (
            len(
                normal_text.strip()
            ) >= 100
            and not normal_bad
        ):

            print(
                "Normal extraction "
                "accepted."
            )

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
                    "📄 Normal PDF text "
                    "extraction အသုံးပြုထားပါတယ်။"
                )
            )

            context.user_data[
                "waiting_for_pdf"
            ] = False

            return


        # ----------------------------------------------------
        # OCR FALLBACK
        # ----------------------------------------------------

        print(
            "Normal extraction rejected."
        )

        print(
            "Starting Myanmar OCR..."
        )


        pdf = fitz.open(
            input_path
        )

        total_pages = len(
            pdf
        )

        pdf.close()


        await processing_message.edit_text(

            "🔎 Myanmar PDF encoding "
            "မမှန်နိုင်ပါ သို့မဟုတ် "
            "Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"

            "🇲🇲 Myanmar OCR "
            "စတင်နေပါတယ်...\n\n"

            f"📄 Total pages: "
            f"{total_pages}\n\n"

            "⏳ Page တစ်မျက်နှာချင်းစီ "
            "လုပ်နေပါတယ်။"
        )


        # ----------------------------------------------------
        # OCR
        # ----------------------------------------------------

        result, failed_pages = (
            await asyncio.to_thread(
                process_pdf_background,
                input_path
            )
        )


        # ----------------------------------------------------
        # Delete input
        # ----------------------------------------------------

        if (
            input_path
            and os.path.exists(
                input_path
            )
        ):

            os.remove(
                input_path
            )

            input_path = None


        if not result.strip():

            await processing_message.edit_text(

                "❌ OCR နဲ့ပါ "
                "စာမဖတ်နိုင်ပါ။"
            )

            return


        # ----------------------------------------------------
        # TXT
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
                f"{str(e)[:1000]}"
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


    # --------------------------------------------------------
    # Render health server
    # --------------------------------------------------------

    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()


    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

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
    # Render + Telegram polling
    # Webhook မသုံးပါ
    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

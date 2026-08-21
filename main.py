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

# OCR request timeout
OCR_TIMEOUT = 120

# ============================================================
# OCR SETTINGS
# ============================================================

# Myanmar OCR အတွက် engine 2 သုံးမယ်
OCR_ENGINE = "2"

OCR_LANGUAGE = "mya"

# ပုံကြီးလွန်းရင် 413 ဖြစ်နိုင်လို့
# အဆင့်ဆင့် လျှော့မယ်
OCR_IMAGE_SETTINGS = [
    (2000, 65),
    (1800, 60),
    (1600, 55),
    (1400, 50),
]

# 429 ဖြစ်ရင် အများဆုံး retry
OCR_429_RETRIES = 3

# 429 ပြန်လာရင် စောင့်မယ့်အချိန်
OCR_429_WAIT = [
    15,
    30,
    60,
]

# Request တစ်ခုနဲ့တစ်ခုကြား
OCR_REQUEST_DELAY = 2.0


# ============================================================
# PROCESS LOCK
# ============================================================

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

        "🤖 AI PDF Helper\n\n"

        "📄 PDF → Text\n"
        "🇲🇲 Myanmar PDF OCR\n\n"

        "PDF → Text ပြောင်းချင်ရင်\n"
        "📄 PDF Tools ကိုနှိပ်ပါ 👇",

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

            "📄 Normal PDF ဖြစ်ရင်\n"
            "→ Text encoding ကိုစစ်မယ်\n\n"

            "🇲🇲 Myanmar encoding ပျက်နေရင်\n"
            "→ Myanmar OCR ပြန်သုံးမယ်\n\n"

            "🖼️ Scanned PDF ဖြစ်ရင်\n"
            "→ OCR သုံးမယ်။"
        )

    elif query.data == "ai":

        await query.edit_message_text(

            "🤖 AI Tools\n\n"
            "မကြာခင် ထည့်ပေးပါမယ်။"
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

            "🇲🇲 Myanmar PDF\n"
            "→ Encoding check\n"
            "→ Myanmar OCR fallback"
        )

    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=main_keyboard()
        )


# ============================================================
# MYANMAR TEXT ANALYSIS
# ============================================================

def count_myanmar_chars(text):

    count = 0

    for ch in text:

        code = ord(ch)

        if 0x1000 <= code <= 0x109F:

            count += 1

    return count


def count_bad_patterns(text):

    patterns = [

        "ေြ",
        "ြေ",
        "်ေ",
        "ေျ",
        "ြ်",
        "ဴ",
        "ဵ",

    ]

    score = 0

    for pattern in patterns:

        score += text.count(pattern)

    return score


def is_bad_myanmar_text(text):

    if not text:

        return True

    text = text.strip()

    if len(text) < 100:

        return True

    myanmar_count = count_myanmar_chars(
        text
    )

    bad_count = count_bad_patterns(
        text
    )

    print(
        "Myanmar chars:",
        myanmar_count
    )

    print(
        "Bad patterns:",
        bad_count
    )

    # Myanmar စာပါပြီး
    # encoding pattern ပျက်နေတယ်
    if (
        myanmar_count > 30
        and bad_count >= 5
    ):

        print(
            "RESULT: BAD ENCODING"
        )

        return True

    # Myanmar စာလုံးမတွေ့ဘူး
    if (
        myanmar_count == 0
        and len(text) > 200
    ):

        print(
            "RESULT: NO MYANMAR"
        )

        return True

    print(
        "RESULT: ACCEPTABLE"
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

        for page_number, page in enumerate(
            reader.pages,
            start=1
        ):

            try:

                text = page.extract_text()

                if text:

                    parts.append(text)

                print(
                    f"Extract page "
                    f"{page_number}"
                )

            except Exception as e:

                print(
                    f"Extract error "
                    f"page {page_number}:",
                    repr(e)
                )

        result = "\n".join(
            parts
        ).strip()

        print(
            "Normal extraction length:",
            len(result)
        )

        return result

    except Exception as e:

        print(
            "Normal extraction failed:",
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
        (
            pix.width,
            pix.height
        ),
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
            / largest
        )

        image = image.resize(

            (
                int(width * scale),
                int(height * scale)
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
# SEND OCR REQUEST
# ============================================================

def send_ocr_request(
    image_buffer
):

    image_buffer.seek(0)

    return requests.post(

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

            "language": OCR_LANGUAGE,

            "OCREngine": OCR_ENGINE,

            "isOverlayRequired":
                "false",

            "detectOrientation":
                "true",

            "scale":
                "true",
        },

        timeout=OCR_TIMEOUT
    )


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
            "OCR_API_KEY မရှိပါ"
        )

    last_error = ""

    for setting_index, (
        max_dimension,
        quality
    ) in enumerate(
        OCR_IMAGE_SETTINGS,
        start=1
    ):

        print(
            f"PAGE {page_number}: "
            f"Image setting "
            f"{setting_index}/"
            f"{len(OCR_IMAGE_SETTINGS)}"
        )

        image_buffer = (
            create_ocr_image(
                page,
                max_dimension,
                quality
            )
        )

        print(
            f"PAGE {page_number}: "
            f"Image size "
            f"{len(image_buffer.getvalue()) / 1024:.1f} KB"
        )


        # ====================================================
        # REQUEST
        # ====================================================

        for retry in range(
            OCR_429_RETRIES + 1
        ):

            try:

                response = (
                    send_ocr_request(
                        image_buffer
                    )
                )

            except requests.Timeout:

                last_error = (
                    "OCR timeout"
                )

                print(
                    f"PAGE {page_number}: "
                    "Timeout"
                )

                break

            except requests.RequestException as e:

                last_error = (
                    f"Network error: {e}"
                )

                print(
                    f"PAGE {page_number}:",
                    last_error
                )

                time.sleep(5)

                continue


            status = response.status_code

            print(
                f"PAGE {page_number}: "
                f"HTTP {status}"
            )


            # =================================================
            # 429
            # =================================================

            if status == 429:

                if retry >= OCR_429_RETRIES:

                    last_error = (
                        "OCR API HTTP 429 "
                        "after retries"
                    )

                    print(
                        f"PAGE {page_number}: "
                        "429 limit reached"
                    )

                    # အကြာကြီး retry မလုပ်တော့ဘူး
                    return (
                        "",
                        last_error
                    )

                wait = OCR_429_WAIT[
                    min(
                        retry,
                        len(OCR_429_WAIT) - 1
                    )
                ]

                print(
                    f"PAGE {page_number}: "
                    f"429 → wait {wait}s"
                )

                time.sleep(
                    wait
                )

                continue


            # =================================================
            # 413
            # =================================================

            if status == 413:

                last_error = (
                    "HTTP 413"
                )

                print(
                    f"PAGE {page_number}: "
                    "413 → smaller image"
                )

                break


            # =================================================
            # OTHER HTTP ERROR
            # =================================================

            if status != 200:

                last_error = (
                    f"OCR HTTP {status}"
                )

                print(
                    f"PAGE {page_number}: "
                    f"{last_error}"
                )

                break


            # =================================================
            # JSON
            # =================================================

            try:

                result = response.json()

            except Exception:

                last_error = (
                    "Invalid OCR JSON"
                )

                break


            # =================================================
            # OCR ERROR
            # =================================================

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
                    f"PAGE {page_number}:",
                    last_error
                )

                break


            # =================================================
            # TEXT
            # =================================================

            parsed = result.get(
                "ParsedResults",
                []
            )

            text_parts = []

            for item in parsed:

                text = item.get(
                    "ParsedText",
                    ""
                )

                if text:

                    text_parts.append(
                        text
                    )

            text = "\n".join(
                text_parts
            ).strip()


            if text:

                print(
                    f"PAGE {page_number}: "
                    f"OCR success "
                    f"({len(text)} chars)"
                )

                return (
                    text,
                    None
                )


            last_error = (
                "OCR returned empty text"
            )

            break


        # =====================================================
        # Next image size
        # =====================================================

        time.sleep(
            OCR_REQUEST_DELAY
        )


    return (
        "",
        last_error or "OCR failed"
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

    pages = {}

    failed_pages = []

    try:

        total = len(pdf)

        print(
            f"OCR START: "
            f"{total} pages"
        )

        for index in range(total):

            page_number = index + 1

            print(
                "\n"
                f"========================\n"
                f"PAGE {page_number}/{total}\n"
                f"========================"
            )


            if progress_callback:

                progress_callback(
                    page_number,
                    total
                )


            page = pdf[index]

            text, error = ocr_page(
                page,
                page_number
            )


            if text:

                pages[
                    page_number
                ] = text

            else:

                pages[
                    page_number
                ] = "[OCR FAILED]"

                failed_pages.append(
                    page_number
                )

                print(
                    f"PAGE {page_number} FAILED:",
                    error
                )


    finally:

        pdf.close()


    # ========================================================
    # BUILD TXT
    # ========================================================

    output = []

    for page_number in range(
        1,
        total + 1
    ):

        output.append(
            f"\n--- Page {page_number} ---\n"
        )

        output.append(
            pages.get(
                page_number,
                "[OCR FAILED]"
            )
        )

        output.append("\n")


    result = "".join(
        output
    ).strip()


    print(
        "\nOCR COMPLETE"
    )

    print(
        "Text length:",
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
# PROGRESS CALLBACK
# ============================================================

def make_progress_callback(
    loop,
    message
):

    last_update = {
        "time": 0
    }

    def callback(
        current,
        total
    ):

        now = time.time()

        # Telegram ကို message
        # အရမ်းများမပို့ရန်
        if (
            current != total
            and now - last_update["time"] < 8
        ):

            return

        last_update["time"] = now

        percent = int(
            current * 100 / total
        )

        text = (

            "🔎 Myanmar OCR လုပ်နေပါတယ်...\n\n"

            f"📄 Page: {current}/{total}\n"

            f"📊 Progress: {percent}%\n\n"

            "⏳ ကျန်တဲ့ page တွေကို "
            "ဆက်ဖတ်နေပါတယ်..."
        )

        try:

            future = asyncio.run_coroutine_threadsafe(

                message.edit_text(
                    text
                ),

                loop
            )

        except Exception:

            pass

    return callback


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


    # ========================================================
    # LOCK USER
    # ========================================================

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

        # ====================================================
        # DOWNLOAD
        # ====================================================

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


        # ====================================================
        # NORMAL EXTRACTION
        # ====================================================

        await processing_message.edit_text(

            "📄 PDF Text Encoding "
            "စစ်နေပါတယ်..."
        )


        normal_text = await asyncio.to_thread(

            extract_pdf_text,
            input_path
        )


        # ====================================================
        # CHECK
        # ====================================================

        bad_encoding = (
            is_bad_myanmar_text(
                normal_text
            )
        )


        # ====================================================
        # NORMAL TEXT OK
        # ====================================================

        if (
            len(normal_text.strip()) >= 100
            and not bad_encoding
        ):

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

                    "📄 Normal PDF text "
                    "extraction အသုံးပြုထားပါတယ်။"
                )
            )

            context.user_data[
                "waiting_for_pdf"
            ] = False

            return


        # ====================================================
        # OCR FALLBACK
        # ====================================================

        await processing_message.edit_text(

            "🔎 PDF Text Encoding "
            "မမှန်ပါ။\n\n"

            "🇲🇲 Myanmar OCR စတင်နေပါတယ်...\n\n"

            "📄 PDF ကို page-by-page "
            "ဖတ်ပါမယ်။\n\n"

            "⚠️ OCR API limit မဖြစ်အောင် "
            "request ကို ထိန်းပြီး ပို့ပါမယ်။"
        )


        # ====================================================
        # PAGE COUNT
        # ====================================================

        pdf = fitz.open(
            input_path
        )

        total_pages = len(pdf)

        pdf.close()


        await processing_message.edit_text(

            "🔎 Myanmar OCR စတင်နေပါတယ်...\n\n"

            f"📄 Total pages: "
            f"{total_pages}\n\n"

            "⏳ Page တစ်မျက်နှာချင်းစီ "
            "ဖတ်နေပါတယ်။\n\n"

            "⚠️ OCR API limit ရှိလို့ "
            "request ကို ထိန်းပြီး ပို့ပါမယ်။"
        )


        # ====================================================
        # OCR
        # ====================================================

        loop = asyncio.get_running_loop()

        callback = make_progress_callback(
            loop,
            processing_message
        )


        result, failed_pages = (
            await asyncio.to_thread(

                process_pdf_ocr,

                input_path,

                callback
            )
        )


        # ====================================================
        # REMOVE INPUT
        # ====================================================

        if (
            input_path
            and os.path.exists(input_path)
        ):

            os.remove(
                input_path
            )

            input_path = None


        if not result.strip():

            await processing_message.edit_text(

                "❌ OCR နဲ့ စာဖတ်လို့မရပါ။\n\n"

                "OCR API limit သို့မဟုတ် "
                "PDF image quality ပြဿနာ "
                "ဖြစ်နိုင်ပါတယ်။"
            )

            return


        # ====================================================
        # CREATE TXT
        # ====================================================

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


        # ====================================================
        # CAPTION
        # ====================================================

        if failed_pages:

            failed = ", ".join(
                str(x)
                for x in failed_pages
            )

            caption = (

                "⚠️ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 Myanmar OCR အသုံးပြုထားပါတယ်။\n\n"

                f"⚠️ OCR မအောင်မြင်သော page: "
                f"{failed}"
            )

        else:

            caption = (

                "✅ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 Myanmar OCR အသုံးပြုထားပါတယ်။\n\n"

                "✅ Page အားလုံး OCR ပြီးပါပြီ။"
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
            "PDF ERROR:",
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
        "AI PDF Helper starting..."
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


    # ========================================================
    # RENDER PORT
    # ========================================================

    threading.Thread(

        target=start_health_server,

        daemon=True

    ).start()


    # ========================================================
    # TELEGRAM
    # ========================================================

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


    # ========================================================
    # POLLING
    # ========================================================

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

import os
import io
import re
import time
import asyncio
import threading
import tempfile
import unicodedata

from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import fitz

from PIL import Image, ImageOps

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


# ============================================================
# OCR IMAGE SETTINGS
# ============================================================

# 413 ဖြစ်ရင် အပေါ်ကနေ အောက်ကို image size လျှော့ပြီး retry
OCR_SETTINGS = [
    (2200, 75),
    (2000, 70),
    (1800, 65),
    (1600, 60),
    (1400, 55),
    (1200, 50),
]


# 429 rate limit ဖြစ်ရင် စောင့်မယ့်အချိန်
RATE_LIMIT_DELAYS = [
    10,
    20,
    40,
    60,
]


# Page တစ်မျက်နှာကို OCR request အများဆုံး
MAX_OCR_ATTEMPTS = len(OCR_SETTINGS)


# ============================================================
# USER PROCESS LOCK
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

            "🇲🇲 Myanmar OCR Cleanup ပါဝင်ပါတယ်။"
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

            "🇲🇲 Myanmar OCR\n"
            "→ Scanned PDF များအတွက်"
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
                    f"Page {index} extraction error:",
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
# MYANMAR TEXT CLEANUP
# ============================================================

def clean_myanmar_text(text):

    if not text:
        return ""


    # --------------------------------------------------------
    # Unicode normalization
    # --------------------------------------------------------

    text = unicodedata.normalize(
        "NFC",
        text
    )


    # --------------------------------------------------------
    # Remove NULL/control characters
    # --------------------------------------------------------

    text = text.replace(
        "\x00",
        ""
    )

    text = re.sub(
        r"[\x01-\x08\x0B\x0C\x0E-\x1F]",
        "",
        text
    )


    # --------------------------------------------------------
    # Normalize line endings
    # --------------------------------------------------------

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )


    # --------------------------------------------------------
    # Normalize spaces
    # --------------------------------------------------------

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )


    # --------------------------------------------------------
    # Remove spaces at beginning/end
    # --------------------------------------------------------

    lines = []

    for line in text.split("\n"):

        line = line.strip()

        if line:

            lines.append(line)

        else:

            # မျက်နှာစာကြား blank line
            # အလွန်များမသွားအောင်
            if lines and lines[-1] != "":

                lines.append("")


    text = "\n".join(lines)


    # --------------------------------------------------------
    # Excessive blank lines
    # --------------------------------------------------------

    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text
    )


    # --------------------------------------------------------
    # OCR မှာ မကြာခဏ ထပ်နေတဲ့ spaces
    # --------------------------------------------------------

    text = re.sub(
        r" {2,}",
        " ",
        text
    )


    # --------------------------------------------------------
    # Myanmar punctuation spacing cleanup
    # --------------------------------------------------------

    text = re.sub(
        r"\s+([၊။！？])",
        r"\1",
        text
    )


    # --------------------------------------------------------
    # English punctuation spacing
    # --------------------------------------------------------

    text = re.sub(
        r"\s+([,.!?;:])",
        r"\1",
        text
    )


    # --------------------------------------------------------
    # Space after Myanmar punctuation
    # --------------------------------------------------------

    text = re.sub(
        r"([၊။])([^\s])",
        r"\1 \2",
        text
    )


    # --------------------------------------------------------
    # Common OCR junk
    # --------------------------------------------------------

    junk_patterns = [
        r"^[|]+$",
        r"^[_]+$",
        r"^[-]{5,}$",
        r"^[~`]+$",
    ]

    cleaned_lines = []

    for line in text.split("\n"):

        stripped = line.strip()

        is_junk = False

        for pattern in junk_patterns:

            if re.fullmatch(
                pattern,
                stripped
            ):

                is_junk = True
                break

        if not is_junk:

            cleaned_lines.append(
                line
            )


    text = "\n".join(
        cleaned_lines
    )


    return text.strip()


# ============================================================
# CREATE OCR IMAGE
# ============================================================

def create_ocr_image(
    page,
    max_dimension,
    quality
):

    # --------------------------------------------------------
    # Render page
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # Auto contrast
    # --------------------------------------------------------

    try:

        image = ImageOps.autocontrast(
            image
        )

    except Exception:

        pass


    width, height = image.size

    largest = max(
        width,
        height
    )


    # --------------------------------------------------------
    # Resize
    # --------------------------------------------------------

    if largest > max_dimension:

        scale = (
            max_dimension
            /
            largest
        )

        new_size = (

            max(
                1,
                int(width * scale)
            ),

            max(
                1,
                int(height * scale)
            )
        )

        image = image.resize(
            new_size,
            Image.Resampling.LANCZOS
        )


    # --------------------------------------------------------
    # JPEG
    # --------------------------------------------------------

    buffer = io.BytesIO()

    image.save(

        buffer,

        format="JPEG",

        quality=quality,

        optimize=True,

        progressive=True
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

            "apikey":
                OCR_API_KEY
        },

        files={

            "file": (

                "page.jpg",

                image_buffer,

                "image/jpeg"
            )
        },

        data={

            # Myanmar OCR
            "language": "mya",

            "OCREngine": "2",

            "isOverlayRequired":
                "false",

            "detectOrientation":
                "true",

            "scale":
                "true",

            "isTable":
                "false",
        },

        timeout=OCR_TIMEOUT
    )


    return response


# ============================================================
# OCR ERROR MESSAGE
# ============================================================

def get_ocr_error_message(
    result
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


    return str(
        message
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
            "OCR_API_KEY မတွေ့ပါ"
        )


    last_error = (
        "Unknown OCR error"
    )


    rate_limit_count = 0


    for attempt, (
        max_dimension,
        quality
    ) in enumerate(
        OCR_SETTINGS,
        start=1
    ):

        print(

            f"OCR page {page_number}: "

            f"attempt {attempt}/"
            f"{MAX_OCR_ATTEMPTS} "

            f"size={max_dimension} "

            f"quality={quality}"
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

                f"OCR page {page_number}: "

                f"image="
                f"{image_size / 1024:.1f} KB"
            )


            response = (
                send_ocr_request(
                    image_buffer
                )
            )


            status = (
                response.status_code
            )


            print(

                f"OCR page {page_number}: "

                f"HTTP {status}"
            )


            # =================================================
            # 413 PAYLOAD TOO LARGE
            # =================================================

            if status == 413:

                last_error = (
                    "HTTP 413"
                )

                print(

                    f"Page {page_number}: "

                    "413 -> "
                    "reducing image size"
                )

                continue


            # =================================================
            # 429 RATE LIMIT
            # =================================================

            if status == 429:

                last_error = (
                    "HTTP 429"
                )

                if rate_limit_count < len(
                    RATE_LIMIT_DELAYS
                ):

                    delay = (
                        RATE_LIMIT_DELAYS[
                            rate_limit_count
                        ]
                    )

                    rate_limit_count += 1

                    print(

                        f"Page {page_number}: "

                        f"OCR rate limited. "

                        f"Waiting {delay}s..."
                    )


                    time.sleep(
                        delay
                    )

                    # Same image size ကို
                    # retry ပြန်လုပ်
                    continue

                else:

                    print(

                        f"Page {page_number}: "

                        "429 retry limit reached"
                    )

                    break


            # =================================================
            # OTHER HTTP ERROR
            # =================================================

            if status != 200:

                last_error = (
                    f"HTTP {status}"
                )

                print(

                    f"Page {page_number}: "

                    f"{last_error}"
                )

                time.sleep(3)

                continue


            # =================================================
            # JSON
            # =================================================

            try:

                result = response.json()

            except Exception as e:

                last_error = (
                    "Invalid JSON response"
                )

                print(

                    f"Page {page_number}: "

                    "Invalid JSON:",
                    repr(e)
                )

                continue


            # =================================================
            # OCR API ERROR
            # =================================================

            if result.get(
                "IsErroredOnProcessing"
            ):

                last_error = (
                    get_ocr_error_message(
                        result
                    )
                )

                print(

                    f"Page {page_number}: "

                    f"OCR error: "
                    f"{last_error}"
                )

                continue


            # =================================================
            # PARSED RESULTS
            # =================================================

            parsed_results = (
                result.get(
                    "ParsedResults",
                    []
                )
            )


            text_parts = []


            for item in parsed_results:

                parsed_text = (
                    item.get(
                        "ParsedText",
                        ""
                    )
                )


                if parsed_text:

                    text_parts.append(
                        parsed_text
                    )


            raw_text = "\n".join(
                text_parts
            ).strip()


            # =================================================
            # CLEANUP
            # =================================================

            text = clean_myanmar_text(
                raw_text
            )


            print(

                f"OCR page {page_number}: "

                f"raw={len(raw_text)} "

                f"clean={len(text)}"
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


    total_pages = len(
        pdf
    )


    all_pages = {}

    failed_pages = []


    try:

        print(

            f"OCR starting: "

            f"{total_pages} page(s)"
        )


        # =====================================================
        # FIRST PASS
        # =====================================================

        for index in range(
            total_pages
        ):

            page_number = (
                index + 1
            )


            print(

                f"===================================="

            )

            print(

                f"OCR page "
                f"{page_number}/"
                f"{total_pages}"
            )


            page = pdf[index]


            text, error = (
                ocr_page(

                    page,

                    page_number
                )
            )


            if text:

                all_pages[
                    page_number
                ] = text

            else:

                print(

                    f"OCR failed on page "
                    f"{page_number}: "
                    f"{error}"
                )

                failed_pages.append(
                    page_number
                )


        # =====================================================
        # SECOND PASS
        # =====================================================

        if failed_pages:

            print(

                "===================================="
            )

            print(

                "Retrying failed pages:",
                failed_pages
            )


            remaining_failed = []


            for page_number in failed_pages:

                print(

                    f"Retry page "
                    f"{page_number}"
                )


                page = pdf[
                    page_number - 1
                ]


                # Rate-limit ကပ်နေရင်
                # retry အတွက် ခဏစောင့်
                time.sleep(2)


                text, error = (
                    ocr_page(

                        page,

                        page_number
                    )
                )


                if text:

                    all_pages[
                        page_number
                    ] = text

                    print(

                        f"Retry SUCCESS "
                        f"page {page_number}"
                    )

                else:

                    remaining_failed.append(
                        page_number
                    )

                    print(

                        f"Retry FAILED "
                        f"page {page_number}: "
                        f"{error}"
                    )


            failed_pages = (
                remaining_failed
            )


    finally:

        pdf.close()


    # =========================================================
    # BUILD FINAL TEXT IN PAGE ORDER
    # =========================================================

    output_parts = []


    for page_number in range(
        1,
        total_pages + 1
    ):

        output_parts.append(

            f"\n--- Page "
            f"{page_number} ---\n"
        )


        if page_number in all_pages:

            output_parts.append(
                all_pages[
                    page_number
                ]
            )

        else:

            output_parts.append(
                "[OCR FAILED]"
            )


    result = "\n".join(
        output_parts
    ).strip()


    # =========================================================
    # FINAL CLEANUP
    # =========================================================

    result = clean_myanmar_text(
        result
    )


    print(
        "Total OCR text length:",
        len(result)
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
        result,
        failed_pages
    )


# ============================================================
# PDF BACKGROUND PROCESSOR
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


    # =========================================================
    # USER LOCK
    # =========================================================

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
    output_path = None


    try:

        # =====================================================
        # DOWNLOAD
        # =====================================================

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
            "PDF downloaded:",
            filename
        )


        # =====================================================
        # NORMAL TEXT EXTRACTION
        # =====================================================

        await processing_message.edit_text(

            "📄 PDF စာသားကို "
            "အရင် Extract လုပ်နေပါတယ်..."
        )


        normal_text = (
            extract_pdf_text(
                input_path
            )
        )


        normal_text = (
            clean_myanmar_text(
                normal_text
            )
        )


        # =====================================================
        # NORMAL PDF
        # =====================================================

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

                    "📄 Normal PDF text extraction "
                    "အသုံးပြုထားပါတယ်။"
                )
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


            return


        # =====================================================
        # OCR REQUIRED
        # =====================================================

        pdf = fitz.open(
            input_path
        )


        total_pages = len(
            pdf
        )


        pdf.close()


        await processing_message.edit_text(

            "🔎 Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"

            "🇲🇲 Myanmar OCR နဲ့ "
            "စာဖတ်နေပါတယ်...\n\n"

            f"📄 Pages: {total_pages}\n\n"

            "⏳ Page တစ်မျက်နှာချင်းစီ "
            "လုပ်နေပါတယ်။\n\n"

            "🔄 413 / 429 ဖြစ်ရင် "
            "အလိုအလျောက် retry လုပ်ပါမယ်။"
        )


        # =====================================================
        # RUN OCR IN BACKGROUND
        # =====================================================

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


        # =====================================================
        # TXT
        # =====================================================

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


        # =====================================================
        # CAPTION
        # =====================================================

        if failed_pages:

            failed_text = ", ".join(
                str(x)
                for x in failed_pages
            )


            caption = (

                "⚠️ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 Myanmar OCR + "
                "Text Cleanup အသုံးပြုထားပါတယ်။\n\n"

                f"⚠️ OCR မအောင်မြင်သော page: "
                f"{failed_text}"
            )

        else:

            caption = (

                "✅ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 Myanmar OCR + "
                "Text Cleanup အသုံးပြုထားပါတယ်။"
            )


        # =====================================================
        # SEND TXT
        # =====================================================

        await update.message.reply_document(

            document=output_path,

            caption=caption
        )


        # =====================================================
        # CLEAN FILE
        # =====================================================

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

        # -----------------------------------------------------
        # Remove input
        # -----------------------------------------------------

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


        # -----------------------------------------------------
        # Remove output
        # -----------------------------------------------------

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


        # -----------------------------------------------------
        # Release user lock
        # -----------------------------------------------------

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

    error = context.error

    print(
        "Telegram error:",
        repr(error)
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "===================================="
    )

    print(
        "AI PDF Helper Bot starting..."
    )

    print(
        "===================================="
    )


    # ========================================================
    # BOT TOKEN
    # ========================================================

    if not BOT_TOKEN:

        raise ValueError(

            "BOT_TOKEN မတွေ့ပါ။"
        )


    # ========================================================
    # OCR KEY
    # ========================================================

    if not OCR_API_KEY:

        print(

            "WARNING: OCR_API_KEY "
            "မတွေ့ပါ။"
        )

    else:

        print(
            "OCR_API_KEY detected."
        )


    # ========================================================
    # RENDER HEALTH SERVER
    # ========================================================

    threading.Thread(

        target=start_health_server,

        daemon=True
    ).start()


    # ========================================================
    # TELEGRAM APP
    # ========================================================

    app = (

        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )


    # ========================================================
    # HANDLERS
    # ========================================================

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

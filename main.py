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

# Myanmar encoding
try:
    from myanmartools import ZawgyiDetector
except Exception:
    ZawgyiDetector = None

try:
    from icu import Transliterator
except Exception:
    Transliterator = None

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

PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)

OCR_URL = (
    "https://api.ocr.space/parse/image"
)

OCR_TIMEOUT = 120

# ------------------------------------------------------------
# OCR settings
# ------------------------------------------------------------

OCR_SETTINGS = [
    (1800, 60),
    (1600, 50),
    (1400, 45),
]

# 429 ကို အများကြီး retry မလုပ်ပါ
OCR_429_RETRIES = 2

# Request ကြား
OCR_REQUEST_DELAY = 2.0

# ------------------------------------------------------------
# Processing
# ------------------------------------------------------------

PROCESSING_USERS = set()

PROCESSING_LOCK = threading.Lock()


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

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

    def log_message(
        self,
        format,
        *args
    ):

        return


def start_health_server():

    try:

        server = HTTPServer(
            (
                "0.0.0.0",
                PORT
            ),
            HealthHandler
        )

        print(
            f"Health server running "
            f"on port {PORT}"
        )

        server.serve_forever()

    except Exception as e:

        print(
            "Health server error:",
            repr(e)
        )


# ============================================================
# MAIN KEYBOARD
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
        "🇲🇲 Myanmar PDF Support\n"
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

    # --------------------------------------------------------
    # PDF
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

            ]

        ])

        await query.edit_message_text(

            "📄 PDF Tools\n\n"

            "PDF → Text ကိုရွေးပါ 👇",

            reply_markup=keyboard
        )

    # --------------------------------------------------------
    # PDF → TEXT
    # --------------------------------------------------------

    elif query.data == "pdf_to_text":

        context.user_data[
            "waiting_for_pdf"
        ] = True

        await query.edit_message_text(

            "📄 PDF → Text\n\n"

            "PDF ဖိုင်တစ်ခု ပို့ပေးပါ။\n\n"

            "🔹 Normal PDF\n"
            "→ Text extraction\n\n"

            "🔹 Zawgyi PDF\n"
            "→ Unicode conversion\n\n"

            "🔹 Myanmar encoding ပျက်နေသော PDF\n"
            "→ OCR fallback\n\n"

            "🔹 Scanned PDF\n"
            "→ Myanmar OCR"

        )

    # --------------------------------------------------------
    # AI
    # --------------------------------------------------------

    elif query.data == "ai":

        await query.edit_message_text(

            "🤖 AI Tools\n\n"

            "AI Features မကြာခင် ထည့်ပေးပါမယ်။"
        )

    # --------------------------------------------------------
    # PREMIUM
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

            "🇲🇲 Myanmar PDF\n"
            "→ Encoding check\n"
            "→ Zawgyi conversion\n"
            "→ OCR fallback"

        )

    # --------------------------------------------------------
    # BACK
    # --------------------------------------------------------

    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=main_keyboard()

        )


# ============================================================
# MYANMAR CHARACTER COUNT
# ============================================================

def count_myanmar_chars(
    text
):

    count = 0

    for ch in text:

        code = ord(ch)

        if (
            0x1000
            <= code
            <= 0x109F
        ):

            count += 1

    return count


# ============================================================
# SUSPICIOUS ENCODING
# ============================================================

def suspicious_score(
    text
):

    if not text:

        return 999999

    patterns = [

        "ေြ",
        "ြေ",
        "ေျ",
        "ျေ",
        "်ေ",
        "ေ်",
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

        score += (
            text.count(pattern)
        )

    return score


# ============================================================
# ZAWGYI DETECTION
# ============================================================

def detect_zawgyi(
    text
):

    if not text:

        return 0.0

    if not ZawgyiDetector:

        return 0.0

    try:

        detector = (
            ZawgyiDetector()
        )

        score = (
            detector
            .get_zawgyi_probability(
                text
            )
        )

        print(
            f"Zawgyi probability: "
            f"{score:.4f}"
        )

        return float(score)

    except Exception as e:

        print(
            "Zawgyi detection error:",
            repr(e)
        )

        return 0.0


# ============================================================
# ZAWGYI → UNICODE
# ============================================================

def zawgyi_to_unicode(
    text
):

    if not text:

        return text

    if not Transliterator:

        print(
            "PyICU not available."
        )

        return text

    try:

        converter = (
            Transliterator
            .createInstance(
                "Zawgyi-my"
            )
        )

        converted = (
            converter.transliterate(
                text
            )
        )

        return converted

    except Exception as e:

        print(
            "Zawgyi conversion error:",
            repr(e)
        )

        return text


# ============================================================
# NORMALIZE / CLEAN TEXT
# ============================================================

def clean_text(
    text
):

    if not text:

        return ""

    # Null characters
    text = text.replace(
        "\x00",
        ""
    )

    # CRLF
    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    return text.strip()


# ============================================================
# TEXT QUALITY
# ============================================================

def text_quality(
    text
):

    text = clean_text(
        text
    )

    if len(text) < 50:

        return {
            "good": False,
            "reason": "too_short",
            "score": 999999
        }

    myanmar = (
        count_myanmar_chars(
            text
        )
    )

    suspicious = (
        suspicious_score(
            text
        )
    )

    # --------------------------------------------------------
    # Zawgyi
    # --------------------------------------------------------

    zawgyi_probability = (
        detect_zawgyi(
            text
        )
    )

    print(
        "Text quality:"
    )

    print(
        "  length =",
        len(text)
    )

    print(
        "  Myanmar chars =",
        myanmar
    )

    print(
        "  suspicious =",
        suspicious
    )

    print(
        "  Zawgyi probability =",
        zawgyi_probability
    )

    # --------------------------------------------------------
    # Strong encoding corruption
    # --------------------------------------------------------

    if (
        myanmar >= 20
        and suspicious >= 8
    ):

        return {
            "good": False,
            "reason": "broken_encoding",
            "score": suspicious
        }

    # --------------------------------------------------------
    # No Myanmar text
    # --------------------------------------------------------

    if (
        myanmar == 0
        and len(text) > 100
    ):

        return {
            "good": False,
            "reason": "no_myanmar",
            "score": suspicious
        }

    # --------------------------------------------------------
    # Good
    # --------------------------------------------------------

    return {
        "good": True,
        "reason": "acceptable",
        "score": suspicious
    }


# ============================================================
# FIX EXTRACTED TEXT
# ============================================================

def fix_extracted_text(
    text
):

    text = clean_text(
        text
    )

    if not text:

        return (
            "",
            False,
            "empty"
        )

    # --------------------------------------------------------
    # Detect Zawgyi
    # --------------------------------------------------------

    probability = (
        detect_zawgyi(
            text
        )
    )

    # Strong Zawgyi
    if probability >= 0.80:

        print(
            "Zawgyi detected."
        )

        converted = (
            zawgyi_to_unicode(
                text
            )
        )

        before_score = (
            suspicious_score(
                text
            )
        )

        after_score = (
            suspicious_score(
                converted
            )
        )

        print(
            "Zawgyi conversion:"
        )

        print(
            "  before =",
            before_score
        )

        print(
            "  after =",
            after_score
        )

        # Converted text ပိုကောင်းရင်သာ
        # အသုံးပြု
        if after_score <= before_score:

            text = converted

            print(
                "Converted text accepted."
            )

        else:

            print(
                "Converted text rejected."
            )

    quality = text_quality(
        text
    )

    return (
        text,
        quality["good"],
        quality["reason"]
    )


# ============================================================
# EXTRACT PDF TEXT
# ============================================================

def extract_pdf_text(
    pdf_path
):

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
                f"Extracting page "
                f"{index}/"
                f"{len(reader.pages)}"
            )

            try:

                text = (
                    page.extract_text()
                )

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

        result = (
            "\n".join(
                parts
            )
        )

        return clean_text(
            result
        )

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
        (
            pix.width,
            pix.height
        ),
        pix.samples
    )

    width, height = (
        image.size
    )

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
                int(
                    width * scale
                ),
                int(
                    height * scale
                )
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

            # Burmese
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
            f"OCR attempt "
            f"{attempt}/"
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

            size_kb = (
                len(
                    image_buffer.getvalue()
                )
                / 1024
            )

            print(
                f"Page {page_number}: "
                f"{size_kb:.1f} KB"
            )

            # ------------------------------------------------
            # Request
            # ------------------------------------------------

            response = (
                send_ocr_request(
                    image_buffer
                )
            )

            status = (
                response.status_code
            )

            print(
                f"Page {page_number}: "
                f"HTTP {status}"
            )

            # ------------------------------------------------
            # 429
            # ------------------------------------------------

            if status == 429:

                last_error = (
                    "OCR API HTTP 429"
                )

                retry_after = (
                    response
                    .headers
                    .get(
                        "Retry-After"
                    )
                )

                if retry_after:

                    try:

                        wait = min(
                            int(
                                retry_after
                            ),
                            60
                        )

                    except Exception:

                        wait = 20

                else:

                    wait = 20

                print(
                    f"Page {page_number}: "
                    f"429 → wait "
                    f"{wait}s"
                )

                time.sleep(
                    wait
                )

                # 429 ဖြစ်ရင်
                # နောက် image size မပြောင်းခင်
                # တစ်ကြိမ်ပဲ ထပ်ကြည့်
                if attempt < (
                    OCR_429_RETRIES
                ):

                    continue

                return (
                    "",
                    last_error
                )

            # ------------------------------------------------
            # 413
            # ------------------------------------------------

            if status == 413:

                last_error = (
                    "OCR API HTTP 413"
                )

                print(
                    "413 → smaller image"
                )

                continue

            # ------------------------------------------------
            # Other HTTP
            # ------------------------------------------------

            if status != 200:

                last_error = (
                    f"OCR API HTTP "
                    f"{status}"
                )

                continue

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:

                result = (
                    response.json()
                )

            except Exception as e:

                last_error = (
                    "Invalid OCR JSON"
                )

                print(
                    repr(e)
                )

                continue

            # ------------------------------------------------
            # API error
            # ------------------------------------------------

            if result.get(
                "IsErroredOnProcessing"
            ):

                message = (
                    result.get(
                        "ErrorMessage",
                        "OCR processing error"
                    )
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
                    "OCR error:",
                    last_error
                )

                continue

            # ------------------------------------------------
            # Parsed results
            # ------------------------------------------------

            parsed_results = (
                result.get(
                    "ParsedResults",
                    []
                )
            )

            text_parts = []

            for item in (
                parsed_results
            ):

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

            text = (
                "\n".join(
                    text_parts
                )
                .strip()
            )

            if text:

                print(
                    f"Page {page_number}: "
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

        except requests.Timeout:

            last_error = (
                "OCR timeout"
            )

            print(
                f"Page {page_number}: "
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
                f"Page {page_number}: "
                "unexpected error:",
                repr(e)
            )

        # Request ကြား
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
    pdf_path
):

    pdf = fitz.open(
        pdf_path
    )

    pages = {}

    failed_pages = []

    try:

        total = len(pdf)

        print(
            f"OCR starting: "
            f"{total} pages"
        )

        for index in range(
            total
        ):

            page_number = (
                index + 1
            )

            page = pdf[index]

            print(
                f"OCR PAGE "
                f"{page_number}/"
                f"{total}"
            )

            text, error = (
                ocr_page(
                    page,
                    page_number
                )
            )

            if text:

                # OCR result ကို
                # Unicode normalize မလုပ်ခင်
                # clean လုပ်
                text = clean_text(
                    text
                )

                pages[
                    page_number
                ] = text

            else:

                pages[
                    page_number
                ] = (
                    "[OCR FAILED]"
                )

                failed_pages.append(
                    page_number
                )

                print(
                    f"FAILED page "
                    f"{page_number}: "
                    f"{error}"
                )

    finally:

        pdf.close()

    # --------------------------------------------------------
    # Build page order
    # --------------------------------------------------------

    output = []

    for page_number in sorted(
        pages.keys()
    ):

        output.append(

            f"\n--- Page "
            f"{page_number} ---\n"
        )

        output.append(
            pages[
                page_number
            ]
        )

        output.append(
            "\n"
        )

    result = "".join(
        output
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
# PDF BACKGROUND
# ============================================================

def process_pdf_background(
    pdf_path
):

    return process_pdf_ocr(
        pdf_path
    )


# ============================================================
# SEND TXT
# ============================================================

async def send_txt_file(
    update,
    output_path,
    caption
):

    try:

        await update.message.reply_document(

            document=output_path,

            caption=caption

        )

    finally:

        if os.path.exists(
            output_path
        ):

            try:

                os.remove(
                    output_path
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
    # Prevent duplicate jobs
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

            "❌ PDF ဖိုင်ပဲ "
            "ပို့ပေးပါ။"

        )

        return

    processing_message = (
        await update.message.reply_text(

            "⏳ PDF ကို "
            "download လုပ်နေပါတယ်..."
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

        print(
            f"Downloaded: "
            f"{filename}"
        )

        # ====================================================
        # NORMAL EXTRACTION
        # ====================================================

        await processing_message.edit_text(

            "📄 PDF စာသားကို "
            "စစ်နေပါတယ်...\n\n"

            "🇲🇲 Myanmar encoding "
            "စစ်ဆေးပါမယ်။"

        )

        normal_text = (
            await asyncio.to_thread(

                extract_pdf_text,

                input_path

            )
        )

        # ====================================================
        # FIX / CHECK
        # ====================================================

        (
            fixed_text,
            is_good,
            reason
        ) = await asyncio.to_thread(

            fix_extracted_text,

            normal_text

        )

        print(
            "Extraction result:",
            reason
        )

        # ====================================================
        # NORMAL TEXT ACCEPT
        # ====================================================

        if (
            len(
                fixed_text.strip()
            ) >= 100
            and is_good
        ):

            print(
                "NORMAL TEXT ACCEPTED"
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
                    fixed_text
                )

            try:

                await processing_message.delete()

            except Exception:

                pass

            await send_txt_file(

                update,

                output_path,

                "✅ PDF → Text ပြီးပါပြီ။\n\n"

                "📄 PDF Text Extraction "
                "အသုံးပြုထားပါတယ်။\n\n"

                "🇲🇲 Myanmar text encoding "
                "စစ်ဆေးပြီး ထုတ်ပေးထားပါတယ်။"

            )

            context.user_data[
                "waiting_for_pdf"
            ] = False

            return

        # ====================================================
        # OCR FALLBACK
        # ====================================================

        print(
            "NORMAL TEXT REJECTED"
        )

        print(
            "Reason:",
            reason
        )

        pdf = fitz.open(
            input_path
        )

        total_pages = len(
            pdf
        )

        pdf.close()

        await processing_message.edit_text(

            "🔎 PDF Text Encoding "
            "မမှန်ပါ သို့မဟုတ် "
            "Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"

            "🇲🇲 Myanmar OCR "
            "စတင်နေပါတယ်...\n\n"

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

        result, failed_pages = (
            await asyncio.to_thread(

                process_pdf_background,

                input_path

            )
        )

        # Input PDF delete
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

            context.user_data[
                "waiting_for_pdf"
            ] = False

            return

        # ====================================================
        # TXT
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

        await send_txt_file(

            update,

            output_path,

            caption

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
        repr(
            context.error
        )
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "AI PDF Helper starting..."
    )

    print(
        "================================"
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

    if not ZawgyiDetector:

        print(
            "WARNING: "
            "myanmartools မတွေ့ပါ။"
        )

    if not Transliterator:

        print(
            "WARNING: "
            "PyICU မတွေ့ပါ။"
        )

    # ========================================================
    # Render health
    # ========================================================

    threading.Thread(

        target=start_health_server,

        daemon=True

    ).start()

    # ========================================================
    # Telegram Application
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
# ENTRY
# ============================================================

if __name__ == "__main__":

    main()

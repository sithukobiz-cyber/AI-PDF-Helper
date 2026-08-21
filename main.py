import os
import io
import re
import time
import json
import math
import sqlite3
import asyncio
import logging
import threading
import tempfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import requests
import pymupdf
from PIL import Image
from icu import Transliterator
from myanmartools import ZawgyiDetector

from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)

from telegram.constants import ChatAction

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# IMPORTANT:
# OpenAI model ID must be a model available to your API project.
# If "gpt-5.6-luna" is not an actual model available to the key,
# OpenAI will return a model-not-found error.
OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-5.6-luna"
).strip()

OCR_API_KEY = os.getenv(
    "OCR_API_KEY",
    ""
).strip()

PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "bot.db"
)

TIMEZONE = os.getenv(
    "TIMEZONE",
    "Asia/Yangon"
)

OCR_URL = (
    "https://api.ocr.space/parse/image"
)

OCR_TIMEOUT = 120

MAX_TELEGRAM_MESSAGE = 3900

MAX_PDF_SIZE_MB = 50

OCR_REQUEST_DELAY = 1.5

OCR_429_RETRIES = 5

OCR_SETTINGS = [
    (2200, 70),
    (2000, 62),
    (1800, 55),
    (1600, 48),
    (1400, 42),
]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s - "
        "%(name)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "ai-pdf-helper"
)


# ============================================================
# GLOBAL CLIENTS
# ============================================================

openai_client = None

if OPENAI_API_KEY:

    openai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY
    )


# ============================================================
# LOCKS
# ============================================================

DB_LOCK = threading.Lock()

PROCESSING_USERS = set()

PROCESSING_LOCK = threading.Lock()


# ============================================================
# DATABASE
# ============================================================

def db_connect():

    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
        check_same_thread=False
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():

    with DB_LOCK:

        connection = db_connect()

        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                done INTEGER DEFAULT 0
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS study_progress (
                user_id INTEGER PRIMARY KEY,
                subject TEXT,
                lesson INTEGER DEFAULT 1,
                score INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )

        connection.commit()

        connection.close()

    logger.info(
        "Database initialized."
    )


def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def register_user(user):

    if not user:
        return

    timestamp = now_iso()

    with DB_LOCK:

        connection = db_connect()

        connection.execute(
            """
            INSERT INTO users
            (
                user_id,
                username,
                first_name,
                created_at,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                timestamp,
                timestamp,
            )
        )

        connection.commit()

        connection.close()


def save_document(
    user_id,
    filename,
    content
):

    with DB_LOCK:

        connection = db_connect()

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO documents
            (
                user_id,
                filename,
                content,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                filename,
                content,
                now_iso(),
            )
        )

        document_id = cursor.lastrowid

        connection.commit()

        connection.close()

    return document_id


def search_documents(
    user_id,
    keyword,
    limit=10
):

    keyword = keyword.strip()

    if not keyword:
        return []

    with DB_LOCK:

        connection = db_connect()

        rows = connection.execute(
            """
            SELECT
                id,
                filename,
                content,
                created_at
            FROM documents
            WHERE user_id = ?
              AND content LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                user_id,
                f"%{keyword}%",
                limit,
            )
        ).fetchall()

        connection.close()

    return rows


# ============================================================
# UTILITY
# ============================================================

def split_message(
    text,
    limit=MAX_TELEGRAM_MESSAGE
):

    if not text:
        return []

    text = str(text)

    chunks = []

    while len(text) > limit:

        cut = text.rfind(
            "\n",
            0,
            limit
        )

        if cut < 1000:
            cut = limit

        chunks.append(
            text[:cut]
        )

        text = text[cut:].lstrip()

    if text:
        chunks.append(text)

    return chunks


async def send_long_message(
    update,
    text
):

    if not text:
        return

    for chunk in split_message(text):

        await update.message.reply_text(
            chunk
        )


def clean_text(text):

    if not text:
        return ""

    text = text.replace(
        "\x00",
        ""
    )

    return text.strip()


# ============================================================
# MYANMAR UNICODE / ZAWGYI
# ============================================================

try:

    ZAWGYI_DETECTOR = (
        ZawgyiDetector()
    )

except Exception as error:

    logger.warning(
        "Zawgyi detector unavailable: %s",
        error
    )

    ZAWGYI_DETECTOR = None


try:

    ZAWGYI_TO_UNICODE = (
        Transliterator.createInstance(
            "Zawgyi-my"
        )
    )

except Exception as error:

    logger.warning(
        "ICU Zawgyi converter unavailable: %s",
        error
    )

    ZAWGYI_TO_UNICODE = None


def convert_myanmar_to_unicode(
    text
):

    if not text:
        return ""

    if (
        ZAWGYI_DETECTOR
        and ZAWGYI_TO_UNICODE
    ):

        try:

            score = (
                ZAWGYI_DETECTOR
                .get_zawgyi_probability(
                    text
                )
            )

            if score >= 0.75:

                logger.info(
                    "Zawgyi detected: %.3f",
                    score
                )

                return (
                    ZAWGYI_TO_UNICODE
                    .transliterate(text)
                )

        except Exception as error:

            logger.warning(
                "Myanmar conversion error: %s",
                error
            )

    return text


def count_myanmar(text):

    return sum(
        1
        for character in text
        if 0x1000 <= ord(character) <= 0x109F
    )


def text_quality_score(text):

    if not text:
        return 0.0

    text = clean_text(text)

    if not text:
        return 0.0

    total = len(text)

    myanmar = count_myanmar(text)

    suspicious_patterns = [
        "ေြ",
        "ြေ",
        "်ေ",
        "ေျ",
        "ြ်",
        "ဴ",
        "ဵ",
        "�",
    ]

    suspicious = sum(
        text.count(pattern)
        for pattern in suspicious_patterns
    )

    score = 0.0

    if myanmar > 0:
        score += min(
            myanmar / max(total * 0.15, 1),
            1.0
        ) * 0.6

    if suspicious:
        score -= min(
            suspicious / 100,
            0.5
        )

    if "\ufffd" in text:
        score -= 0.5

    return max(
        0.0,
        min(
            1.0,
            score
        )
    )


def is_text_bad(text):

    if not text:
        return True

    text = clean_text(text)

    if len(text) < 100:
        return True

    if "\ufffd" in text:
        return True

    myanmar = count_myanmar(text)

    suspicious = sum(
        text.count(pattern)
        for pattern in [
            "ေြ",
            "ြေ",
            "်ေ",
            "ေျ",
            "ြ်",
        ]
    )

    # Myanmar document
    if myanmar >= 20:

        if suspicious >= 5:
            return True

        if text_quality_score(text) < 0.25:
            return True

    return False


# ============================================================
# PDF NORMAL EXTRACTION
# ============================================================

def extract_pdf_text(
    pdf_path
):

    parts = []

    try:

        document = pymupdf.open(
            pdf_path
        )

        for page_number in range(
            len(document)
        ):

            page = document[
                page_number
            ]

            text = page.get_text(
                "text"
            )

            if text:

                parts.append(
                    text
                )

        document.close()

    except Exception as error:

        logger.exception(
            "PDF extraction error"
        )

        return ""

    result = "\n".join(
        parts
    )

    result = convert_myanmar_to_unicode(
        result
    )

    result = clean_text(
        result
    )

    logger.info(
        "Normal PDF text length: %s",
        len(result)
    )

    return result


# ============================================================
# OCR IMAGE
# ============================================================

def create_ocr_image(
    page,
    max_dimension,
    quality
):

    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(
            2.0,
            2.0
        ),
        alpha=False
    )

    image = Image.frombytes(
        "RGB",
        (
            pixmap.width,
            pixmap.height
        ),
        pixmap.samples
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
                max(
                    1,
                    int(width * scale)
                ),
                max(
                    1,
                    int(height * scale)
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


def ocr_request(
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
            "language": "mya",
            "OCREngine": "3",
            "isOverlayRequired": "false",
            "detectOrientation": "true",
            "scale": "true",
        },
        timeout=OCR_TIMEOUT
    )


def ocr_one_page(
    page,
    page_number
):

    if not OCR_API_KEY:

        return (
            "",
            "OCR_API_KEY မရှိပါ"
        )

    last_error = (
        "Unknown OCR error"
    )

    for setting_number, (
        max_dimension,
        quality
    ) in enumerate(
        OCR_SETTINGS,
        start=1
    ):

        logger.info(
            "OCR page %s: image setting %s/%s",
            page_number,
            setting_number,
            len(OCR_SETTINGS)
        )

        image_buffer = None

        try:

            image_buffer = create_ocr_image(
                page,
                max_dimension,
                quality
            )

            for retry in range(
                OCR_429_RETRIES
            ):

                response = ocr_request(
                    image_buffer
                )

                status = (
                    response.status_code
                )

                logger.info(
                    "OCR page %s HTTP %s",
                    page_number,
                    status
                )

                if status == 429:

                    wait_seconds = min(
                        60,
                        5 * (
                            2 ** retry
                        )
                    )

                    logger.warning(
                        "OCR 429. "
                        "Waiting %ss",
                        wait_seconds
                    )

                    time.sleep(
                        wait_seconds
                    )

                    image_buffer.seek(
                        0
                    )

                    continue

                if status == 413:

                    last_error = (
                        "OCR HTTP 413"
                    )

                    break

                if status != 200:

                    last_error = (
                        f"OCR HTTP {status}"
                    )

                    break

                try:

                    payload = (
                        response.json()
                    )

                except Exception:

                    last_error = (
                        "OCR returned invalid JSON"
                    )

                    break

                if payload.get(
                    "IsErroredOnProcessing"
                ):

                    error_message = (
                        payload.get(
                            "ErrorMessage",
                            "OCR processing error"
                        )
                    )

                    if isinstance(
                        error_message,
                        list
                    ):

                        error_message = (
                            " ".join(
                                str(item)
                                for item
                                in error_message
                            )
                        )

                    last_error = str(
                        error_message
                    )

                    break

                parsed = payload.get(
                    "ParsedResults",
                    []
                )

                text_parts = []

                for item in parsed:

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

                text = "\n".join(
                    text_parts
                )

                text = convert_myanmar_to_unicode(
                    text
                )

                text = clean_text(
                    text
                )

                if text:

                    return (
                        text,
                        None
                    )

                last_error = (
                    "OCR returned empty text"
                )

                break

        except requests.Timeout:

            last_error = (
                "OCR request timeout"
            )

        except requests.RequestException as error:

            last_error = (
                f"OCR network error: {error}"
            )

        except Exception as error:

            logger.exception(
                "OCR page error"
            )

            last_error = str(
                error
            )

        finally:

            if image_buffer:

                image_buffer.close()

        time.sleep(
            OCR_REQUEST_DELAY
        )

    return (
        "",
        last_error
    )


def process_pdf_ocr(
    pdf_path,
    status_callback=None
):

    if not OCR_API_KEY:

        return (
            "",
            []
        )

    document = pymupdf.open(
        pdf_path
    )

    total_pages = len(
        document
    )

    pages = {}

    failed = []

    try:

        for index in range(
            total_pages
        ):

            page_number = (
                index + 1
            )

            if status_callback:

                status_callback(
                    page_number,
                    total_pages
                )

            text, error = ocr_one_page(
                document[index],
                page_number
            )

            if text:

                pages[
                    page_number
                ] = text

            else:

                pages[
                    page_number
                ] = (
                    "[OCR FAILED: "
                    f"{error}]"
                )

                failed.append(
                    page_number
                )

    finally:

        document.close()

    output = []

    for page_number in sorted(
        pages
    ):

        output.append(
            f"\n--- Page {page_number} ---\n"
        )

        output.append(
            pages[page_number]
        )

        output.append("\n")

    return (
        "".join(output).strip(),
        failed
    )


# ============================================================
# PDF PROCESSING
# ============================================================

async def process_pdf_file(
    update,
    context,
    input_path,
    filename,
    status_message
):

    normal_text = await asyncio.to_thread(
        extract_pdf_text,
        input_path
    )

    # Normal text is accepted only if it looks valid.
    if (
        len(normal_text) >= 100
        and not is_text_bad(normal_text)
    ):

        document_id = save_document(
            update.effective_user.id,
            filename,
            normal_text
        )

        output_path = tempfile.mktemp(
            suffix=".txt"
        )

        try:

            with open(
                output_path,
                "w",
                encoding="utf-8"
            ) as file:

                file.write(
                    normal_text
                )

            await status_message.delete()

            await update.message.reply_document(
                document=output_path,
                caption=(
                    "✅ PDF → Text ပြီးပါပြီ။\n\n"
                    "📄 Normal PDF text extraction\n"
                    "အသုံးပြုထားပါတယ်။\n\n"
                    f"🗄️ Database ID: {document_id}"
                )
            )

        finally:

            if os.path.exists(
                output_path
            ):

                os.remove(
                    output_path
                )

        return

    # OCR fallback
    await status_message.edit_text(
        "🔎 PDF Text Encoding မမှန်ပါ "
        "သို့မဟုတ် Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"
        "🇲🇲 Myanmar OCR စတင်နေပါတယ်..."
    )

    document = pymupdf.open(
        input_path
    )

    total_pages = len(
        document
    )

    document.close()

    await status_message.edit_text(
        "🔎 PDF Text Encoding မမှန်ပါ "
        "သို့မဟုတ် Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"
        "🇲🇲 Myanmar OCR စတင်နေပါတယ်...\n\n"
        f"📄 Total pages: {total_pages}\n\n"
        "⏳ Page တစ်မျက်နှာချင်းစီ ဖတ်နေပါတယ်။\n\n"
        "⚠️ OCR API limit ရှိလို့ request ကို "
        "ထိန်းပြီး ပို့ပါမယ်။"
    )

    last_update = 0

    def progress(
        page_number,
        page_total
    ):

        nonlocal last_update

        # Callback runs in worker thread.
        # We only log here.
        if page_number != last_update:

            last_update = page_number

            logger.info(
                "OCR progress: %s/%s",
                page_number,
                page_total
            )

    result, failed_pages = await asyncio.to_thread(
        process_pdf_ocr,
        input_path,
        progress
    )

    if not result.strip():

        await status_message.edit_text(
            "❌ OCR နဲ့ပါ စာဖတ်မရပါ။\n\n"
            "OCR_API_KEY နဲ့ OCR service ကို စစ်ပါ။"
        )

        return

    result = convert_myanmar_to_unicode(
        result
    )

    document_id = save_document(
        update.effective_user.id,
        filename,
        result
    )

    output_path = tempfile.mktemp(
        suffix=".txt"
    )

    try:

        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(
                result
            )

        await status_message.delete()

        if failed_pages:

            failed_text = ", ".join(
                str(page)
                for page in failed_pages
            )

            caption = (
                "⚠️ PDF → Text ပြီးပါပြီ။\n\n"
                "🇲🇲 Myanmar OCR အသုံးပြုထားပါတယ်။\n\n"
                f"⚠️ မအောင်မြင်သော pages: "
                f"{failed_text}\n\n"
                f"🗄️ Database ID: {document_id}"
            )

        else:

            caption = (
                "✅ PDF → Text ပြီးပါပြီ။\n\n"
                "🇲🇲 Myanmar OCR အသုံးပြုထားပါတယ်။\n\n"
                f"🗄️ Database ID: {document_id}"
            )

        await update.message.reply_document(
            document=output_path,
            caption=caption
        )

    finally:

        if os.path.exists(
            output_path
        ):

            os.remove(
                output_path
            )


# ============================================================
# MAIN MENU
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📄 PDF Tools",
                callback_data="menu_pdf"
            ),
            InlineKeyboardButton(
                "🤖 AI Tools",
                callback_data="menu_ai"
            ),
        ],

        [
            InlineKeyboardButton(
                "🔎 Search",
                callback_data="menu_search"
            ),
            InlineKeyboardButton(
                "🌤️ Weather",
                callback_data="menu_weather"
            ),
        ],
    if data == "menu_weather":

        await query.edit_message_text(
            "🌤 Weather\n\n"
            "မြို့/နိုင်ငံကို ရိုက်ပို့ပါ။\n\n"
            "ဥပမာ:\n"
            "/weather Yangon\n"
            "/weather Mandalay\n"
            "/weather Tokyo\n"
            "/weather London",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ])
        )

        return


    if data == "menu_rate":

        await query.edit_message_text(
            "💱 Exchange Rate\n\n"
            "ငွေကြေး ၂ မျိုးထည့်ပါ။\n\n"
            "ဥပမာ:\n"
            "/rate USD MMK\n"
            "/rate EUR USD\n"
            "/rate GBP USD\n"
            "/rate USD JPY\n"
            "/rate USD CNY",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ])
        )

        return
    if data == "menu_search":

        await query.edit_message_text(
            "🔎 Database Search\n\n"
            "ရှာချင်တဲ့စာကို ပို့ပါ။\n\n"
            "ဥပမာ:\n"
            "/search sesame\n"
            "/search fertilizer",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ])
        )

        return
        [
            InlineKeyboardButton(
                "💱 Exchange Rate",
                callback_data="menu_rate"
            ),
            InlineKeyboardButton(
                "⏰ Reminder",
                callback_data="menu_remind"
            ),
        ],

        [
            InlineKeyboardButton(
                "📚 Study",
                callback_data="menu_study"
            ),
        ],

        [
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

    register_user(
        update.effective_user
    )

    context.user_data[
        "waiting_for_pdf"
    ] = False

    await update.message.reply_text(
        "🤖 AI PDF Helper မှ ကြိုဆိုပါတယ်!\n\n"
        "📄 PDF Tools\n"
        "🤖 AI Tools\n"
        "🔎 Database Search\n"
        "🌤 Weather\n"
        "💱 Exchange Rate\n"
        "⏰ Reminder\n"
        "📚 Study\n\n"
        "အောက်က Menu ကနေ ရွေးပါ 👇",
        reply_markup=main_keyboard()
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_user(
        update.effective_user
    )

    text = """
🤖 AI PDF Helper Commands

📄 PDF
• PDF Tools → PDF → Text

🤖 AI
• စာပို့ပြီး AI ကို မေးနိုင်ပါတယ်

🔎 Search
/search keyword

🌤 Weather
/weather Yangon
/weather Tokyo
/weather London
/weather New York

💱 Rate
/rate USD MMK
/rate USD EUR
/rate USD JPY
/rate EUR GBP

⏰ Reminder
/remind 10m ရေသောက်ရန်
/remind 2h စာဖတ်ရန်
/remind 30m meeting
/reminders

📚 Study
/Study
/Study English
/Study IT
/Study Programming
/Study Quiz
/Study Daily
"""

    await send_long_message(
        update,
        text
    )


# ============================================================
# PDF MENU
# ============================================================

async def pdf_menu(
    query
):

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📄 PDF → Text",
                    callback_data="pdf_to_text"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Main Menu",
                    callback_data="main_menu"
                )
            ],
        ]
    )

    await query.edit_message_text(
        "📄 PDF Tools\n\n"
        "PDF → Text ကိုရွေးပါ 👇",
        reply_markup=keyboard
    )


async def ai_menu(
    query
):

    await query.edit_message_text(
        "🤖 AI Tools\n\n"
        "စာကို တိုက်ရိုက်ပို့ပြီး AI ကိုမေးနိုင်ပါတယ်။\n\n"
        "ဥပမာ:\n"
        "Python ဆိုတာဘာလဲ?\n"
        "ဒီစာကို အကျဉ်းချုပ်ပေးပါ။",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ]
        )
    )


async def study_menu(
    query
):

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🇬🇧 English",
                    callback_data="study_English"
                ),
                InlineKeyboardButton(
                    "💻 IT",
                    callback_data="study_IT"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👨‍💻 Programming",
                    callback_data="study_Programming"
                ),
                InlineKeyboardButton(
                    "📝 Quiz",
                    callback_data="study_Quiz"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📅 Daily Lesson",
                    callback_data="study_Daily"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Main Menu",
                    callback_data="main_menu"
                )
            ],
        ]
    )

    await query.edit_message_text(
        "📚 STUDY CENTER\n\n"
        "သင်ယူမယ့် Subject ကိုရွေးပါ 👇",
        reply_markup=keyboard
    )


async def remind_menu(
    query
):

    await query.edit_message_text(
        "⏰ Reminder\n\n"
        "ဥပမာ:\n"
        "/remind 10m ရေသောက်ရန်\n"
        "/remind 2h စာဖတ်ရန်\n"
        "/remind 1d အလုပ်လုပ်ရန်\n\n"
        "/reminders — စာရင်းကြည့်",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ]
        )
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    data = query.data

    # ========================================================
    # MAIN MENU
    # ========================================================

    if data == "main_menu":
        await query.edit_message_text(
            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",
            reply_markup=main_keyboard()
        )
        return

    # ========================================================
    # PDF MENU
    # ========================================================

    if data == "menu_pdf":
        await pdf_menu(query)
        return

    # ========================================================
    # AI MENU
    # ========================================================

    if data == "menu_ai":
        await ai_menu(query)
        return

    # ========================================================
    # STUDY MENU
    # ========================================================

    if data == "menu_study":
        await study_menu(query)
        return

    # ========================================================
    # REMINDER MENU
    # ========================================================

    if data == "menu_remind":
        await remind_menu(query)
        return

    # ========================================================
    # HELP
    # ========================================================

    if data == "menu_help":
        await query.edit_message_text(
            "ℹ️ Help\n\n"
            "/start — Main Menu\n"
            "/search — Database ထဲရှာရန်\n"
            "/weather — ကမ္ဘာတစ်ဝှမ်း ရာသီဥတု\n"
            "/rate — ငွေလဲနှုန်း\n"
            "/remind — Reminder\n"
            "/study — English / IT / Programming / Quiz / Daily Lesson\n\n"
            "📄 PDF Tools\n"
            "→ PDF → Text\n\n"
            "🤖 AI Tools\n"
            "→ AI အသုံးပြုနိုင်ပါတယ်။",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ])
        )
        return

    # ========================================================
    # PDF → TEXT
    # ========================================================

    if data == "pdf_to_text":
        context.user_data["waiting_for_pdf"] = True

        await query.edit_message_text(
            "📄 PDF → Text\n\n"
            "PDF ဖိုင်တစ်ခု ပို့ပါ။\n\n"
            "📄 Normal PDF\n"
            "→ Text extraction လုပ်မယ်။\n\n"
            "🇲🇲 Myanmar PDF\n"
            "→ Myanmar encoding စစ်မယ်။\n\n"
            "🖼️ Scanned PDF\n"
            "→ OCR အသုံးပြုမယ်။"
        )
        return

    # ========================================================
    # STUDY SUBJECTS
    # ========================================================

    if data.startswith("study_"):
        subject = data.split("_", 1)[1]

        await send_study_lesson(
            query,
            context,
            subject
        )
        return

    # ========================================================
    # WEATHER BUTTON
    # ========================================================

    if data == "weather":
        await query.edit_message_text(
            "🌤 Weather\n\n"
            "မြို့/နိုင်ငံကို ရိုက်ပို့ပါ။\n\n"
            "ဥပမာ:\n"
            "/weather Yangon\n"
            "/weather Mandalay\n"
            "/weather Tokyo\n"
            "/weather London\n"
            "/weather New York"
        )
        return

    # ========================================================
    # RATE BUTTON
    # ========================================================

    if data == "rate":
        await query.edit_message_text(
            "💱 Currency Exchange Rate\n\n"
            "ငွေကြေး ၂ မျိုးကို ရိုက်ပို့ပါ။\n\n"
            "ဥပမာ:\n"
            "/rate USD MMK\n"
            "/rate EUR USD\n"
            "/rate GBP USD\n"
            "/rate USD JPY\n"
            "/rate USD CNY\n"
            "/rate SGD MMK\n"
            "/rate THB MMK"
        )
        return

    # ========================================================
    # UNKNOWN CALLBACK
    # ========================================================

    await query.answer(
        "ဒီခလုတ်ကို မသတ်မှတ်ရသေးပါ။",
        show_alert=True
    )

# ============================================================
# WEATHER
# ============================================================

GEOCODING_URL = (
    "https://geocoding-api.open-meteo.com/v1/search"
)

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
)


WEATHER_CODES = {
    0: "☀️ Clear sky",
    1: "🌤 Mainly clear",
    2: "⛅ Partly cloudy",
    3: "☁️ Overcast",
    45: "🌫 Fog",
    48: "🌫 Depositing rime fog",
    51: "🌦 Light drizzle",
    53: "🌦 Moderate drizzle",
    55: "🌧 Dense drizzle",
    61: "🌧 Slight rain",
    63: "🌧 Moderate rain",
    65: "🌧 Heavy rain",
    71: "🌨 Slight snow",
    73: "🌨 Moderate snow",
    75: "❄️ Heavy snow",
    80: "🌦 Rain showers",
    81: "🌧 Rain showers",
    82: "⛈ Violent rain showers",
    95: "⛈ Thunderstorm",
    96: "⛈ Thunderstorm + hail",
    99: "⛈ Thunderstorm + hail",
}


def geocode_location(
    location
):

    response = requests.get(
        GEOCODING_URL,
        params={
            "name": location,
            "count": 5,
            "language": "en",
            "format": "json",
        },
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    results = data.get(
        "results",
        []
    )

    if not results:
        return None

    return results[0]


def get_weather(
    latitude,
    longitude
):

    response = requests.get(
        WEATHER_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,"
                "relative_humidity_2m,"
                "apparent_temperature,"
                "is_day,"
                "precipitation,"
                "weather_code,"
                "wind_speed_10m"
            ),
            "timezone": "auto",
            "forecast_days": 1,
        },
        timeout=20
    )

    response.raise_for_status()

    return response.json()


async def weather_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_user(
        update.effective_user
    )

    if not context.args:

        await update.message.reply_text(
            "🌤 Weather\n\n"
            "အသုံးပြုပုံ:\n"
            "/weather Yangon\n"
            "/weather Tokyo\n"
            "/weather London\n"
            "/weather New York"
        )

        return

    location = " ".join(
        context.args
    )

    message = await update.message.reply_text(
        "🌍 Location ရှာနေပါတယ်..."
    )

    try:

        place = await asyncio.to_thread(
            geocode_location,
            location
        )

        if not place:

            await message.edit_text(
                f"❌ '{location}' ကို မတွေ့ပါ။"
            )

            return

        weather = await asyncio.to_thread(
            get_weather,
            place["latitude"],
            place["longitude"]
        )

        current = weather.get(
            "current",
            {}
        )

        code = current.get(
            "weather_code"
        )

        condition = WEATHER_CODES.get(
            code,
            "🌤 Unknown"
        )

        temperature = current.get(
            "temperature_2m"
        )

        feels = current.get(
            "apparent_temperature"
        )

        humidity = current.get(
            "relative_humidity_2m"
        )

        wind = current.get(
            "wind_speed_10m"
        )

        precipitation = current.get(
            "precipitation"
        )

        name = place.get(
            "name",
            location
        )

        country = place.get(
            "country",
            ""
        )

        timezone_name = weather.get(
            "timezone",
            ""
        )

        text = (
            f"🌤 Weather — {name}, {country}\n\n"
            f"{condition}\n\n"
            f"🌡 Temperature: {temperature}°C\n"
            f"🌡 Feels like: {feels}°C\n"
            f"💧 Humidity: {humidity}%\n"
            f"💨 Wind: {wind} km/h\n"
            f"🌧 Precipitation: {precipitation} mm\n\n"
            f"🕐 Timezone: {timezone_name}"
        )

        await message.edit_text(
            text
        )

    except Exception as error:

        logger.exception(
            "Weather error"
        )

        await message.edit_text(
            "❌ Weather ရယူမရပါ။\n\n"
            f"Error: {str(error)[:500]}"
        )


# ============================================================
# CURRENCY
# ============================================================

RATE_URL = (
    "https://open.er-api.com/v6/latest/"
)


def get_exchange_rate(
    base,
    target
):

    base = base.upper()
    target = target.upper()

    response = requests.get(
        RATE_URL + base,
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if data.get(
        "result"
    ) != "success":

        raise RuntimeError(
            "Exchange API error"
        )

    rates = data.get(
        "rates",
        {}
    )

    if target not in rates:

        raise ValueError(
            f"Currency {target} not supported"
        )

    return rates[target]


async def rate_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_user(
        update.effective_user
    )

    if len(context.args) < 2:

        await update.message.reply_text(
            "💱 အသုံးပြုပုံ:\n\n"
            "/rate USD MMK\n"
            "/rate USD EUR\n"
            "/rate USD JPY\n"
            "/rate EUR GBP\n\n"
            "Currency code 3 လုံးသုံးပါ။"
        )

        return

    base = context.args[0].upper()

    target = context.args[1].upper()

    message = await update.message.reply_text(
        "💱 Exchange rate ရှာနေပါတယ်..."
    )

    try:

        rate = await asyncio.to_thread(
            get_exchange_rate,
            base,
            target
        )

        await message.edit_text(
            "💱 Exchange Rate\n\n"
            f"1 {base} = "
            f"{rate:,.6f} {target}\n\n"
            "⚠️ Rate သည် live market rate "
            "အဖြစ် အတိအကျမဟုတ်နိုင်ပါ။"
        )

    except Exception as error:

        await message.edit_text(
            "❌ Exchange rate ရယူမရပါ။\n\n"
            f"{str(error)[:500]}"
        )


# ============================================================
# REMINDER
# ============================================================

REMINDER_PATTERN = re.compile(
    r"^(\d+)\s*([smhd])$",
    re.IGNORECASE
)


def parse_duration(
    value
):

    match = REMINDER_PATTERN.match(
        value.strip()
    )

    if not match:
        return None

    amount = int(
        match.group(1)
    )

    unit = match.group(2).lower()

    if amount <= 0:
        return None

    if unit == "s":
        return timedelta(
            seconds=amount
        )

    if unit == "m":
        return timedelta(
            minutes=amount
        )

    if unit == "h":
        return timedelta(
            hours=amount
        )

    if unit == "d":
        return timedelta(
            days=amount
        )

    return None


def save_reminder(
    user_id,
    text,
    remind_at
):

    with DB_LOCK:

        connection = db_connect()

        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO reminders
            (
                user_id,
                text,
                remind_at,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                text,
                remind_at,
                now_iso()
            )
        )

        reminder_id = (
            cursor.lastrowid
        )

        connection.commit()

        connection.close()

    return reminder_id


def get_reminders(
    user_id
):

    with DB_LOCK:

        connection = db_connect()

        rows = connection.execute(
            """
            SELECT
                id,
                text,
                remind_at,
                done
            FROM reminders
            WHERE user_id = ?
              AND done = 0
            ORDER BY remind_at ASC
            """,
            (
                user_id,
            )
        ).fetchall()

        connection.close()

    return rows


def delete_reminder(
    user_id,
    reminder_id
):

    with DB_LOCK:

        connection = db_connect()

        cursor = connection.cursor()

        cursor.execute(
            """
            DELETE FROM reminders
            WHERE id = ?
              AND user_id = ?
            """,
            (
                reminder_id,
                user_id
            )
        )

        deleted = (
            cursor.rowcount > 0
        )

        connection.commit()

        connection.close()

    return deleted


def mark_reminder_done(
    reminder_id
):

    with DB_LOCK:

        connection = db_connect()

        connection.execute(
            """
            UPDATE reminders
            SET done = 1
            WHERE id = ?
            """,
            (
                reminder_id,
            )
        )

        connection.commit()

        connection.close()


async def reminder_job(
    context: ContextTypes.DEFAULT_TYPE
):

    reminder_id = (
        context.job.data[
            "id"
        ]
    )

    user_id = (
        context.job.data[
            "user_id"
        ]
    )

    text = (
        context.job.data[
            "text"
        ]
    )

    try:

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⏰ Reminder!\n\n"
                f"📝 {text}"
            )
        )

    finally:

        await asyncio.to_thread(
            mark_reminder_done,
            reminder_id
        )


def schedule_reminder_job(
    application,
    reminder_id,
    user_id,
    text,
    remind_at
):

    current = datetime.now(
        timezone.utc
    )

    delay = (
        remind_at - current
    ).total_seconds()

    if delay < 1:
        delay = 1

    application.job_queue.run_once(
        reminder_job,
        when=delay,
        data={
            "id": reminder_id,
            "user_id": user_id,
            "text": text,
        },
        name=f"reminder_{reminder_id}"
    )


def restore_reminders(
    application
):

    rows = []

    with DB_LOCK:

        connection = db_connect()

        rows = connection.execute(
            """
            SELECT
                id,
                user_id,
                text,
                remind_at
            FROM reminders
            WHERE done = 0
            """
        ).fetchall()

        connection.close()

    for row in rows:

        try:

            remind_at = datetime.fromisoformat(
                row["remind_at"]
            )

            schedule_reminder_job(
                application,
                row["id"],
                row["user_id"],
                row["text"],
                remind_at
            )

        except Exception as error:

            logger.warning(
                "Could not restore reminder %s: %s",
                row["id"],
                error
            )


async def remind_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_user(
        update.effective_user
    )

    if len(context.args) < 2:

        await update.message.reply_text(
            "⏰ Reminder အသုံးပြုပုံ:\n\n"
            "/remind 10m ရေသောက်ရန်\n"
            "/remind 30m စာဖတ်ရန်\n"
            "/remind 2h meeting\n"
            "/remind 1d အလုပ်လုပ်ရန်\n\n"
            "/reminders — စာရင်းကြည့်\n"
            "/delremind ID — ဖျက်"
        )

        return

    duration = parse_duration(
        context.args[0]
    )

    if not duration:

        await update.message.reply_text(
            "❌ Time format မမှန်ပါ။\n\n"
            "10m / 30m / 2h / 1d လိုသုံးပါ။"
        )

        return

    reminder_text = " ".join(
        context.args[1:]
    ).strip()

    if not reminder_text:

        await update.message.reply_text(
            "❌ Reminder စာသားထည့်ပါ။"
        )

        return

    remind_at = (
        datetime.now(
            timezone.utc
        )
        + duration
    )

    reminder_id = await asyncio.to_thread(
        save_reminder,
        update.effective_user.id,
        reminder_text,
        remind_at.isoformat()
    )

    schedule_reminder_job(
        context.application,
        reminder_id,
        update.effective_user.id,
        reminder_text,
        remind_at
    )

    local_time = (
        remind_at
        .astimezone(
            ZoneInfo(TIMEZONE)
        )
        .strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    await update.message.reply_text(
        "✅ Reminder ထည့်ပြီးပါပြီ။\n\n"
        f"🆔 ID: {reminder_id}\n"
        f"📝 {reminder_text}\n"
        f"🕐 {local_time} ({TIMEZONE})"
    )


async def reminders_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    rows = await asyncio.to_thread(
        get_reminders,
        update.effective_user.id
    )

    if not rows:

        await update.message.reply_text(
            "⏰ လက်ရှိ Reminder မရှိပါ။"
        )

        return

    lines = [
        "⏰ Your Reminders\n"
    ]

    for row in rows:

        try:

            local_time = (
                datetime.fromisoformat(
                    row["remind_at"]
                )
                .astimezone(
                    ZoneInfo(TIMEZONE)
                )
                .strftime(
                    "%Y-%m-%d %H:%M"
                )
            )

        except Exception:

            local_time = row[
                "remind_at"
            ]

        lines.append(
            f"🆔 {row['id']} | "
            f"{local_time}\n"
            f"📝 {row['text']}"
        )

    lines.append(
        "\nဖျက်ရန်: /delremind ID"
    )

    await update.message.reply_text(
        "\n\n".join(lines)
    )


async def delremind_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "/delremind ID"
        )

        return

    try:

        reminder_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ ID မမှန်ပါ။"
        )

        return

    deleted = await asyncio.to_thread(
        delete_reminder,
        update.effective_user.id,
        reminder_id
    )

    if deleted:

        await update.message.reply_text(
            f"✅ Reminder {reminder_id} ဖျက်ပြီးပါပြီ။"
        )

    else:

        await update.message.reply_text(
            "❌ အဲဒီ Reminder ID မတွေ့ပါ။"
        )


# ============================================================
# SEARCH
# ============================================================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_user(
        update.effective_user
    )

    if not context.args:

        await update.message.reply_text(
            "🔎 Search အသုံးပြုပုံ:\n\n"
            "/search keyword\n\n"
            "ဥပမာ:\n"
            "/search နှမ်း\n"
            "/search fertilizer"
        )

        return

    keyword = " ".join(
        context.args
    )

    rows = await asyncio.to_thread(
        search_documents,
        update.effective_user.id,
        keyword
    )

    if not rows:

        await update.message.reply_text(
            f"🔎 '{keyword}' ကို Database ထဲမှာ မတွေ့ပါ။"
        )

        return

    parts = [
        f"🔎 Search results for: {keyword}\n"
    ]

    for row in rows:

        content = row[
            "content"
        ]

        position = content.lower().find(
            keyword.lower()
        )

        if position >= 0:

            start = max(
                0,
                position - 180
            )

            end = min(
                len(content),
                position + 500
            )

            snippet = content[
                start:end
            ]

        else:

            snippet = content[:500]

        parts.append(
            f"📄 {row['filename']}\n"
            f"{snippet}\n"
            f"🆔 DB ID: {row['id']}"
        )

    await send_long_message(
        update,
        "\n\n".join(parts)
    )


# ============================================================
# STUDY
# ============================================================

STUDY_CONTENT = {

    "English": [
        (
            "Lesson 1 — Basic English",
            "Vocabulary: hello, thank you, please, sorry.\n\n"
            "Practice:\n"
            "1. Say hello to someone.\n"
            "2. Make one sentence using 'please'."
        ),
        (
            "Lesson 2 — Present Simple",
            "Use the present simple for routines and facts.\n\n"
            "Example:\n"
            "I study English every day.\n"
            "She works in Yangon."
        ),
    ],

    "IT": [
        (
            "Lesson 1 — What is IT?",
            "IT means Information Technology.\n\n"
            "It includes computers, networks, software, "
            "databases, cybersecurity and cloud systems."
        ),
        (
            "Lesson 2 — Computer Basics",
            "Learn CPU, RAM, storage, operating system, "
            "input/output devices and networking basics."
        ),
    ],

    "Programming": [
        (
            "Lesson 1 — Programming Basics",
            "A program is a set of instructions executed "
            "by a computer.\n\n"
            "Learn variables, conditions, loops and functions."
        ),
        (
            "Lesson 2 — Python Variables",
            "Example:\n\n"
            "name = 'Si Thu'\n"
            "age = 20\n\n"
            "A variable stores a value that your program can use."
        ),
    ],

    "Daily": [
        (
            "Daily Lesson 1",
            "Today learn one English word, one IT concept "
            "and one programming concept.\n\n"
            "Homework: explain each concept in your own words."
        ),
    ],

}


async def get_study_progress(
    user_id
):

    with DB_LOCK:

        connection = db_connect()

        row = connection.execute(
            """
            SELECT *
            FROM study_progress
            WHERE user_id = ?
            """,
            (
                user_id,
            )
        ).fetchone()

        connection.close()

    return row


def set_study_progress(
    user_id,
    subject,
    lesson,
    score=0
):

    with DB_LOCK:

        connection = db_connect()

        connection.execute(
            """
            INSERT INTO study_progress
            (
                user_id,
                subject,
                lesson,
                score,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                subject=excluded.subject,
                lesson=excluded.lesson,
                score=excluded.score,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                subject,
                lesson,
                score,
                now_iso()
            )
        )

        connection.commit()

        connection.close()


async def send_study_lesson(
    query,
    context,
    subject
):

    if subject == "Quiz":

        await query.edit_message_text(
            "📝 Quiz\n\n"
            "Question 1:\n"
            "Python မှာ variable တစ်ခုကို ဘယ်လိုသတ်မှတ်မလဲ?\n\n"
            "A) var x = 10\n"
            "B) x = 10\n"
            "C) int x := 10\n\n"
            "ဖြေလိုရင် A / B / C ကို ပို့ပါ။"
        )

        context.user_data[
            "quiz_answer"
        ] = "B"

        return

    lessons = STUDY_CONTENT.get(
        subject,
        []
    )

    if not lessons:

        await query.edit_message_text(
            "❌ ဒီ Subject မှာ Lesson မရှိသေးပါ။"
        )

        return

    user_id = query.from_user.id

    progress = await get_study_progress(
        user_id
    )

    if progress and progress["subject"] == subject:

        lesson_number = (
            progress["lesson"]
        )

    else:

        lesson_number = 1

    index = (
        (lesson_number - 1)
        % len(lessons)
    )

    title, content = lessons[
        index
    ]

    set_study_progress(
        user_id,
        subject,
        lesson_number + 1,
        progress["score"] if progress else 0
    )

    await query.edit_message_text(
        f"📚 {subject}\n\n"
        f"📖 {title}\n\n"
        f"{content}\n\n"
        "➡️ နောက် Lesson အတွက် /Study "
        f"{subject}"
    )


async def study_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_user(
        update.effective_user
    )

    if not context.args:

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🇬🇧 English",
                        callback_data="study_English"
                    ),
                    InlineKeyboardButton(
                        "💻 IT",
                        callback_data="study_IT"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "👨‍💻 Programming",
                        callback_data="study_Programming"
                    ),
                    InlineKeyboardButton(
                        "📝 Quiz",
                        callback_data="study_Quiz"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📅 Daily Lesson",
                        callback_data="study_Daily"
                    )
                ],
            ]
        )

        await update.message.reply_text(
            "📚 STUDY CENTER\n\n"
            "သင်ယူမယ့် Subject ကိုရွေးပါ 👇",
            reply_markup=keyboard
        )

        return

    requested = " ".join(
        context.args
    ).lower()

    subject_map = {
        "english": "English",
        "it": "IT",
        "programming": "Programming",
        "program": "Programming",
        "quiz": "Quiz",
        "daily": "Daily",
    }

    subject = subject_map.get(
        requested
    )

    if not subject:

        await update.message.reply_text(
            "📚 Study Subjects:\n\n"
            "/Study English\n"
            "/Study IT\n"
            "/Study Programming\n"
            "/Study Quiz\n"
            "/Study Daily"
        )

        return

    class FakeQuery:

        from_user = update.effective_user

        async def edit_message_text(
            self,
            text,
            **kwargs
        ):

            await update.message.reply_text(
                text,
                **kwargs
            )

    await send_study_lesson(
        FakeQuery(),
        context,
        subject
    )


# ============================================================
# AI
# ============================================================

AI_SYSTEM_PROMPT = """
You are AI PDF Helper, a helpful assistant.

The user may speak Burmese or English.
Reply in the user's language.

For Burmese:
- Use proper Unicode Myanmar.
- Do not intentionally output Zawgyi.
- Explain clearly and practically.

You can help with:
- English
- IT
- Programming
- PDF/document questions
- General knowledge
- Study
- Summaries
- Explanations
"""


async def ask_ai(
    prompt
):

    if not openai_client:

        return (
            "❌ OPENAI_API_KEY မသတ်မှတ်ထားပါ။"
        )

    try:

        response = await openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=AI_SYSTEM_PROMPT,
            input=prompt
        )

        output = (
            response.output_text
            or ""
        ).strip()

        if not output:

            return (
                "❌ AI က response မပြန်ပါ။"
            )

        return output

    except Exception as error:

        logger.exception(
            "OpenAI API error"
        )

        return (
            "❌ AI API error ဖြစ်နေပါတယ်။\n\n"
            f"{str(error)[:1000]}"
        )


async def ai_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:

        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:

        return

    register_user(
        update.effective_user
    )

    # Quiz answer
    expected = context.user_data.get(
        "quiz_answer"
    )

    if expected:

        answer = text.strip().upper()

        if answer == expected:

            context.user_data[
                "quiz_answer"
            ] = None

            await update.message.reply_text(
                "✅ Correct!\n\n"
                "B) x = 10"
            )

        else:

            await update.message.reply_text(
                "❌ မမှန်သေးပါ။\n"
                "Hint: Python မှာ variable "
                "ကြေညာဖို့ `=` သုံးပါတယ်။"
            )

        return

    # If waiting for PDF,
    # normal text should not be treated as AI
    if context.user_data.get(
        "waiting_for_pdf"
    ):

        return

    await update.message.chat.send_action(
        ChatAction.TYPING
    )

    answer = await ask_ai(
        text
    )

    await send_long_message(
        update,
        answer
    )


# ============================================================
# PDF DOCUMENT HANDLER
# ============================================================

async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_user(
        update.effective_user
    )

    document = (
        update.message.document
    )

    if not document:

        return

    filename = (
        document.file_name
        or "document.pdf"
    )

    if not filename.lower().endswith(
        ".pdf"
    ):

        await update.message.reply_text(
            "❌ PDF ဖိုင်ပဲ ပို့ပေးပါ။"
        )

        return

    if document.file_size:

        size_mb = (
            document.file_size
            / 1024
            / 1024
        )

        if size_mb > MAX_PDF_SIZE_MB:

            await update.message.reply_text(
                f"❌ PDF size {MAX_PDF_SIZE_MB}MB "
                "ထက် မကျော်ရပါ။"
            )

            return

    user_id = (
        update.effective_user.id
    )

    with PROCESSING_LOCK:

        if user_id in PROCESSING_USERS:

            await update.message.reply_text(
                "⏳ သင့် PDF ကို လုပ်နေပြီးသားပါ။"
            )

            return

        PROCESSING_USERS.add(
            user_id
        )

    status_message = None

    input_path = None

    try:

        context.user_data[
            "waiting_for_pdf"
        ] = True

        status_message = (
            await update.message.reply_text(
                "⏳ PDF ကို download လုပ်နေပါတယ်..."
            )
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

        await status_message.edit_text(
            "📄 PDF ကို စစ်နေပါတယ်..."
        )

        await process_pdf_file(
            update,
            context,
            input_path,
            filename,
            status_message
        )

        context.user_data[
            "waiting_for_pdf"
        ] = False

    except Exception as error:

        logger.exception(
            "PDF handler error"
        )

        if status_message:

            try:

                await status_message.edit_text(
                    "❌ PDF processing မအောင်မြင်ပါ။\n\n"
                    f"{str(error)[:1000]}"
                )

            except Exception:
                pass

    finally:

        if input_path:

            try:

                if os.path.exists(
                    input_path
                ):

                    os.remove(
                        input_path
                    )

            except Exception:
                pass

        context.user_data[
            "waiting_for_pdf"
        ] = False

        with PROCESSING_LOCK:

            PROCESSING_USERS.discard(
                user_id
            )


# ============================================================
# APPLICATION STARTUP
# ============================================================

async def post_init(
    application
):

    logger.info(
        "AI PDF Helper starting..."
    )

    init_database()

    # Remove webhook so this deployment
    # uses polling only.
    try:

        await application.bot.delete_webhook(
            drop_pending_updates=True
        )

        logger.info(
            "Telegram webhook cleared."
        )

    except Exception as error:

        logger.warning(
            "Could not clear webhook: %s",
            error
        )

    # IMPORTANT:
    # Restore reminders only after
    # Application is running/initialized.
    restore_reminders(
        application
    )

    logger.info(
        "OCR API: %s",
        "ENABLED"
        if OCR_API_KEY
        else "DISABLED"
    )

    logger.info(
        "AI API: %s",
        "ENABLED"
        if OPENAI_API_KEY
        else "DISABLED"
    )


async def post_shutdown(
    application
):

    logger.info(
        "Application shutting down..."
    )


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(
        self
    ):

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"AI PDF Helper is running"
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

        logger.info(
            "Health server running on port %s",
            PORT
        )

        server.serve_forever()

    except Exception as error:

        logger.exception(
            "Health server error: %s",
            error
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    error = context.error

    logger.error(
        "Telegram error: %r",
        error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN မတွေ့ပါ။ "
            "Render Environment Variables ထဲ ထည့်ပါ။"
        )

    logger.info(
        "AI PDF Helper starting..."
    )

    # Render health server
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # --------------------------------------------------------
    # Build Application
    # --------------------------------------------------------

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)

        # Telegram long polling settings
        .get_updates_read_timeout(
            30
        )
        .get_updates_write_timeout(
            30
        )
        .get_updates_connect_timeout(
            30
        )
        .get_updates_pool_timeout(
            5
        )

        # General Telegram request settings
        .read_timeout(
            30
        )
        .write_timeout(
            30
        )
        .connect_timeout(
            30
        )
        .pool_timeout(
            5
        )

        .post_init(
            post_init
        )

        .post_shutdown(
            post_shutdown
        )

        .build()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "search",
            search_command
        )
    )

    application.add_handler(
        CommandHandler(
            "weather",
            weather_command
        )
    )

    application.add_handler(
        CommandHandler(
            "rate",
            rate_command
        )
    )

    application.add_handler(
        CommandHandler(
            "remind",
            remind_command
        )
    )

    application.add_handler(
        CommandHandler(
            "reminders",
            reminders_command
        )
    )

    application.add_handler(
        CommandHandler(
            "delremind",
            delremind_command
        )
    )

    application.add_handler(
        CommandHandler(
            "Study",
            study_command
        )
    )

    # Lowercase alias
    application.add_handler(
        CommandHandler(
            "study",
            study_command
        )
    )

    # --------------------------------------------------------
    # Buttons
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # --------------------------------------------------------
    # PDF
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Document.PDF,
            document_handler
        )
    )

    # --------------------------------------------------------
    # AI text
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            ai_message_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot is running..."
    )

    # IMPORTANT:
    # Only ONE polling process must run for this bot token.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=True,
        stop_signals=None
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

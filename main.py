import os
import io
import re
import time
import asyncio
import sqlite3
import threading
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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

# Optional AI
try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

DB_PATH = os.getenv(
    "DB_PATH",
    "bot_database.sqlite3"
)

OCR_URL = (
    "https://api.ocr.space/parse/image"
)

WEATHER_GEO_URL = (
    "https://geocoding-api.open-meteo.com/v1/search"
)

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
)

RATE_URL = (
    "https://api.frankfurter.dev/v2/rate"
)

OCR_TIMEOUT = 120

HTTP_TIMEOUT = 30

OCR_SETTINGS = [
    (2200, 70),
    (2000, 60),
    (1800, 55),
    (1600, 45),
    (1400, 40),
]

OCR_429_RETRIES = 3

OCR_REQUEST_DELAY = 2.0

MAX_PDF_SIZE_MB = 50

MAX_AI_TEXT = 12000

PROCESSING_USERS = set()

PROCESSING_LOCK = threading.Lock()

DB_LOCK = threading.Lock()


# ============================================================
# CURRENCY NAMES
# ============================================================

CURRENCY_NAMES = {

    "USD": "US Dollar",
    "EUR": "Euro",
    "GBP": "British Pound",
    "JPY": "Japanese Yen",
    "CNY": "Chinese Yuan",
    "SGD": "Singapore Dollar",
    "THB": "Thai Baht",
    "KRW": "South Korean Won",
    "AUD": "Australian Dollar",
    "CAD": "Canadian Dollar",
    "CHF": "Swiss Franc",
    "HKD": "Hong Kong Dollar",
    "NZD": "New Zealand Dollar",
    "INR": "Indian Rupee",
    "MYR": "Malaysian Ringgit",
    "IDR": "Indonesian Rupiah",
    "PHP": "Philippine Peso",
    "VND": "Vietnamese Dong",
    "AED": "UAE Dirham",
    "SAR": "Saudi Riyal",
    "QAR": "Qatari Riyal",
    "TRY": "Turkish Lira",
    "ZAR": "South African Rand",
    "SEK": "Swedish Krona",
    "NOK": "Norwegian Krone",
    "DKK": "Danish Krone",
    "PLN": "Polish Zloty",
    "CZK": "Czech Koruna",
    "HUF": "Hungarian Forint",
    "MMK": "Myanmar Kyat",
}


# ============================================================
# WEATHER CODES
# ============================================================

WEATHER_CODES = {

    0: "☀️ Clear sky",

    1: "🌤️ Mainly clear",
    2: "⛅ Partly cloudy",
    3: "☁️ Overcast",

    45: "🌫️ Fog",
    48: "🌫️ Depositing rime fog",

    51: "🌦️ Light drizzle",
    53: "🌦️ Moderate drizzle",
    55: "🌧️ Dense drizzle",

    56: "🌧️ Light freezing drizzle",
    57: "🌧️ Dense freezing drizzle",

    61: "🌧️ Slight rain",
    63: "🌧️ Moderate rain",
    65: "🌧️ Heavy rain",

    66: "🌧️ Light freezing rain",
    67: "🌧️ Heavy freezing rain",

    71: "🌨️ Slight snow",
    73: "🌨️ Moderate snow",
    75: "❄️ Heavy snow",

    77: "❄️ Snow grains",

    80: "🌦️ Slight rain showers",
    81: "🌧️ Moderate rain showers",
    82: "⛈️ Violent rain showers",

    85: "🌨️ Slight snow showers",
    86: "🌨️ Heavy snow showers",

    95: "⛈️ Thunderstorm",
    96: "⛈️ Thunderstorm with hail",
    99: "⛈️ Thunderstorm with heavy hail",
}


# ============================================================
# DATABASE
# ============================================================

def get_db():

    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with DB_LOCK:

        conn = get_db()

        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                page_count INTEGER DEFAULT 0,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                remind_at TEXT NOT NULL,
                message TEXT NOT NULL,
                sent INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_documents_user
            ON documents(user_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(remind_at, sent)
        """)

        conn.commit()

        conn.close()

    print("Database initialized.")


def save_document(
    user_id,
    filename,
    page_count,
    text
):

    with DB_LOCK:

        conn = get_db()

        conn.execute(
            """
            INSERT INTO documents
            (
                user_id,
                filename,
                page_count,
                text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                filename,
                page_count,
                text,
                datetime.utcnow().isoformat()
            )
        )

        conn.commit()

        conn.close()


def search_documents(
    user_id,
    query
):

    with DB_LOCK:

        conn = get_db()

        rows = conn.execute(
            """
            SELECT
                id,
                filename,
                page_count,
                text,
                created_at
            FROM documents
            WHERE user_id = ?
              AND text LIKE ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (
                user_id,
                "%" + query + "%"
            )
        ).fetchall()

        conn.close()

    return rows


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
            f"Health server running on "
            f"port {PORT}"
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
                "🌤 Weather",
                callback_data="weather"
            ),

            InlineKeyboardButton(
                "💱 Exchange Rate",
                callback_data="rate"
            ),
        ],

        [
            InlineKeyboardButton(
                "🔎 Search",
                callback_data="search"
            ),

            InlineKeyboardButton(
                "⏰ Reminder",
                callback_data="remind"
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

    context.user_data[
        "waiting_for_pdf"
    ] = False

    await update.message.reply_text(

        "🤖 AI PDF Helper\n\n"

        "📄 PDF → Text / OCR\n"
        "🤖 AI Assistant\n"
        "🌤 Worldwide Weather\n"
        "💱 Currency Rate\n"
        "🔎 PDF Database Search\n"
        "⏰ Reminder\n\n"

        "အောက်က Menu ကိုရွေးပါ 👇",

        reply_markup=main_keyboard()
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "📚 AI PDF Helper Commands\n\n"

        "📄 PDF\n"
        "/start → Main Menu\n\n"

        "🔎 Search\n"
        "/search sesame\n"
        "/search မြေဆီလွှာ\n\n"

        "🌤 Weather\n"
        "/weather Yangon\n"
        "/weather Tokyo\n"
        "/weather London\n"
        "/weather New York, USA\n\n"

        "💱 Rate\n"
        "/rate USD MMK\n"
        "/rate USD EUR\n"
        "/rate USD JPY 100\n"
        "/rate SGD MMK\n\n"

        "⏰ Reminder\n"
        "/remind 10m စာဖတ်ရန်\n"
        "/remind 2h အလုပ်လုပ်ရန်\n"
        "/remind 1d မနက်ဖြန်အလုပ်လုပ်ရန်\n"
        "/reminders\n"
        "/cancelremind 1\n\n"

        "🤖 AI\n"
        "/ask မြန်မာနိုင်ငံအကြောင်းရှင်းပြပါ\n\n"

        "📄 PDF ပို့ပြီး PDF → Text လုပ်နိုင်ပါတယ်။"
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
            "→ Text extraction\n\n"

            "🇲🇲 Myanmar encoding "
            "မမှန်ရင် OCR fallback\n\n"

            "🖼️ Scanned PDF\n"
            "→ OCR\n\n"

            "📚 Extracted text ကို "
            "Database ထဲသိမ်းပြီး "
            "/search နဲ့ ပြန်ရှာနိုင်ပါတယ်။"
        )

    elif query.data == "ai":

        await query.edit_message_text(

            "🤖 AI Tools\n\n"

            "/ask မေးခွန်း\n\n"

            "ဥပမာ:\n"
            "/ask နှမ်းစိုက်ပျိုးနည်းရှင်းပြပါ\n\n"

            "OPENAI_API_KEY ထည့်ထားရင် "
            "AI Assistant အလုပ်လုပ်ပါမယ်။"
        )

    elif query.data == "weather":

        await query.edit_message_text(

            "🌤 Worldwide Weather\n\n"

            "မြို့နာမည်နဲ့ ရှာနိုင်ပါတယ်။\n\n"

            "/weather Yangon\n"
            "/weather Tokyo\n"
            "/weather London\n"
            "/weather New York\n\n"

            "🌍 ကမ္ဘာတစ်ဝှမ်း location "
            "ရှာနိုင်ပါတယ်။"
        )

    elif query.data == "rate":

        await query.edit_message_text(

            "💱 Exchange Rate\n\n"

            "Currency code 3 လုံးသုံးပါ။\n\n"

            "/rate USD MMK\n"
            "/rate USD EUR\n"
            "/rate USD JPY 100\n"
            "/rate SGD MMK\n"
            "/rate CNY MMK\n\n"

            "အသုံးများတဲ့ currency တွေ "
            "အများကြီး support လုပ်ပါတယ်။"
        )

    elif query.data == "search":

        await query.edit_message_text(

            "🔎 Database Search\n\n"

            "/search <keyword>\n\n"

            "ဥပမာ:\n"
            "/search နှမ်း\n"
            "/search နိုက်ထရိုဂျင်\n"
            "/search မြေဆီလွှာ"
        )

    elif query.data == "remind":

        await query.edit_message_text(

            "⏰ Reminder\n\n"

            "/remind 10m စာဖတ်ရန်\n"
            "/remind 2h အလုပ်လုပ်ရန်\n"
            "/remind 1d မနက်ဖြန်သတိပေးရန်\n\n"

            "/reminders → List\n"
            "/cancelremind ID → Cancel"
        )

    elif query.data == "help":

        await query.edit_message_text(

            "ℹ️ Help\n\n"

            "/start\n"
            "/search\n"
            "/weather\n"
            "/rate\n"
            "/remind\n"
            "/reminders\n"
            "/cancelremind\n"
            "/ask\n\n"

            "PDF ပို့ရင် PDF → Text/OCR "
            "လုပ်ပေးပါတယ်။"
        )

    elif query.data == "back":

        await query.edit_message_text(

            "🤖 AI PDF Helper\n\n"
            "Main Menu 👇",

            reply_markup=main_keyboard()
        )


# ============================================================
# SEARCH
# ============================================================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not context.args:

        await update.message.reply_text(

            "🔎 Search အသုံးပြုနည်း\n\n"

            "/search keyword\n\n"

            "ဥပမာ:\n"
            "/search နှမ်း\n"
            "/search နိုက်ထရိုဂျင်"
        )

        return

    query = " ".join(
        context.args
    ).strip()

    rows = search_documents(
        user_id,
        query
    )

    if not rows:

        await update.message.reply_text(

            f"🔎 \"{query}\" ကို "
            "သင့် Database ထဲမှာ မတွေ့ပါ။"
        )

        return

    messages = [

        f"🔎 Search result: "
        f"\"{query}\"\n"
    ]

    for row in rows:

        text = row["text"]

        pos = text.lower().find(
            query.lower()
        )

        if pos < 0:

            snippet = text[:500]

        else:

            start = max(
                0,
                pos - 150
            )

            end = min(
                len(text),
                pos + 500
            )

            snippet = text[
                start:end
            ]

        messages.append(

            "\n📄 "
            f"{row['filename']}\n"
            f"📑 Pages: {row['page_count']}\n"
            f"📝 {snippet}\n"
        )

    result = "\n".join(
        messages
    )

    # Telegram message limit safety
    if len(result) > 3900:

        result = result[:3900]

        result += "\n\n... ဆက်လက်ရှိပါသည်။"

    await update.message.reply_text(
        result
    )


# ============================================================
# WEATHER
# ============================================================

def get_weather(
    location
):

    geo_response = requests.get(
        WEATHER_GEO_URL,
        params={
            "name": location,
            "count": 1,
            "language": "en",
            "format": "json",
        },
        timeout=HTTP_TIMEOUT
    )

    geo_response.raise_for_status()

    geo_data = geo_response.json()

    results = geo_data.get(
        "results",
        []
    )

    if not results:

        return None

    place = results[0]

    latitude = place["latitude"]
    longitude = place["longitude"]

    weather_response = requests.get(
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

            "daily": (
                "weather_code,"
                "temperature_2m_max,"
                "temperature_2m_min,"
                "precipitation_probability_max"
            ),

            "forecast_days": 3,

            "timezone": "auto",
        },
        timeout=HTTP_TIMEOUT
    )

    weather_response.raise_for_status()

    weather = weather_response.json()

    return (
        place,
        weather
    )


async def weather_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(

            "🌤 Weather အသုံးပြုနည်း\n\n"

            "/weather Yangon\n"
            "/weather Tokyo\n"
            "/weather London\n"
            "/weather New York\n"
            "/weather Bangkok, Thailand"
        )

        return

    location = " ".join(
        context.args
    )

    msg = await update.message.reply_text(
        "🌍 Location ရှာနေပါတယ်..."
    )

    try:

        result = await asyncio.to_thread(
            get_weather,
            location
        )

        if not result:

            await msg.edit_text(

                f"❌ \"{location}\" ကို "
                "မတွေ့ပါ။\n\n"

                "မြို့နာမည် + နိုင်ငံနာမည် "
                "ထည့်ကြည့်ပါ။"
            )

            return

        place, weather = result

        current = weather.get(
            "current",
            {}
        )

        daily = weather.get(
            "daily",
            {}
        )

        code = current.get(
            "weather_code"
        )

        description = WEATHER_CODES.get(
            code,
            "🌤️ Unknown"
        )

        name = place.get(
            "name",
            location
        )

        country = place.get(
            "country",
            ""
        )

        temp = current.get(
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

        lines = [

            f"🌤️ Weather — {name}",

            f"🌍 {country}",

            "",

            f"{description}",

            f"🌡 Temperature: {temp}°C",

            f"🥵 Feels like: {feels}°C",

            f"💧 Humidity: {humidity}%",

            f"💨 Wind: {wind} km/h",

            f"🌧 Precipitation: {precipitation} mm",

            "",

            "📅 3-Day Forecast:",
        ]

        dates = daily.get(
            "time",
            []
        )

        maxs = daily.get(
            "temperature_2m_max",
            []
        )

        mins = daily.get(
            "temperature_2m_min",
            []
        )

        probs = daily.get(
            "precipitation_probability_max",
            []
        )

        codes = daily.get(
            "weather_code",
            []
        )

        for i in range(
            min(
                3,
                len(dates)
            )
        ):

            dcode = codes[i]

            ddesc = WEATHER_CODES.get(
                dcode,
                "🌤️"
            )

            lines.append(

                f"{dates[i]} "
                f"{ddesc} "
                f"{mins[i]}–{maxs[i]}°C "
                f"🌧 {probs[i]}%"
            )

        await msg.edit_text(
            "\n".join(lines)
        )

    except Exception as e:

        print(
            "Weather error:",
            repr(e)
        )

        await msg.edit_text(

            "❌ Weather ရှာမရပါ။\n\n"
            "Location ကို "
            "မြို့ + နိုင်ငံပုံစံနဲ့ "
            "ပြန်စမ်းပါ။"
        )


# ============================================================
# CURRENCY
# ============================================================

def normalize_currency(
    code
):

    return code.strip().upper()


def format_decimal(
    value
):

    try:

        d = Decimal(str(value))

        if abs(d) >= 1000:

            return f"{d:,.2f}"

        if abs(d) >= 1:

            return f"{d:,.4f}"

        return f"{d:.8f}".rstrip(
            "0"
        ).rstrip(".")

    except Exception:

        return str(value)


def get_rate(
    base,
    quote
):

    response = requests.get(

        f"{RATE_URL}/"
        f"{base}/"
        f"{quote}",

        timeout=HTTP_TIMEOUT
    )

    if response.status_code != 200:

        try:

            data = response.json()

        except Exception:

            data = {}

        message = data.get(
            "message",
            "Rate unavailable"
        )

        raise ValueError(
            message
        )

    return response.json()


async def rate_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if len(context.args) < 2:

        await update.message.reply_text(

            "💱 Rate အသုံးပြုနည်း\n\n"

            "/rate USD MMK\n"
            "/rate USD EUR\n"
            "/rate USD JPY 100\n"
            "/rate SGD MMK\n\n"

            "Currency code 3 လုံးသုံးပါ။\n\n"

            "ဥပမာ:\n"
            "USD = US Dollar\n"
            "EUR = Euro\n"
            "GBP = British Pound\n"
            "JPY = Japanese Yen\n"
            "CNY = Chinese Yuan\n"
            "SGD = Singapore Dollar\n"
            "THB = Thai Baht\n"
            "KRW = Korean Won\n"
            "MMK = Myanmar Kyat"
        )

        return

    base = normalize_currency(
        context.args[0]
    )

    quote = normalize_currency(
        context.args[1]
    )

    amount = Decimal("1")

    if len(context.args) >= 3:

        try:

            amount = Decimal(
                context.args[2]
            )

            if amount <= 0:

                raise ValueError

        except Exception:

            await update.message.reply_text(
                "❌ Amount မမှန်ပါ။"
            )

            return

    if len(base) != 3 or len(quote) != 3:

        await update.message.reply_text(

            "❌ Currency code ကို "
            "3 လုံးသုံးပါ။\n\n"

            "ဥပမာ: USD MMK"
        )

        return

    msg = await update.message.reply_text(
        "💱 Exchange rate ရှာနေပါတယ်..."
    )

    try:

        data = await asyncio.to_thread(

            get_rate,
            base,
            quote
        )

        rate = Decimal(
            str(data["rate"])
        )

        converted = (
            amount * rate
        )

        base_name = CURRENCY_NAMES.get(
            base,
            base
        )

        quote_name = CURRENCY_NAMES.get(
            quote,
            quote
        )

        date = data.get(
            "date",
            "N/A"
        )

        await msg.edit_text(

            "💱 Exchange Rate\n\n"

            f"💵 {base} — {base_name}\n"
            f"💴 {quote} — {quote_name}\n\n"

            f"1 {base} = "
            f"{format_decimal(rate)} "
            f"{quote}\n\n"

            f"{format_decimal(amount)} "
            f"{base} = "
            f"{format_decimal(converted)} "
            f"{quote}\n\n"

            f"📅 Rate date: {date}\n"

            "ℹ️ Market/cash rate မဟုတ်နိုင်ပါ။ "
            "Reference rate အဖြစ်သုံးပါ။"
        )

    except Exception as e:

        print(
            "Rate error:",
            repr(e)
        )

        await msg.edit_text(

            "❌ ဒီ currency pair ကို "
            "rate source မှာ မတွေ့ပါ။\n\n"

            "ဥပမာ:\n"
            "/rate USD EUR\n"
            "/rate USD MMK\n"
            "/rate SGD MMK"
        )


# ============================================================
# REMINDER
# ============================================================

DURATION_RE = re.compile(
    r"^(\d+(?:\.\d+)?)([smhd])$",
    re.IGNORECASE
)


def parse_duration(
    value
):

    match = DURATION_RE.match(
        value.strip()
    )

    if not match:

        return None

    number = float(
        match.group(1)
    )

    unit = match.group(2).lower()

    if unit == "s":

        return timedelta(
            seconds=number
        )

    if unit == "m":

        return timedelta(
            minutes=number
        )

    if unit == "h":

        return timedelta(
            hours=number
        )

    if unit == "d":

        return timedelta(
            days=number
        )

    return None


def save_reminder(
    user_id,
    chat_id,
    remind_at,
    message
):

    with DB_LOCK:

        conn = get_db()

        cur = conn.execute(

            """
            INSERT INTO reminders
            (
                user_id,
                chat_id,
                remind_at,
                message,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,

            (
                user_id,
                chat_id,
                remind_at.isoformat(),
                message,
                datetime.utcnow().isoformat()
            )
        )

        reminder_id = cur.lastrowid

        conn.commit()

        conn.close()

    return reminder_id


async def remind_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if len(context.args) < 2:

        await update.message.reply_text(

            "⏰ Reminder အသုံးပြုနည်း\n\n"

            "/remind 10m စာဖတ်ရန်\n"
            "/remind 2h အလုပ်လုပ်ရန်\n"
            "/remind 1d မနက်ဖြန်လုပ်ရန်\n\n"

            "ယူနစ်:\n"
            "s = seconds\n"
            "m = minutes\n"
            "h = hours\n"
            "d = days"
        )

        return

    duration = parse_duration(
        context.args[0]
    )

    if duration is None:

        await update.message.reply_text(

            "❌ Time format မမှန်ပါ။\n\n"

            "ဥပမာ:\n"
            "10m\n"
            "2h\n"
            "1d"
        )

        return

    message = " ".join(
        context.args[1:]
    ).strip()

    if not message:

        await update.message.reply_text(
            "❌ Reminder စာသားထည့်ပါ။"
        )

        return

    remind_at = (
        datetime.now()
        + duration
    )

    reminder_id = save_reminder(

        update.effective_user.id,

        update.effective_chat.id,

        remind_at,

        message
    )

    await update.message.reply_text(

        "✅ Reminder တင်ပြီးပါပြီ။\n\n"

        f"🆔 ID: {reminder_id}\n"
        f"⏰ {remind_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📝 {message}"
    )


async def reminders_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    with DB_LOCK:

        conn = get_db()

        rows = conn.execute(

            """
            SELECT
                id,
                remind_at,
                message
            FROM reminders
            WHERE user_id = ?
              AND sent = 0
            ORDER BY remind_at
            LIMIT 20
            """,

            (user_id,)
        ).fetchall()

        conn.close()

    if not rows:

        await update.message.reply_text(
            "⏰ Active reminder မရှိပါ။"
        )

        return

    lines = [
        "⏰ Active Reminders\n"
    ]

    for row in rows:

        lines.append(

            f"🆔 {row['id']}\n"
            f"⏰ {row['remind_at']}\n"
            f"📝 {row['message']}\n"
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


async def cancel_remind_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(

            "/cancelremind ID\n\n"

            "ဥပမာ:\n"
            "/cancelremind 3"
        )

        return

    try:

        reminder_id = int(
            context.args[0]
        )

    except Exception:

        await update.message.reply_text(
            "❌ Reminder ID မမှန်ပါ။"
        )

        return

    with DB_LOCK:

        conn = get_db()

        cur = conn.execute(

            """
            UPDATE reminders
            SET sent = 1
            WHERE id = ?
              AND user_id = ?
              AND sent = 0
            """,

            (
                reminder_id,
                update.effective_user.id
            )
        )

        conn.commit()

        changed = cur.rowcount

        conn.close()

    if changed:

        await update.message.reply_text(
            "✅ Reminder cancel လုပ်ပြီးပါပြီ။"
        )

    else:

        await update.message.reply_text(
            "❌ Reminder မတွေ့ပါ။"
        )


async def reminder_worker(
    application
):

    while True:

        try:

            now = datetime.now()

            with DB_LOCK:

                conn = get_db()

                rows = conn.execute(

                    """
                    SELECT
                        id,
                        chat_id,
                        message
                    FROM reminders
                    WHERE sent = 0
                      AND remind_at <= ?
                    ORDER BY remind_at
                    LIMIT 50
                    """,

                    (now.isoformat(),)
                ).fetchall()

                for row in rows:

                    try:

                        await application.bot.send_message(

                            chat_id=row["chat_id"],

                            text=(
                                "⏰ Reminder\n\n"
                                f"📝 {row['message']}"
                            )
                        )

                        conn.execute(

                            """
                            UPDATE reminders
                            SET sent = 1
                            WHERE id = ?
                            """,

                            (row["id"],)
                        )

                    except Exception as e:

                        print(
                            "Reminder send error:",
                            repr(e)
                        )

                conn.commit()

                conn.close()

        except Exception as e:

            print(
                "Reminder worker error:",
                repr(e)
            )

        await asyncio.sleep(10)


# ============================================================
# MYANMAR TEXT QUALITY
# ============================================================

def count_myanmar_chars(
    text
):

    return sum(

        1
        for ch in text

        if 0x1000 <= ord(ch) <= 0x109F
    )


def suspicious_myanmar_score(
    text
):

    patterns = [

        "ေြ",
        "ြေ",
        "်ေ",
        "ေျ",
        "ြ်",
        "ဴ",
        "ဵ",
    ]

    return sum(
        text.count(p)
        for p in patterns
    )


def is_myanmar_text_bad(
    text
):

    if not text:

        return True

    stripped = text.strip()

    if len(stripped) < 100:

        return True

    myanmar = count_myanmar_chars(
        stripped
    )

    suspicious = suspicious_myanmar_score(
        stripped
    )

    print(
        "Myanmar chars:",
        myanmar,
        "Suspicious:",
        suspicious
    )

    if myanmar >= 20 and suspicious >= 8:

        return True

    if myanmar == 0 and len(stripped) > 200:

        return True

    return False


# ============================================================
# NORMAL PDF EXTRACTION
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
                f"{index}"
            )

            try:

                text = page.extract_text()

                if text:

                    parts.append(text)

            except Exception as e:

                print(
                    "Page extraction error:",
                    repr(e)
                )

        return "\n".join(
            parts
        ).strip()

    except Exception as e:

        print(
            "PDF extraction error:",
            repr(e)
        )

        return ""


# ============================================================
# OCR IMAGE
# ============================================================

def create_ocr_image(
    page,
    max_dimension,
    quality
):

    pix = page.get_pixmap(
        matrix=fitz.Matrix(
            2,
            2
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
            /
            largest
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

            # OCR.space language code
            # may not provide Burmese in
            # every engine/account.
            #
            # Engine 3 supports auto detection.
            "language": "auto",

            "OCREngine": "3",

            "isOverlayRequired": "false",

            "detectOrientation": "true",

            "scale": "true",
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

    for (
        max_dimension,
        quality
    ) in OCR_SETTINGS:

        print(

            f"OCR page "
            f"{page_number} "
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

            for retry in range(
                OCR_429_RETRIES + 1
            ):

                response = (
                    send_ocr_request(
                        image_buffer
                    )
                )

                print(

                    f"Page "
                    f"{page_number}: "
                    f"HTTP "
                    f"{response.status_code}"
                )

                if response.status_code == 429:

                    if retry >= OCR_429_RETRIES:

                        last_error = (
                            "OCR API HTTP 429"
                        )

                        break

                    wait = (
                        10
                        *
                        (retry + 1)
                    )

                    print(
                        f"429 → "
                        f"wait {wait}s"
                    )

                    time.sleep(
                        wait
                    )

                    continue

                if response.status_code == 413:

                    last_error = (
                        "OCR API HTTP 413"
                    )

                    break

                if response.status_code != 200:

                    last_error = (

                        "OCR API HTTP "
                        f"{response.status_code}"
                    )

                    break

                try:

                    result = response.json()

                except Exception:

                    last_error = (
                        "Invalid OCR JSON"
                    )

                    break

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

                    break

                parsed = result.get(
                    "ParsedResults",
                    []
                )

                parts = []

                for item in parsed:

                    text = item.get(
                        "ParsedText",
                        ""
                    )

                    if text:

                        parts.append(
                            text
                        )

                text = "\n".join(
                    parts
                ).strip()

                if text:

                    return (
                        text,
                        None
                    )

                last_error = (
                    "OCR returned empty text"
                )

                break

            time.sleep(
                OCR_REQUEST_DELAY
            )

        except requests.Timeout:

            last_error = (
                "OCR timeout"
            )

        except requests.RequestException as e:

            last_error = (
                f"OCR network error: {e}"
            )

        except Exception as e:

            last_error = str(e)

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

    failed = []

    try:

        total = len(pdf)

        for index in range(total):

            page_number = index + 1

            print(
                f"OCR page "
                f"{page_number}/"
                f"{total}"
            )

            text, error = ocr_page(

                pdf[index],

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

                failed.append(
                    page_number
                )

                print(
                    f"OCR failed "
                    f"page {page_number}: "
                    f"{error}"
                )

    finally:

        pdf.close()

    output = []

    for number in sorted(
        pages.keys()
    ):

        output.append(

            f"\n--- Page "
            f"{number} ---\n"
        )

        output.append(
            pages[number]
        )

    result = "\n".join(
        output
    ).strip()

    return (
        result,
        failed
    )


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

    user_id = update.effective_user.id

    with PROCESSING_LOCK:

        if user_id in PROCESSING_USERS:

            await update.message.reply_text(

                "⏳ သင့် PDF ကို "
                "လုပ်နေပြီးသားပါ။"
            )

            return

        PROCESSING_USERS.add(
            user_id
        )

    document = update.message.document

    if not document:

        with PROCESSING_LOCK:

            PROCESSING_USERS.discard(
                user_id
            )

        return

    filename = (
        document.file_name
        or "document.pdf"
    )

    if not filename.lower().endswith(
        ".pdf"
    ):

        with PROCESSING_LOCK:

            PROCESSING_USERS.discard(
                user_id
            )

        await update.message.reply_text(
            "❌ PDF ဖိုင်ပဲ ပို့ပါ။"
        )

        return

    file_size = (
        document.file_size
        or 0
    )

    if file_size > (
        MAX_PDF_SIZE_MB
        * 1024
        * 1024
    ):

        with PROCESSING_LOCK:

            PROCESSING_USERS.discard(
                user_id
            )

        await update.message.reply_text(

            f"❌ PDF size က "
            f"{MAX_PDF_SIZE_MB} MB "
            "ထက်ကြီးနေပါတယ်။"
        )

        return

    status = await update.message.reply_text(
        "⏳ PDF download လုပ်နေပါတယ်..."
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

        await status.edit_text(

            "📄 PDF text extraction "
            "စစ်နေပါတယ်..."
        )

        normal_text = await asyncio.to_thread(

            extract_pdf_text,

            input_path
        )

        bad = is_myanmar_text_bad(
            normal_text
        )

        # ----------------------------------------------------
        # NORMAL TEXT ACCEPT
        # ----------------------------------------------------

        if (
            len(normal_text.strip()) >= 100
            and not bad
        ):

            pdf = fitz.open(
                input_path
            )

            page_count = len(pdf)

            pdf.close()

            save_document(

                user_id,

                filename,

                page_count,

                normal_text
            )

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

            await status.delete()

            await update.message.reply_document(

                document=output_path,

                caption=(

                    "✅ PDF → Text ပြီးပါပြီ။\n\n"

                    "📄 Normal PDF extraction\n"

                    "📚 Database ထဲလည်း "
                    "သိမ်းပြီးပါပြီ။\n\n"

                    "🔎 /search keyword "
                    "နဲ့ ပြန်ရှာနိုင်ပါတယ်။"
                )
            )

            context.user_data[
                "waiting_for_pdf"
            ] = False

            return

        # ----------------------------------------------------
        # OCR
        # ----------------------------------------------------

        if not OCR_API_KEY:

            await status.edit_text(

                "❌ Normal PDF text က "
                "Myanmar encoding မမှန်ပါ။\n\n"

                "OCR_API_KEY မရှိလို့ "
                "OCR fallback မလုပ်နိုင်ပါ။"
            )

            return

        pdf = fitz.open(
            input_path
        )

        total_pages = len(pdf)

        pdf.close()

        await status.edit_text(

            "🔎 PDF Text Encoding "
            "မမှန်ပါ သို့မဟုတ် "
            "Scanned PDF ဖြစ်နိုင်ပါတယ်။\n\n"

            "🇲🇲 Myanmar OCR စတင်နေပါတယ်...\n\n"

            f"📄 Total pages: "
            f"{total_pages}\n\n"

            "⏳ Page တစ်မျက်နှာချင်းစီ "
            "ဖတ်နေပါတယ်။\n\n"

            "⚠️ OCR API limit ရှိလို့ "
            "request ကို ထိန်းပြီး ပို့ပါမယ်။"
        )

        result, failed = await asyncio.to_thread(

            process_pdf_ocr,

            input_path
        )

        if not result.strip():

            await status.edit_text(

                "❌ OCR နဲ့ စာဖတ်မရပါ။"
            )

            return

        save_document(

            user_id,

            filename,

            total_pages,

            result
        )

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

        if failed:

            failed_text = ", ".join(
                map(str, failed)
            )

            caption = (

                "⚠️ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 OCR အသုံးပြုထားပါတယ်။\n\n"

                f"⚠️ မအောင်မြင်တဲ့ page: "
                f"{failed_text}\n\n"

                "📚 Database ထဲ သိမ်းထားပါတယ်။"
            )

        else:

            caption = (

                "✅ PDF → Text ပြီးပါပြီ။\n\n"

                "🇲🇲 OCR အသုံးပြုထားပါတယ်။\n\n"

                "📚 Database ထဲ သိမ်းထားပါတယ်။\n"

                "🔎 /search keyword နဲ့ "
                "ပြန်ရှာနိုင်ပါတယ်။"
            )

        try:

            await status.delete()

        except Exception:

            pass

        await update.message.reply_document(

            document=output_path,

            caption=caption
        )

        context.user_data[
            "waiting_for_pdf"
        ] = False

    except Exception as e:

        print(
            "PDF error:",
            repr(e)
        )

        try:

            await status.edit_text(

                "❌ PDF processing "
                "မအောင်မြင်ပါ။\n\n"

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
# AI
# ============================================================

def ai_answer_sync(
    question
):

    if not OPENAI_API_KEY:

        raise RuntimeError(
            "OPENAI_API_KEY မထည့်ထားပါ။"
        )

    if OpenAI is None:

        raise RuntimeError(
            "openai package မရှိပါ။"
        )

    client = OpenAI(
        api_key=OPENAI_API_KEY
    )

    response = client.responses.create(

        model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5.6-luna"
        ),

        input=(

            "You are a helpful Telegram "
            "assistant. Answer clearly. "
            "If the user asks in Burmese, "
            "answer in Burmese.\n\n"

            f"User question:\n{question}"
        )
    )

    return response.output_text


async def ask_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(

            "🤖 AI အသုံးပြုနည်း\n\n"

            "/ask မေးခွန်း\n\n"

            "ဥပမာ:\n"
            "/ask နှမ်းစိုက်ပျိုးနည်းရှင်းပြပါ"
        )

        return

    question = " ".join(
        context.args
    ).strip()

    if len(question) > MAX_AI_TEXT:

        question = question[
            :MAX_AI_TEXT
        ]

    msg = await update.message.reply_text(
        "🤖 AI စဉ်းစားနေပါတယ်..."
    )

    try:

        answer = await asyncio.to_thread(

            ai_answer_sync,

            question
        )

        if len(answer) > 3900:

            answer = answer[:3900]

            answer += "\n\n..."

        await msg.edit_text(
            answer
        )

    except Exception as e:

        print(
            "AI error:",
            repr(e)
        )

        await msg.edit_text(

            "❌ AI မအလုပ်လုပ်ပါ။\n\n"

            "Render Environment Variables "
            "ထဲမှာ OPENAI_API_KEY ထည့်ထားမထား "
            "စစ်ပါ။"
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

    init_db()

    if OCR_API_KEY:

        print(
            "OCR API: ENABLED"
        )

    else:

        print(
            "OCR API: DISABLED"
        )

    if OPENAI_API_KEY:

        print(
            "AI API: ENABLED"
        )

    else:

        print(
            "AI API: DISABLED"
        )

    threading.Thread(

        target=start_health_server,

        daemon=True

    ).start()

    app = (

        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    app.add_handler(
        CommandHandler(
            "search",
            search_documents_command
        )
    )

    app.add_handler(
        CommandHandler(
            "weather",
            weather_command
        )
    )

    app.add_handler(
        CommandHandler(
            "rate",
            rate_command
        )
    )

    app.add_handler(
        CommandHandler(
            "remind",
            remind_command
        )
    )

    app.add_handler(
        CommandHandler(
            "reminders",
            reminders_command
        )
    )

    app.add_handler(
        CommandHandler(
            "cancelremind",
            cancel_remind_command
        )
    )

    app.add_handler(
        CommandHandler(
            "ask",
            ask_command
        )
    )

    # --------------------------------------------------------
    # Buttons
    # --------------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # --------------------------------------------------------
    # PDF
    # --------------------------------------------------------

    app.add_handler(

        MessageHandler(

            filters.Document.PDF,

            handle_pdf
        )
    )

    app.add_error_handler(
        error_handler
    )

    async def post_init(
        application
    ):

        application.create_task(
            reminder_worker(
                application
            )
        )

    # PTB post init
    app.post_init = post_init

    print(
        "Bot is running..."
    )

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# SEARCH COMMAND WRAPPER
# ============================================================

async def search_documents_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await search_command(
        update,
        context
    )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    main()

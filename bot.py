import os
import re
import logging
import asyncio
from datetime import datetime, timedelta

import psycopg2
from aiohttp import web

from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не найден")

ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "137602775"))

RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
PUBLIC_URL = os.getenv("PUBLIC_URL")

if PUBLIC_URL:
    BASE_URL = PUBLIC_URL.rstrip("/")
elif RAILWAY_PUBLIC_DOMAIN:
    BASE_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}"
else:
    BASE_URL = ""

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}" if BASE_URL else ""
PORT = int(os.getenv("PORT", "8080"))

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELETHON_SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")
ENABLE_HUMO_LISTENER = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)

DEFAULT_BANK_DEPOSIT = 88288796
DEFAULT_BANK_ACCOUNT = 0
DEFAULT_BANK_PERCENT = 0
DEFAULT_MY_BALANCE = 0

CREDITS = {
    "кредит ТБС банк": {
        "key": "credit_tbs_balance",
        "total": 14209240.81,
        "paid": 7557562.12,
        "balance": 6651642.69,
        "monthly": 1209240.81,
        "pay_day": 12,
    },
    "кредит Узум банк": {
        "key": "credit_uzum_balance",
        "total": 6804000,
        "paid": 3402000,
        "balance": 3402000,
        "monthly": 567000,
        "pay_day": 25,
    },
    "кредит Миллий банк": {
        "key": "credit_milliy_balance",
        "total": 72000000,
        "paid": 18690649,
        "balance": 53309351,
        "monthly": 2324838.79,
        "pay_day": 5,
    },
}

DEFAULT_EXPENSE_CATEGORIES = [
    "магазин",
    "рынок",
    "такси",
    "школа",
    "аптека",
    "кафе",
    "ресторан",
    "еда",
    "коммунальные",
    "Жалал",
    "билет",
    "Фуркат",
    "заправка пропан",
    "заправка бензин",
    "кредитная карта",
    "одежда",
    "кредит Узум банк",
    "кредит Миллий банк",
    "кредит ТБС банк",
    "прочее",
]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

user_state = {}
pending_expense = {}

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cursor = conn.cursor()


def now_uz():
    return datetime.utcnow() + timedelta(hours=5)


def init_db():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id SERIAL PRIMARY KEY,
        type TEXT,
        amount DOUBLE PRECISION,
        category TEXT,
        comment TEXT,
        date TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bank (
        name TEXT PRIMARY KEY,
        value DOUBLE PRECISION
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS humo_messages (
        id SERIAL PRIMARY KEY,
        telegram_message_id TEXT UNIQUE,
        raw_text TEXT,
        created_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE,
        created_at TEXT
    )
    """)

    for cat in DEFAULT_EXPENSE_CATEGORIES:
        add_category_to_db(cat)

    bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    bank_get("account", DEFAULT_BANK_ACCOUNT)
    bank_get("percent", DEFAULT_BANK_PERCENT)
    bank_get("my_balance", DEFAULT_MY_BALANCE)

    for credit_name, info in CREDITS.items():
        bank_get(info["key"], info["balance"])


def bank_get(name, default=0):
    cursor.execute("SELECT value FROM bank WHERE name=%s", (name,))
    row = cursor.fetchone()

    if row is None:
        cursor.execute(
            "INSERT INTO bank (name, value) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            (name, default)
        )
        return default

    return row[0]


def bank_set(name, value):
    cursor.execute(
        """
        INSERT INTO bank (name, value)
        VALUES (%s, %s)
        ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value
        """,
        (name, float(value))
    )


def add_category_to_db(name):
    name = str(name or "").strip()
    if not name:
        return False

    cursor.execute(
        """
        INSERT INTO categories (name, created_at)
        VALUES (%s, %s)
        ON CONFLICT (name) DO NOTHING
        """,
        (name, now_uz().strftime("%Y-%m-%d %H:%M:%S"))
    )
    return True


def get_categories():
    cursor.execute("SELECT name FROM categories ORDER BY id ASC")
    rows = cursor.fetchall()
    categories = [row[0] for row in rows]

    if not categories:
        for cat in DEFAULT_EXPENSE_CATEGORIES:
            add_category_to_db(cat)
        categories = DEFAULT_EXPENSE_CATEGORIES[:]

    return categories


def save_transaction(t_type, amount, category, comment, date_value=None):
    if date_value is None:
        date_value = now_uz().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        """
        INSERT INTO transactions (type, amount, category, comment, date)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (t_type, amount, category, comment, date_value)
    )

    row = cursor.fetchone()
    return row[0] if row else None


def update_transaction(record_id, category=None, comment=None):
    if category is not None:
        cursor.execute(
            "UPDATE transactions SET category=%s WHERE id=%s",
            (category, record_id)
        )

    if comment is not None:
        cursor.execute(
            "UPDATE transactions SET comment=%s WHERE id=%s",
            (comment, record_id)
        )


def already_processed_humo_message(message_id):
    cursor.execute(
        "SELECT id FROM humo_messages WHERE telegram_message_id=%s",
        (str(message_id),)
    )
    return cursor.fetchone() is not None


def mark_humo_message_processed(message_id, raw_text):
    cursor.execute(
        """
        INSERT INTO humo_messages (telegram_message_id, raw_text, created_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_message_id) DO NOTHING
        """,
        (str(message_id), raw_text, now_uz().strftime("%Y-%m-%d %H:%M:%S"))
    )


def main_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход"))
    kb.row(KeyboardButton("📊 Сегодня"), KeyboardButton("📅 Неделя"), KeyboardButton("🗓 Месяц"))
    kb.row(KeyboardButton("💰 Остаток"), KeyboardButton("🏦 Банк"), KeyboardButton("🗑 Удалить"))
    kb.row(KeyboardButton("➕ Добавить категорию"), KeyboardButton("📂 Категории"))
    return kb


kb = main_keyboard()


def make_keyboard(buttons, cols=2):
    keyboard = []
    row = []

    for btn in buttons:
        row.append(KeyboardButton(str(btn)))

        if len(row) == cols:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def category_reply_kb():
    return make_keyboard(get_categories() + ["➕ Добавить категорию", "🏠 Меню"], cols=2)


def category_inline_kb(record_id):
    kb_inline = InlineKeyboardMarkup(row_width=2)

    buttons = []
    for cat in get_categories():
        buttons.append(
            InlineKeyboardButton(
                text=cat,
                callback_data=f"cat:{record_id}:{cat}"
            )
        )

    kb_inline.add(*buttons)
    kb_inline.add(
        InlineKeyboardButton(
            text="➕ Новая категория",
            callback_data=f"addcat:{record_id}"
        )
    )
    kb_inline.add(
        InlineKeyboardButton(
            text="✏️ Изменить комментарий",
            callback_data=f"comment:{record_id}"
        )
    )
    return kb_inline


def fmt_sum(value):
    value = float(value or 0)

    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")

    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def normalize_text(text):
    text = (text or "").lower()
    text = text.replace("ё", "е")
    return text.strip()


def parse_money(raw):
    raw = str(raw or "").strip()
    raw = raw.replace(" ", "").replace("\u00a0", "")

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts[-1]) == 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "." in raw:
        parts = raw.split(".")
        if len(parts[-1]) == 2:
            raw = raw
        else:
            raw = raw.replace(".", "")

    return float(raw)


def extract_number(text):
    match = re.search(r"\d[\d\s.,]*", str(text or ""))

    if match:
        try:
            return parse_money(match.group(0))
        except Exception:
            return None

    return None


def detect_category(text):
    text = normalize_text(text)

    if "кафе" in text or "cafe" in text or "coffee" in text or "kofe" in text:
        return "кафе"
    if "ресторан" in text or "restaurant" in text:
        return "ресторан"
    if "еда" in text or "food" in text:
        return "еда"
    if "магаз" in text or "market" in text or "korzinka" in text:
        return "магазин"
    if "рынок" in text or "bozor" in text:
        return "рынок"
    if "такси" in text or "yandex" in text or "яндекс" in text:
        return "такси"
    if "школ" in text:
        return "школа"
    if "аптек" in text or "dorixona" in text or "pharm" in text or "лекар" in text:
        return "аптека"
    if any(x in text for x in ["свет", "мусор", "коммун", "газ", "вода", "uzgas", "hududiy"]):
        return "коммунальные"
    if "жалал" in text:
        return "Жалал"
    if "билет" in text:
        return "билет"
    if "фуркат" in text:
        return "Фуркат"
    if "пропан" in text or "gaz" in text:
        return "заправка пропан"
    if "бензин" in text or "benzin" in text:
        return "заправка бензин"
    if "кредитн" in text:
        return "кредитная карта"
    if "одежд" in text:
        return "одежда"
    if "узум" in text:
        return "кредит Узум банк"
    if "миллий" in text or "nbu" in text:
        return "кредит Миллий банк"
    if "тбс" in text or "tbs" in text:
        return "кредит ТБС банк"

    return "прочее"


def clean_comment(text):
    text = normalize_text(text)
    text = re.sub(r"\d[\d\s.,]*", "", text)
    text = text.replace("сум", "").replace("uzs", "").replace("so'm", "").replace("som", "")
    return " ".join(text.split()) or "без комментария"


def extract_sms_datetime(text):
    text = str(text or "")

    date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
    time_match = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?", text)

    if date_match:
        date_text = date_match.group(1)
        time_text = time_match.group(1) if time_match else now_uz().strftime("%H:%M")

        try:
            return datetime.strptime(
                f"{date_text} {time_text}",
                "%d.%m.%Y %H:%M"
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    return now_uz().strftime("%Y-%m-%d %H:%M:%S")


def extract_humo_comment(text):
    lines = [x.strip() for x in str(text or "").splitlines() if x.strip()]

    skip_words = [
        "humo",
        "uzs",
        "summa",
        "сумма",
        "karta",
        "карта",
        "qoldiq",
        "остаток",
        "sana",
        "дата",
        "xarid",
        "kirim",
        "chiqim",
        "to'lov",
        "tolov",
        "пополнение",
        "оплата",
    ]

    candidates = []

    for line in lines:
        low = normalize_text(line)

        if any(word in low for word in skip_words):
            continue

        if re.search(r"\d{2}\.\d{2}\.\d{4}", line):
            continue

        if len(line) >= 3:
            candidates.append(line)

    if candidates:
        return candidates[-1][:200]

    return "HUMO"


def parse_humo_message(text):
    raw = str(text or "")
    low = normalize_text(raw)

    if "uzs" not in low:
        return None

    is_income = any(x in low for x in [
        "kirim", "to'ldirish", "toldirish", "пополнение",
        "поступление", "vozvrat", "refund", "зачисление",
    ])

    is_expense = any(x in low for x in [
        "xarid", "to'lov", "tolov", "оплата",
        "списание", "purchase", "chiqim",
    ])

    if not is_income and not is_expense:
        is_expense = True

    amount_match = re.search(
        r"(?:summa|сумма)[^\d]*([\d\s.,]+)\s*uzs",
        low,
        re.I
    )

    if not amount_match:
        amount_match = re.search(r"([\d\s.,]+)\s*uzs", low, re.I)

    if not amount_match:
        return None

    amount = parse_money(amount_match.group(1))
    date_value = extract_sms_datetime(raw)
    comment = extract_humo_comment(raw)

    return {
        "type": "income" if is_income else "expense",
        "amount": amount,
        "date": date_value,
        "comment": comment,
        "raw": raw,
    }


async def send_message_to_user(text, reply_markup=None):
    await bot.send_message(ALLOWED_USER_ID, text, reply_markup=reply_markup)


async def send(message: types.Message, text: str, reply_markup=None):
    await bot.send_message(message.chat.id, text, reply_markup=reply_markup)


def is_allowed(message: types.Message):
    return bool(message.from_user and message.from_user.id == ALLOWED_USER_ID)


def apply_credit_payment(category, amount):
    if category not in CREDITS:
        return None

    info = CREDITS[category]
    old_balance = bank_get(info["key"], info["balance"])
    new_balance = max(0, old_balance - amount)

    bank_set(info["key"], new_balance)

    return {
        "name": category,
        "paid": amount,
        "old_balance": old_balance,
        "new_balance": new_balance,
        "monthly": info["monthly"],
        "pay_day": info["pay_day"],
    }


def rollback_credit_payment(category, amount):
    if category not in CREDITS:
        return

    info = CREDITS[category]
    balance = bank_get(info["key"], info["balance"])
    bank_set(info["key"], balance + amount)


async def process_humo_text(raw_text, message_id=None):
    parsed = parse_humo_message(raw_text)

    if not parsed:
        return

    if message_id is not None:
        if already_processed_humo_message(message_id):
            return
        mark_humo_message_processed(message_id, raw_text)

    amount = parsed["amount"]
    date_value = parsed["date"]
    comment = parsed["comment"]

    current_balance = bank_get("my_balance", DEFAULT_MY_BALANCE)

    if parsed["type"] == "income":
        new_balance = current_balance + amount
        bank_set("my_balance", new_balance)

        save_transaction("income", amount, "пополнение", comment, date_value)

        await send_message_to_user(
            f"➕ Приход HUMO\n\n"
            f"Сумма: {fmt_sum(amount)} сум\n"
            f"Откуда: {comment}\n"
            f"💳 Баланс: {fmt_sum(new_balance)} сум",
            reply_markup=kb
        )
        return

    new_balance = current_balance - amount
    bank_set("my_balance", new_balance)

    guessed_category = detect_category(comment)

    record_id = save_transaction(
        "expense",
        amount,
        "ожидает категорию",
        comment,
        date_value
    )

    text = (
        f"➖ Расход HUMO\n\n"
        f"Сумма: {fmt_sum(amount)} сум\n"
        f"Комментарий: {comment}\n"
        f"💳 Баланс: {fmt_sum(new_balance)} сум\n\n"
    )

    if guessed_category != "прочее":
        text += f"Похоже на: {guessed_category}\n"

    text += "Выбери категорию:"

    await send_message_to_user(text, reply_markup=category_inline_kb(record_id))


async def bank_report(message):
    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)
    percent = bank_get("percent", DEFAULT_BANK_PERCENT)

    text = (
        f"🏦 Банк\n\n"
        f"💼 Вклад: {fmt_sum(deposit)} сум\n"
        f"💳 На счёте банка: {fmt_sum(account)} сум\n"
        f"📈 Последний процент: {fmt_sum(percent)} сум\n\n"
        f"💰 Всего в банке: {fmt_sum(deposit + account)} сум\n\n"
        f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум\n\n"
        f"💳 Кредиты:\n"
    )

    for credit_name, info in CREDITS.items():
        balance = bank_get(info["key"], info["balance"])
        text += (
            f"• {credit_name}: осталось {fmt_sum(balance)} сум\n"
            f"  Платёж: {fmt_sum(info['monthly'])} сум, до {info['pay_day']}-го числа\n"
        )

    text += (
        f"\nМожно написать:\n"
        f"• банк процент 52 005,73\n"
        f"• банк счет 364 630\n"
        f"• банк на вклад 1 000 000\n"
        f"• мой баланс 2 884 740"
    )

    await send(message, text)


async def process_bank_command(message, text):
    text = normalize_text(text)
    amount = extract_number(text)

    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)

    if amount is None:
        await bank_report(message)
        return

    if "на вклад" in text or "на депозит" in text:
        if amount <= 0:
            await send(message, "❌ Сумма должна быть больше 0")
            return

        if account < amount:
            await send(
                message,
                f"❌ Недостаточно денег на счёте банка\n\n"
                f"💳 На счёте банка: {fmt_sum(account)} сум\n"
                f"Нужно перенести: {fmt_sum(amount)} сум"
            )
            return

        account -= amount
        deposit += amount

        bank_set("account", account)
        bank_set("deposit", deposit)

        await send(
            message,
            f"✅ Переведено на вклад\n\n"
            f"➡️ Сумма: {fmt_sum(amount)} сум\n"
            f"💼 Вклад: {fmt_sum(deposit)} сум\n"
            f"💳 На счёте банка: {fmt_sum(account)} сум\n"
            f"💰 Всего в банке: {fmt_sum(deposit + account)} сум"
        )
        return

    if "процент" in text:
        account += amount
        bank_set("percent", amount)
        bank_set("account", account)
        save_transaction("income", amount, "банк", "процент банка")

        await send(
            message,
            f"🏦 Процент начислен\n\n"
            f"📈 Процент: {fmt_sum(amount)} сум\n"
            f"💳 На счёте банка: {fmt_sum(account)} сум\n\n"
            f"ℹ️ В обычный отчет это не попадает."
        )
        return

    if "вклад" in text:
        bank_set("deposit", amount)
        await send(message, f"🏦 Вклад обновлён\n\n💼 Вклад: {fmt_sum(amount)} сум")
        return

    if "счет" in text or "счёт" in text:
        bank_set("account", amount)
        await send(message, f"🏦 Счёт банка обновлён\n\n💳 На счёте банка: {fmt_sum(amount)} сум")
        return

    await bank_report(message)


async def process_my_balance_command(message, text):
    amount = extract_number(text)

    if amount is None:
        await send(
            message,
            f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум"
        )
        return

    bank_set("my_balance", amount)
    await send(message, f"💳 Мой баланс обновлён: {fmt_sum(amount)} сум", reply_markup=kb)


async def report(message, mode):
    now = now_uz()

    if mode == "today":
        title = "📊 Сегодня"
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif mode == "week":
        title = "📅 Неделя"
        start = now - timedelta(days=7)
    elif mode == "month":
        title = "🗓 Месяц"
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        title = "💰 Всё время"
        start = datetime(2000, 1, 1)

    cursor.execute("""
    SELECT type, amount, category, comment, date
    FROM transactions
    ORDER BY date ASC
    """)
    rows = cursor.fetchall()

    income = 0
    expense = 0
    categories = {}
    lines = []

    for t, amount, category, comment, date_str in rows:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        if dt < start:
            continue

        if category == "банк":
            continue

        if t == "income":
            income += amount
        else:
            expense += amount
            categories[category] = categories.get(category, 0) + amount

        sign = "➕" if t == "income" else "➖"
        lines.append(
            f"{sign} {fmt_sum(amount)} — {category} — {comment} ({dt.strftime('%d.%m %H:%M')})"
        )

    balance = bank_get("my_balance", DEFAULT_MY_BALANCE)

    text = (
        f"{title}\n\n"
        f"➕ Приход: {fmt_sum(income)} сум\n"
        f"➖ Расход: {fmt_sum(expense)} сум\n"
        f"📌 Разница: {fmt_sum(income - expense)} сум\n"
        f"💳 Мой баланс: {fmt_sum(balance)} сум\n"
    )

    if categories:
        text += "\n🏷 Расходы по категориям:\n"
        for cat, total in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            text += f"• {cat}: {fmt_sum(total)} сум\n"

    if lines:
        text += "\n🧾 Последние записи:\n"
        for line in lines[-20:]:
            text += line + "\n"
    else:
        text += "\n🧾 Записей за этот период нет."

    await send(message, text, reply_markup=kb)


async def delete_last(message):
    cursor.execute("""
    SELECT id, type, amount, category, comment
    FROM transactions
    ORDER BY id DESC
    LIMIT 1
    """)
    row = cursor.fetchone()

    if not row:
        await send(message, "Удалять нечего")
        return

    record_id, t, amount, category, comment = row
    cursor.execute("DELETE FROM transactions WHERE id=%s", (record_id,))

    if category != "банк":
        balance = bank_get("my_balance", DEFAULT_MY_BALANCE)

        if t == "income":
            bank_set("my_balance", balance - amount)
        elif t == "expense":
            bank_set("my_balance", balance + amount)
            rollback_credit_payment(category, amount)

    type_ru = "приход" if t == "income" else "расход"

    await send(
        message,
        f"🗑 Удалено:\n"
        f"{type_ru} — {fmt_sum(amount)} сум\n"
        f"{category} — {comment}\n\n"
        f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум",
        reply_markup=kb
    )


async def show_categories(message):
    categories = get_categories()
    text = "📂 Категории:\n\n"
    for cat in categories:
        text += f"• {cat}\n"
    text += "\nЧтобы добавить новую:\nдобавить категорию кафе"
    await send(message, text, reply_markup=kb)


async def add_category_command(message, raw_text):
    text = raw_text.strip()
    low = normalize_text(text)
    name = ""

    if low.startswith("добавить категорию"):
        name = text[len("добавить категорию"):].strip()
    elif low.startswith("категория"):
        name = text[len("категория"):].strip()
    elif user_state.get(message.from_user.id) == "add_category":
        name = text.strip()

    if not name:
        user_state[message.from_user.id] = "add_category"
        await send(message, "Напиши название новой категории:")
        return

    add_category_to_db(name)
    user_state[message.from_user.id] = None

    await send(
        message,
        f"✅ Категория добавлена: {name}\n\n"
        f"Теперь она будет видна в кнопках.",
        reply_markup=kb
    )


async def process_text(message, raw_text):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return

    text = normalize_text(raw_text)
    chat_id = message.from_user.id

    if text == "🏠 меню":
        user_state[chat_id] = None
        pending_expense.pop(chat_id, None)
        await send(message, "Меню", reply_markup=kb)
        return

    if user_state.get(chat_id) == "add_category":
        await add_category_command(message, raw_text)
        return

    if user_state.get(chat_id, "").startswith("edit_comment:"):
        record_id = int(user_state[chat_id].split(":")[1])
        new_comment = raw_text.strip() or "без комментария"
        update_transaction(record_id, comment=new_comment)
        user_state[chat_id] = None
        await send(message, f"✅ Комментарий сохранён:\n{new_comment}", reply_markup=kb)
        return

    if user_state.get(chat_id, "").startswith("add_category_for_record:"):
        record_id = int(user_state[chat_id].split(":")[1])
        new_category = raw_text.strip()

        if not new_category:
            await send(message, "Напиши название категории.")
            return

        add_category_to_db(new_category)
        update_transaction(record_id, category=new_category)

        cursor.execute("SELECT amount FROM transactions WHERE id=%s", (record_id,))
        row = cursor.fetchone()
        if row:
            apply_credit_payment(new_category, row[0])

        user_state[chat_id] = None
        await send(message, f"✅ Новая категория добавлена и сохранена: {new_category}", reply_markup=kb)
        return

    if text.startswith("добавить категорию") or text.startswith("категория") or "добавить категорию" in text:
        await add_category_command(message, raw_text)
        return

    if text == "📂 категории":
        await show_categories(message)
        return

    if text == "➕ добавить категорию":
        user_state[chat_id] = "add_category"
        await send(message, "Напиши название новой категории:")
        return

    parsed_humo = parse_humo_message(raw_text)
    if parsed_humo:
        await process_humo_text(raw_text)
        return

    if "мой баланс" in text:
        await process_my_balance_command(message, raw_text)
        return

    if "сегодня" in text:
        await report(message, "today")
        return

    if "неделя" in text:
        await report(message, "week")
        return

    if "месяц" in text:
        await report(message, "month")
        return

    if "остаток" in text:
        await report(message, "all")
        return

    if "банк" in text:
        await process_bank_command(message, raw_text)
        return

    if "удал" in text:
        await delete_last(message)
        return

    if "приход" in text:
        user_state[chat_id] = "income"
        await send(message, "Введи приход\nНапример: 1 500 000 зарплата")
        return

    if "расход" in text:
        user_state[chat_id] = "expense"
        await send(message, "Введи расход\nНапример: магазин 20 000 продукты")
        return

    state = user_state.get(chat_id)

    if state in ["income", "expense"]:
        amount = extract_number(raw_text)

        if amount is None:
            await send(message, "❌ Не понял сумму")
            return

        category = detect_category(raw_text)
        comment = clean_comment(raw_text)

        if state == "income":
            balance = bank_get("my_balance", DEFAULT_MY_BALANCE) + amount
            bank_set("my_balance", balance)
        else:
            balance = bank_get("my_balance", DEFAULT_MY_BALANCE) - amount
            bank_set("my_balance", balance)

        save_transaction(state, amount, category, comment)
        credit_result = apply_credit_payment(category, amount) if state == "expense" else None

        user_state[chat_id] = None
        type_ru = "приход" if state == "income" else "расход"

        msg = (
            f"✅ Сохранено:\n"
            f"{type_ru} — {fmt_sum(amount)} сум\n"
            f"Категория: {category}\n"
            f"Комментарий: {comment}\n"
            f"💳 Мой баланс: {fmt_sum(balance)} сум"
        )

        if credit_result:
            msg += f"\n\n💳 {category}\n📉 Осталось погасить: {fmt_sum(credit_result['new_balance'])} сум"

        await send(message, msg, reply_markup=kb)
        return

    await send(
        message,
        "Используй кнопки:\n"
        "➕ Приход / ➖ Расход / 📊 Сегодня / 📅 Неделя / 🗓 Месяц\n\n"
        "Добавить категорию:\n"
        "добавить категорию кафе\n\n"
        "Или напиши:\n"
        "мой баланс 2 884 740",
        reply_markup=kb
    )


@dp.message_handler(lambda m: m.from_user and m.from_user.id != ALLOWED_USER_ID, content_types=types.ContentTypes.ANY)
async def deny_access(message: types.Message):
    await send(message, "⛔ Доступ запрещён")


@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return

    await send(
        message,
        "💰 Финансовый бот готов\n\n"
        "HUMO расходы будут приходить сюда автоматически.\n\n"
        "Можно добавить категорию:\n"
        "добавить категорию кафе",
        reply_markup=kb
    )


@dp.message_handler(content_types=types.ContentType.VOICE)
async def voice_handler(message: types.Message):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return
    await send(message, "🎙 Голос пока отключён. Пиши текстом.")


@dp.message_handler(content_types=types.ContentType.PHOTO)
async def photo_handler(message: types.Message):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return
    await send(message, "📷 Скрин пока не читаю. Скопируй текст или дождись HUMO уведомления.")


@dp.message_handler(content_types=types.ContentType.TEXT)
async def text_handler(message: types.Message):
    await process_text(message, message.text)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("cat:"))
async def category_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ALLOWED_USER_ID:
        await callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback_query.answer("Сохраняю...")

    parts = callback_query.data.split(":", 2)
    record_id = int(parts[1])
    category = parts[2]

    cursor.execute("SELECT amount FROM transactions WHERE id=%s", (record_id,))
    row = cursor.fetchone()

    if not row:
        await callback_query.answer("Запись не найдена", show_alert=True)
        return

    amount = row[0]
    update_transaction(record_id, category=category)
    credit_result = apply_credit_payment(category, amount)

    if credit_result:
        text = (
            f"✅ Категория сохранена: {category}\n\n"
            f"💳 Платёж по кредиту: {fmt_sum(amount)} сум\n"
            f"📉 Осталось погасить: {fmt_sum(credit_result['new_balance'])} сум"
        )
    else:
        text = f"✅ Категория сохранена: {category}\n💳 Баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум"

    await callback_query.message.edit_text(text)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("comment:"))
async def comment_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ALLOWED_USER_ID:
        await callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback_query.answer()
    record_id = int(callback_query.data.split(":")[1])
    user_state[callback_query.from_user.id] = f"edit_comment:{record_id}"
    await callback_query.message.answer("✏️ Напиши новый комментарий для этой записи:")


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("addcat:"))
async def add_category_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ALLOWED_USER_ID:
        await callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback_query.answer()
    record_id = int(callback_query.data.split(":")[1])
    user_state[callback_query.from_user.id] = f"add_category_for_record:{record_id}"
    await callback_query.message.answer("➕ Напиши название новой категории:")


telethon_client = None


async def start_humo_listener():
    global telethon_client

    if not ENABLE_HUMO_LISTENER:
        logging.warning("HUMO listener отключён: нет TELEGRAM_API_ID или TELEGRAM_API_HASH")
        return

    api_id = int(TELEGRAM_API_ID)
    api_hash = TELEGRAM_API_HASH

    if TELETHON_SESSION_STRING:
        session = StringSession(TELETHON_SESSION_STRING)
    else:
        session = "finance_session"

    telethon_client = TelegramClient(session, api_id, api_hash)

    @telethon_client.on(events.NewMessage(from_users="HUMOcardbot"))
    async def humo_handler(event):
        text = event.raw_text
        logging.info("Новое сообщение HUMO получено")
        await process_humo_text(text, message_id=event.message.id)

    await telethon_client.start()
    logging.info("HUMO listener запущен")


async def handle_index(request):
    return web.Response(text="Finance bot is running")


async def handle_health(request):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
    return web.Response(text="OK")


async def handle_webhook(request):
    data = await request.json()
    update = types.Update.to_object(data)
    await dp.process_update(update)
    return web.Response(text="ok")


async def on_startup(app):
    init_db()
    await start_humo_listener()

    if WEBHOOK_URL:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"Webhook set: {WEBHOOK_URL}")


async def on_shutdown(app):
    if telethon_client:
        await telethon_client.disconnect()

    await bot.delete_webhook()
    await bot.close()


def run_webhook():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)


async def run_polling():
    init_db()
    await start_humo_listener()
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling()


if __name__ == "__main__":
    if WEBHOOK_URL:
        run_webhook()
    else:
        asyncio.run(run_polling())

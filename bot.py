import os
import re
import logging
import asyncio
from datetime import datetime, timedelta

import psycopg2
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ================== НАСТРОЙКИ ==================

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
    "кредит ТБС банк": {"key": "credit_tbs_balance", "balance": 6651642.69, "monthly": 1209240.81, "pay_day": 12},
    "кредит Узум банк": {"key": "credit_uzum_balance", "balance": 3402000, "monthly": 567000, "pay_day": 25},
    "кредит Миллий банк": {"key": "credit_milliy_balance", "balance": 53309351, "monthly": 2324838.79, "pay_day": 5},
}

EXPENSE_CATEGORIES = [
    "магазин", "рынок", "такси", "школа", "аптека", "коммунальные", "Жалал", "билет", "Фуркат",
    "заправка пропан", "заправка бензин", "кредитная карта", "одежда",
    "кредит Узум банк", "кредит Миллий банк", "кредит ТБС банк", "прочее",
]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
user_state = {}
pending_expense = {}
telethon_client = None

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cursor = conn.cursor()

# ================== БАЗА ==================

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
    bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    bank_get("account", DEFAULT_BANK_ACCOUNT)
    bank_get("percent", DEFAULT_BANK_PERCENT)
    bank_get("my_balance", DEFAULT_MY_BALANCE)
    for _, info in CREDITS.items():
        bank_get(info["key"], info["balance"])


def bank_get(name, default=0):
    cursor.execute("SELECT value FROM bank WHERE name=%s", (name,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO bank (name, value) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            (name, default),
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
        (name, float(value)),
    )


def save_transaction(t_type, amount, category, comment, date_value=None):
    if date_value is None:
        date_value = now_uz().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        """
        INSERT INTO transactions (type, amount, category, comment, date)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (t_type, amount, category, comment, date_value),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def update_transaction(record_id, category=None, comment=None):
    if category is not None:
        cursor.execute("UPDATE transactions SET category=%s WHERE id=%s", (category, record_id))
    if comment is not None:
        cursor.execute("UPDATE transactions SET comment=%s WHERE id=%s", (comment, record_id))


def already_processed_humo_message(message_id):
    cursor.execute("SELECT id FROM humo_messages WHERE telegram_message_id=%s", (str(message_id),))
    return cursor.fetchone() is not None


def mark_humo_message_processed(message_id, raw_text):
    cursor.execute(
        """
        INSERT INTO humo_messages (telegram_message_id, raw_text, created_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_message_id) DO NOTHING
        """,
        (str(message_id), raw_text, now_uz().strftime("%Y-%m-%d %H:%M:%S")),
    )

# ================== КНОПКИ ==================

kb = ReplyKeyboardMarkup(resize_keyboard=True)
kb.row(KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход"))
kb.row(KeyboardButton("📊 Сегодня"), KeyboardButton("📅 Неделя"), KeyboardButton("🗓 Месяц"))
kb.row(KeyboardButton("💰 Остаток"), KeyboardButton("🏦 Банк"), KeyboardButton("🗑 Удалить"))


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

category_kb = make_keyboard(EXPENSE_CATEGORIES + ["🏠 Меню"], cols=2)


def category_inline_kb(record_id):
    kb_inline = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(text=cat, callback_data=f"cat:{record_id}:{cat}") for cat in EXPENSE_CATEGORIES]
    kb_inline.add(*buttons)
    kb_inline.add(InlineKeyboardButton(text="✏️ Изменить комментарий", callback_data=f"comment:{record_id}"))
    return kb_inline

# ================== ПОМОЩНИКИ ==================

def now_uz():
    return datetime.utcnow() + timedelta(hours=5)


def fmt_sum(value):
    value = float(value or 0)
    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def normalize_text(text):
    return (text or "").lower().replace("ё", "е").strip()


def parse_money(raw):
    raw = str(raw or "").strip().replace(" ", "").replace("\u00a0", "")
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        raw = raw.replace(",", ".") if len(parts[-1]) == 2 else raw.replace(",", "")
    elif "." in raw:
        parts = raw.split(".")
        if len(parts[-1]) != 2:
            raw = raw.replace(".", "")
    return float(raw)


def extract_number(text):
    match = re.search(r"\d[\d\s.,]*", str(text or ""))
    if not match:
        return None
    try:
        return parse_money(match.group(0))
    except Exception:
        return None


def detect_category(text):
    text = normalize_text(text)
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
    if any(x in text for x in ["свет", "мусор", "коммун", "газ", "вода"]):
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
            return datetime.strptime(f"{date_text} {time_text}", "%d.%m.%Y %H:%M").strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return now_uz().strftime("%Y-%m-%d %H:%M:%S")


def extract_humo_comment(text):
    lines = [x.strip() for x in str(text or "").splitlines() if x.strip()]
    skip_words = ["humo", "uzs", "summa", "сумма", "karta", "карта", "qoldiq", "остаток", "sana", "дата", "xarid", "kirim", "chiqim", "to'lov", "tolov", "пополнение", "оплата"]
    candidates = []
    for line in lines:
        low = normalize_text(line)
        if any(word in low for word in skip_words):
            continue
        if re.search(r"\d{2}\.\d{2}\.\d{4}", line):
            continue
        if len(line) >= 3:
            candidates.append(line)
    return candidates[-1][:200] if candidates else "HUMO"


def parse_humo_message(text):
    raw = str(text or "")
    low = normalize_text(raw)
    if "uzs" not in low:
        return None
    is_income = any(x in low for x in ["kirim", "to'ldirish", "toldirish", "пополнение", "поступление", "vozvrat", "refund", "зачисление"])
    is_expense = any(x in low for x in ["xarid", "to'lov", "tolov", "оплата", "списание", "purchase", "chiqim"])
    if not is_income and not is_expense:
        is_expense = True
    amount_match = re.search(r"(?:summa|сумма)[^\d]*([\d\s.,]+)\s*uzs", low, re.I)
    if not amount_match:
        amount_match = re.search(r"([\d\s.,]+)\s*uzs", low, re.I)
    if not amount_match:
        return None
    amount = parse_money(amount_match.group(1))
    return {"type": "income" if is_income else "expense", "amount": amount, "date": extract_sms_datetime(raw), "comment": extract_humo_comment(raw), "raw": raw}

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
    return {"name": category, "paid": amount, "old_balance": old_balance, "new_balance": new_balance, "monthly": info["monthly"], "pay_day": info["pay_day"]}


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
            f"➕ Приход HUMO\n\nСумма: {fmt_sum(amount)} сум\nОткуда: {comment}\n💳 Баланс: {fmt_sum(new_balance)} сум",
            reply_markup=kb,
        )
        return

    new_balance = current_balance - amount
    bank_set("my_balance", new_balance)
    guessed_category = detect_category(comment)
    record_id = save_transaction("expense", amount, "ожидает категорию", comment, date_value)
    text = f"➖ Расход HUMO\n\nСумма: {fmt_sum(amount)} сум\nКомментарий: {comment}\n💳 Баланс: {fmt_sum(new_balance)} сум\n\n"
    if guessed_category != "прочее":
        text += f"Похоже на: {guessed_category}\n"
    text += "Выбери категорию:"
    await send_message_to_user(text, reply_markup=category_inline_kb(record_id))

# ================== ОТЧЁТЫ ==================

async def bank_report(message):
    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)
    percent = bank_get("percent", DEFAULT_BANK_PERCENT)
    text = (
        f"🏦 Банк\n\n💼 Вклад: {fmt_sum(deposit)} сум\n💳 На счёте банка: {fmt_sum(account)} сум\n📈 Последний процент: {fmt_sum(percent)} сум\n\n"
        f"💰 Всего в банке: {fmt_sum(deposit + account)} сум\n\n💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум\n\n💳 Кредиты:\n"
    )
    for credit_name, info in CREDITS.items():
        balance = bank_get(info["key"], info["balance"])
        text += f"• {credit_name}: осталось {fmt_sum(balance)} сум\n  Платёж: {fmt_sum(info['monthly'])} сум, до {info['pay_day']}-го числа\n"
    text += "\nМожно написать:\n• банк процент 52 005,73\n• банк счет 364 630\n• банк на вклад 1 000 000\n• мой баланс 2 884 740"
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
        if account < amount:
            await send(message, f"❌ Недостаточно денег на счёте банка\n\n💳 На счёте банка: {fmt_sum(account)} сум")
            return
        bank_set("account", account - amount)
        bank_set("deposit", deposit + amount)
        await send(message, f"✅ Переведено на вклад\n\n➡️ Сумма: {fmt_sum(amount)} сум")
        return
    if "процент" in text:
        bank_set("percent", amount)
        bank_set("account", account + amount)
        save_transaction("income", amount, "банк", "процент банка")
        await send(message, f"🏦 Процент начислен\n\n📈 Процент: {fmt_sum(amount)} сум")
        return
    if "вклад" in text:
        bank_set("deposit", amount)
        await send(message, f"🏦 Вклад обновлён: {fmt_sum(amount)} сум")
        return
    if "счет" in text or "счёт" in text:
        bank_set("account", amount)
        await send(message, f"🏦 Счёт банка обновлён: {fmt_sum(amount)} сум")
        return
    await bank_report(message)

async def process_my_balance_command(message, text):
    amount = extract_number(text)
    if amount is None:
        await send(message, f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум")
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
    cursor.execute("SELECT type, amount, category, comment, date FROM transactions ORDER BY date ASC")
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
        if dt < start or category == "банк":
            continue
        if t == "income":
            income += amount
        else:
            expense += amount
            categories[category] = categories.get(category, 0) + amount
        sign = "➕" if t == "income" else "➖"
        lines.append(f"{sign} {fmt_sum(amount)} — {category} — {comment} ({dt.strftime('%d.%m %H:%M')})")
    text = f"{title}\n\n➕ Приход: {fmt_sum(income)} сум\n➖ Расход: {fmt_sum(expense)} сум\n📌 Разница: {fmt_sum(income - expense)} сум\n💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум\n"
    if categories:
        text += "\n🏷 Расходы по категориям:\n"
        for cat, total in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            text += f"• {cat}: {fmt_sum(total)} сум\n"
    if lines:
        text += "\n🧾 Последние записи:\n" + "\n".join(lines[-20:])
    else:
        text += "\n🧾 Записей за этот период нет."
    await send(message, text, reply_markup=kb)

async def delete_last(message):
    cursor.execute("SELECT id, type, amount, category, comment FROM transactions ORDER BY id DESC LIMIT 1")
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
    await send(message, f"🗑 Удалено:\n{type_ru} — {fmt_sum(amount)} сум\n{category} — {comment}\n\n💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум", reply_markup=kb)

# ================== ОБРАБОТКА ==================

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
    if user_state.get(chat_id, "").startswith("edit_comment:"):
        record_id = int(user_state[chat_id].split(":")[1])
        update_transaction(record_id, comment=raw_text.strip() or "без комментария")
        user_state[chat_id] = None
        await send(message, "✅ Комментарий сохранён", reply_markup=kb)
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
        msg = f"✅ Сохранено:\n{type_ru} — {fmt_sum(amount)} сум\nКатегория: {category}\nКомментарий: {comment}\n💳 Мой баланс: {fmt_sum(balance)} сум"
        if credit_result:
            msg += f"\n\n💳 {category}\n📉 Осталось погасить: {fmt_sum(credit_result['new_balance'])} сум"
        await send(message, msg, reply_markup=kb)
        return
    await send(message, "Используй кнопки или напиши:\nмой баланс 2 884 740\nрасход 20 000 такси\nприход 1 500 000 зарплата", reply_markup=kb)

@dp.message_handler(lambda m: m.from_user and m.from_user.id != ALLOWED_USER_ID, content_types=types.ContentTypes.ANY)
async def deny_access(message: types.Message):
    await send(message, "⛔ Доступ запрещён")

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return
    await send(message, "💰 Финансовый бот готов\n\nТеперь HUMO расходы будут приходить сюда автоматически.", reply_markup=kb)

@dp.message_handler(content_types=types.ContentType.VOICE)
async def voice_handler(message: types.Message):
    await send(message, "🎙 Голос пока отключён. Пиши текстом.")

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def photo_handler(message: types.Message):
    await send(message, "📷 Скрин пока не читаю. Скопируй текст или дождись HUMO уведомления.")

@dp.message_handler(content_types=types.ContentType.TEXT)
async def text_handler(message: types.Message):
    await process_text(message, message.text)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("cat:"))
async def category_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ALLOWED_USER_ID:
        await callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return
    _, record_id_text, category = callback_query.data.split(":", 2)
    record_id = int(record_id_text)
    cursor.execute("SELECT amount FROM transactions WHERE id=%s", (record_id,))
    row = cursor.fetchone()
    if not row:
        await callback_query.answer("Запись не найдена", show_alert=True)
        return
    amount = row[0]
    update_transaction(record_id, category=category)
    credit_result = apply_credit_payment(category, amount)
    if credit_result:
        text = f"✅ Категория сохранена: {category}\n\n💳 Платёж по кредиту: {fmt_sum(amount)} сум\n📉 Осталось погасить: {fmt_sum(credit_result['new_balance'])} сум"
    else:
        text = f"✅ Категория сохранена: {category}\n💳 Баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум"
    await callback_query.message.edit_text(text)
    await callback_query.answer("Сохранено")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("comment:"))
async def comment_callback(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ALLOWED_USER_ID:
        await callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return
    record_id = int(callback_query.data.split(":")[1])
    user_state[callback_query.from_user.id] = f"edit_comment:{record_id}"
    await callback_query.message.answer("✏️ Напиши новый комментарий для этой записи:")
    await callback_query.answer()

# ================== TELETHON HUMO LISTENER ==================

async def start_humo_listener():
    global telethon_client
    if not ENABLE_HUMO_LISTENER:
        logging.warning("HUMO listener отключён: нет TELEGRAM_API_ID или TELEGRAM_API_HASH")
        return
    api_id = int(TELEGRAM_API_ID)
    api_hash = TELEGRAM_API_HASH
    session = StringSession(TELETHON_SESSION_STRING) if TELETHON_SESSION_STRING else "finance_session"
    telethon_client = TelegramClient(session, api_id, api_hash)

    @telethon_client.on(events.NewMessage(from_users="HUMOcardbot"))
    async def humo_handler(event):
        logging.info("Новое сообщение HUMO получено")
        await process_humo_text(event.raw_text, message_id=event.message.id)

    await telethon_client.start()
    logging.info("HUMO listener запущен")

# ================== WEBHOOK / RAILWAY ==================

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

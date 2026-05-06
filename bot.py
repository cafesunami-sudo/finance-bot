import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
RENDER_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
WEBHOOK_URL = f"https://{RENDER_HOSTNAME}{WEBHOOK_PATH}" if RENDER_HOSTNAME else ""

ALLOWED_USER_ID = 137602775

# Банк отдельно
DEFAULT_BANK_DEPOSIT = 88288796
DEFAULT_BANK_ACCOUNT = 0
DEFAULT_BANK_PERCENT = 0

# Мой обычный баланс: карта/наличные, не банк
DEFAULT_MY_BALANCE = 0

# Кредиты
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

EXPENSE_CATEGORIES = [
    "магазин",
    "рынок",
    "такси",
    "школа",
    "аптека",
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

# ================== БАЗА ==================

conn = sqlite3.connect("finance.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT,
    amount REAL,
    category TEXT,
    comment TEXT,
    date TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bank (
    name TEXT PRIMARY KEY,
    value REAL
)
""")

conn.commit()

# ================== ВОССТАНОВЛЕНИЕ ДАННЫХ ==================

RESTORED_DATA = [
    ("income", 575556, "прочее", "восстановлено", "2026-04-29 16:29:20"),
    ("expense", 40000, "аптека", "аптека купили лекарство для брата", "2026-04-29 22:08:35"),
    ("expense", 40400, "магазин", "магазин купил молоко", "2026-04-29 22:33:24"),
    ("expense", 10000, "школа", "школа купил пирожки", "2026-04-30 15:19:51"),
    ("expense", 60000, "магазин", "магазин", "2026-04-30 20:44:50"),
    ("expense", 50000, "коммунальные", "за мусор", "2026-05-01 11:43:48"),
    ("expense", 100000, "коммунальные", "за свет", "2026-05-01 11:50:31"),
    ("expense", 13000, "школа", "купил пирожки", "2026-05-01 16:57:55"),
]

for item in RESTORED_DATA:
    cursor.execute(
        """
        SELECT id FROM transactions
        WHERE type=? AND amount=? AND category=? AND comment=? AND date=?
        """,
        item
    )
    exists = cursor.fetchone()
    if not exists:
        cursor.execute(
            "INSERT INTO transactions (type, amount, category, comment, date) VALUES (?, ?, ?, ?, ?)",
            item
        )

conn.commit()


def bank_get(name, default=0):
    cursor.execute("SELECT value FROM bank WHERE name=?", (name,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("INSERT INTO bank (name, value) VALUES (?, ?)", (name, default))
        conn.commit()
        return default
    return row[0]


def bank_set(name, value):
    cursor.execute("INSERT OR REPLACE INTO bank (name, value) VALUES (?, ?)", (name, float(value)))
    conn.commit()


bank_get("deposit", DEFAULT_BANK_DEPOSIT)
bank_get("account", DEFAULT_BANK_ACCOUNT)
bank_get("percent", DEFAULT_BANK_PERCENT)
bank_get("my_balance", DEFAULT_MY_BALANCE)

for credit_name, info in CREDITS.items():
    bank_get(info["key"], info["balance"])

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

user_state = {}
pending_expense = {}

# ================== ПОМОЩНИКИ ==================


def now_uz():
    return datetime.utcnow() + timedelta(hours=5)


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
        # 48.000,00 => 48000.00
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        # 48,000.00 => 48000.00
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
        except:
            return None
    return None


def detect_category(text):
    text = normalize_text(text)

    if "магаз" in text:
        return "магазин"
    if "рынок" in text or "bozor" in text:
        return "рынок"
    if "такси" in text or "yandex" in text or "яндекс" in text:
        return "такси"
    if "школ" in text:
        return "школа"
    if "аптек" in text or "лекар" in text:
        return "аптека"
    if any(x in text for x in ["свет", "мусор", "коммун", "газ", "вода"]):
        return "коммунальные"
    if "жалал" in text:
        return "Жалал"
    if "билет" in text:
        return "билет"
    if "фуркат" in text:
        return "Фуркат"
    if "пропан" in text:
        return "заправка пропан"
    if "бензин" in text:
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
        except:
            pass

    return now_uz().strftime("%Y-%m-%d %H:%M:%S")


def next_credit_reminder_text(credit_name):
    info = CREDITS.get(credit_name)
    if not info:
        return ""

    pay_day = int(info.get("pay_day", 1))
    monthly = info.get("monthly", 0)
    balance = bank_get(info["key"], info.get("balance", 0))

    # Формат специально для бота-напоминателя:
    # Кредит Миллий банк | остаток 52 001 041,12 | платеж 2 324 838,79 | дата 5
    title = str(credit_name or "").strip()
    if title.lower().startswith("кредит"):
        title = "К" + title[1:]
    else:
        title = "Кредит " + title

    return f"{title} | остаток {fmt_sum(balance)} | платеж {fmt_sum(monthly)} | дата {pay_day}"


async def send(message: types.Message, text: str, reply_markup=None):
    await bot.send_message(message.chat.id, text, reply_markup=reply_markup)


def is_allowed(message: types.Message):
    return bool(message.from_user and message.from_user.id == ALLOWED_USER_ID)


def save_transaction(t_type, amount, category, comment, date_value=None):
    if date_value is None:
        date_value = now_uz().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        "INSERT INTO transactions (type, amount, category, comment, date) VALUES (?, ?, ?, ?, ?)",
        (t_type, amount, category, comment, date_value)
    )
    conn.commit()
    return cursor.lastrowid


def update_transaction(record_id, category=None, comment=None):
    if category is not None:
        cursor.execute("UPDATE transactions SET category=? WHERE id=?", (category, record_id))
    if comment is not None:
        cursor.execute("UPDATE transactions SET comment=? WHERE id=?", (comment, record_id))
    conn.commit()


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


def apply_credit_payment_with_balance(category, paid_amount, new_credit_balance):
    if category not in CREDITS:
        return None

    info = CREDITS[category]
    old_balance = bank_get(info["key"], info["balance"])
    principal_paid = old_balance - new_credit_balance
    if principal_paid < 0:
        principal_paid = 0
    interest_paid = paid_amount - principal_paid
    if interest_paid < 0:
        interest_paid = 0

    bank_set(info["key"], new_credit_balance)
    return {
        "name": category,
        "paid": paid_amount,
        "old_balance": old_balance,
        "new_balance": new_credit_balance,
        "principal_paid": principal_paid,
        "interest_paid": interest_paid,
        "monthly": info["monthly"],
        "pay_day": info["pay_day"],
    }


def rollback_credit_payment(category, amount):
    if category not in CREDITS:
        return
    info = CREDITS[category]
    balance = bank_get(info["key"], info["balance"])
    bank_set(info["key"], balance + amount)


# ================== SMS ==================


def parse_bank_percent_sms(text):
    raw = str(text or "")
    low = normalize_text(raw)

    if "to'langan %" not in low and "tolangan %" not in low and "omonati" not in low:
        return None

    percent_match = re.search(r"(?:to'?langan\s*%|tolangan\s*%)\s*-\s*([\d\s.,]+)\s*uzs", low, re.I)
    if not percent_match:
        percent_match = re.search(r"%\s*-\s*([\d\s.,]+)\s*uzs", low, re.I)

    balance_match = re.search(r"qoldiq\s*-\s*([\d\s.,]+)\s*uzs", low, re.I)

    if not percent_match:
        return None

    percent = parse_money(percent_match.group(1))
    account = parse_money(balance_match.group(1)) if balance_match else None
    date_value = extract_sms_datetime(raw)

    return {
        "percent": percent,
        "account": account,
        "date": date_value,
    }


def parse_nbu_credit_sms(text):
    raw = str(text or "")
    low = normalize_text(raw)

    if "nbu" not in low and "milliy" not in low and "миллий" not in low:
        return None
    if "kredit" not in low or "qoldig" not in low:
        return None

    paid_match = re.search(r"bo['‘’`]?yicha\s*([\d\s.,]+)\s*so['‘’`]?m\s*yechildi", low, re.I)
    if not paid_match:
        paid_match = re.search(r"([\d\s.,]+)\s*so['‘’`]?m\s*yechildi", low, re.I)

    balance_match = re.search(r"kredit\s+qoldig['‘’`]?i\s*([\d\s.,]+)\s*so['‘’`]?m", low, re.I)

    if not paid_match or not balance_match:
        return None

    return {
        "category": "кредит Миллий банк",
        "amount": parse_money(paid_match.group(1)),
        "credit_balance": parse_money(balance_match.group(1)),
        "date": extract_sms_datetime(raw),
    }


def parse_card_sms(text):
    raw = str(text or "")
    low = normalize_text(raw)

    is_income = any(x in low for x in ["пополнение", "поступление", "kirim", "to'ldirish", "toldirish"])
    is_expense = any(x in low for x in ["оплата", "списание", "xarid", "purchase", "tolov", "to'lov"])

    if not is_income and not is_expense:
        return None

    # Берем первую сумму перед UZS/сум.
    amount_match = re.search(r"([\d\s.,]+)\s*(?:uzs|сум|so['‘’`]?m)", low, re.I)
    if not amount_match:
        return None

    amount = parse_money(amount_match.group(1))
    date_value = extract_sms_datetime(raw)

    return {
        "type": "income" if is_income else "expense",
        "amount": amount,
        "date": date_value,
    }


async def handle_bank_percent_sms(message, parsed):
    percent = parsed["percent"]
    account_from_sms = parsed.get("account")

    bank_set("percent", percent)
    if account_from_sms is not None:
        bank_set("account", account_from_sms)
        account = account_from_sms
    else:
        account = bank_get("account", DEFAULT_BANK_ACCOUNT) + percent
        bank_set("account", account)

    save_transaction("income", percent, "банк", "процент банка", parsed.get("date"))

    await send(
        message,
        f"🏦 SMS банка обработана\n\n"
        f"📈 Процент: {fmt_sum(percent)} сум\n"
        f"💳 На счёте банка: {fmt_sum(account)} сум\n\n"
        f"ℹ️ В обычный отчет это не попадает, банк отдельно."
    )


async def handle_nbu_credit_sms(message, parsed):
    amount = parsed["amount"]
    category = parsed["category"]
    new_credit_balance = parsed["credit_balance"]
    date_value = parsed.get("date")

    balance = bank_get("my_balance", DEFAULT_MY_BALANCE)
    new_my_balance = balance - amount
    bank_set("my_balance", new_my_balance)

    credit_result = apply_credit_payment_with_balance(category, amount, new_credit_balance)

    save_transaction(
        "expense",
        amount,
        category,
        "SMS NBU: оплата кредита Миллий банк",
        date_value
    )

    reminder_text = next_credit_reminder_text(category)

    await send(
        message,
        f"✅ SMS NBU обработана\n\n"
        f"💳 Оплачено с карты: {fmt_sum(amount)} сум\n"
        f"🏦 Категория: {category}\n"
        f"📉 Основной долг уменьшился на: {fmt_sum(credit_result['principal_paid'])} сум\n"
        f"📈 Проценты/прочие начисления: {fmt_sum(credit_result['interest_paid'])} сум\n"
        f"📌 Остаток кредита по SMS: {fmt_sum(credit_result['new_balance'])} сум\n"
        f"💳 Мой баланс: {fmt_sum(new_my_balance)} сум\n\n"
        f"📝 Текст для бота-напоминателя:\n"
        f"{reminder_text}",
        reply_markup=kb
    )


async def handle_card_sms(message, parsed):
    amount = parsed["amount"]
    date_value = parsed["date"]
    balance = bank_get("my_balance", DEFAULT_MY_BALANCE)

    if parsed["type"] == "income":
        new_balance = balance + amount
        bank_set("my_balance", new_balance)
        save_transaction("income", amount, "пополнение", "пополнение карты", date_value)
        await send(
            message,
            f"✅ Пополнение карты сохранено\n\n"
            f"➕ Сумма: {fmt_sum(amount)} сум\n"
            f"💳 Мой баланс: {fmt_sum(new_balance)} сум"
        )
        return

    # Расход по карте: сначала сохраняем, потом обязательно просим категорию.
    new_balance = balance - amount
    bank_set("my_balance", new_balance)
    record_id = save_transaction("expense", amount, "ожидает категорию", "оплата картой", date_value)
    pending_expense[message.from_user.id] = {
        "record_id": record_id,
        "amount": amount,
        "balance": new_balance,
    }
    user_state[message.from_user.id] = "choose_expense_category"

    await send(
        message,
        f"➖ Оплата картой сохранена\n\n"
        f"Сумма: {fmt_sum(amount)} сум\n"
        f"💳 Мой баланс: {fmt_sum(new_balance)} сум\n\n"
        f"Выбери категорию расхода:",
        reply_markup=category_kb
    )


# ================== БАНК ==================


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
        f"• мой баланс 0\n\n"
        f"Или отправь SMS NBU по кредиту Миллий банк."
    )

    await send(message, text)


async def process_bank_command(message, text):
    text = normalize_text(text)
    amount = extract_number(text)

    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)
    percent = bank_get("percent", DEFAULT_BANK_PERCENT)

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
            f"➡️ Сумма: {fmt_sum(amount)} сум\n\n"
            f"💼 Вклад: {fmt_sum(deposit)} сум\n"
            f"💳 На счёте банка: {fmt_sum(account)} сум\n"
            f"💰 Всего в банке: {fmt_sum(deposit + account)} сум"
        )
        return

    if "процент" in text:
        percent = amount
        account += amount
        bank_set("percent", percent)
        bank_set("account", account)
        save_transaction("income", amount, "банк", "процент банка")

        await send(
            message,
            f"🏦 Процент начислен\n\n"
            f"📈 Процент: {fmt_sum(percent)} сум\n"
            f"💳 На счёте банка: {fmt_sum(account)} сум\n\n"
            f"ℹ️ В обычный отчет это не попадает."
        )
        return

    if "вклад" in text:
        deposit = amount
        bank_set("deposit", deposit)
        await send(message, f"🏦 Вклад обновлён\n\n💼 Вклад: {fmt_sum(deposit)} сум")
        return

    if "счет" in text or "счёт" in text:
        account = amount
        bank_set("account", account)
        await send(message, f"🏦 Счёт банка обновлён\n\n💳 На счёте банка: {fmt_sum(account)} сум")
        return

    await bank_report(message)


async def process_my_balance_command(message, text):
    amount = extract_number(text)
    if amount is None:
        await send(message, f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум")
        return

    bank_set("my_balance", amount)
    await send(message, f"💳 Мой баланс обновлён: {fmt_sum(amount)} сум")


async def process_milliy_balance_command(message, text):
    amount = extract_number(text)
    if amount is None:
        current = bank_get(CREDITS["кредит Миллий банк"]["key"], CREDITS["кредит Миллий банк"]["balance"])
        await send(
            message,
            f"💳 Остаток кредита Миллий банк: {fmt_sum(current)} сум\n\n"
            f"Чтобы исправить, напиши:\n"
            f"миллий остаток 52 001 041,12"
        )
        return

    info = CREDITS["кредит Миллий банк"]
    old_balance = bank_get(info["key"], info["balance"])
    bank_set(info["key"], amount)

    await send(
        message,
        f"✅ Остаток Миллий банка исправлен\n\n"
        f"Было: {fmt_sum(old_balance)} сум\n"
        f"Стало: {fmt_sum(amount)} сум\n\n"
        f"ℹ️ Расходы и мой баланс не изменены.",
        reply_markup=kb
    )


# ================== ОТЧЁТЫ ==================


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

        if dt < start:
            continue

        # Банк полностью отдельно: в Сегодня/Неделя/Месяц не показываем.
        if category == "банк":
            continue

        if t == "income":
            income += amount
        else:
            expense += amount
            categories[category] = categories.get(category, 0) + amount

        sign = "➕" if t == "income" else "➖"
        lines.append(f"{sign} {fmt_sum(amount)} — {category} — {comment} ({dt.strftime('%d.%m %H:%M')})")

    text = (
        f"{title}\n\n"
        f"➕ Приход: {fmt_sum(income)} сум\n"
        f"➖ Расход: {fmt_sum(expense)} сум\n"
        f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум\n"
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

    await send(message, text)


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

    if user_state.get(chat_id) == "choose_expense_category":
        selected = raw_text.strip()
        if selected not in EXPENSE_CATEGORIES:
            await send(message, "Выбери категорию кнопкой.", reply_markup=category_kb)
            return

        pending = pending_expense.get(chat_id)
        if not pending:
            user_state[chat_id] = None
            await send(message, "Нет расхода для выбора категории.", reply_markup=kb)
            return

        amount = pending["amount"]
        update_transaction(pending["record_id"], category=selected, comment=selected)

        credit_result = apply_credit_payment(selected, amount)
        user_state[chat_id] = None
        pending_expense.pop(chat_id, None)

        if credit_result:
            await send(
                message,
                f"✅ Категория сохранена: {selected}\n\n"
                f"💳 Платёж по кредиту: {fmt_sum(amount)} сум\n"
                f"📉 Осталось погасить: {fmt_sum(credit_result['new_balance'])} сум\n"
                f"📅 Ежемесячный платёж: {fmt_sum(credit_result['monthly'])} сум\n"
                f"🗓 Дата платежа: {credit_result['pay_day']}-е число каждого месяца",
                reply_markup=kb
            )
        else:
            await send(
                message,
                f"✅ Категория сохранена: {selected}\n"
                f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум",
                reply_markup=kb
            )
        return

    # SMS банка: процент по вкладу
    parsed_bank_sms = parse_bank_percent_sms(raw_text)
    if parsed_bank_sms:
        await handle_bank_percent_sms(message, parsed_bank_sms)
        return

    # SMS NBU по кредиту Миллий банк: берем сумму списания и точный остаток из SMS.
    parsed_nbu_credit_sms = parse_nbu_credit_sms(raw_text)
    if parsed_nbu_credit_sms:
        await handle_nbu_credit_sms(message, parsed_nbu_credit_sms)
        return

    # SMS карты: пополнение или оплата
    parsed_card_sms = parse_card_sms(raw_text)
    if parsed_card_sms:
        await handle_card_sms(message, parsed_card_sms)
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

    if "миллий" in text and "остаток" in text:
        await process_milliy_balance_command(message, raw_text)
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
            msg += (
                f"\n\n💳 {category}\n"
                f"📉 Осталось погасить: {fmt_sum(credit_result['new_balance'])} сум"
            )

        await send(message, msg, reply_markup=kb)
        return

    await send(
        message,
        "Используй кнопки или напиши:\n"
        "расход 20 000 такси\n"
        "приход 1 500 000 зарплата\n"
        "банк процент 52 005,73\n"
        "мой баланс 0\n\n"
        "Или просто отправь SMS банка/карты текстом.",
        reply_markup=kb
    )


async def delete_last(message):
    cursor.execute("SELECT id, type, amount, category, comment FROM transactions ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()

    if not row:
        await send(message, "Удалять нечего")
        return

    record_id, t, amount, category, comment = row
    cursor.execute("DELETE FROM transactions WHERE id=?", (record_id,))
    conn.commit()

    # Откат обычного баланса
    if category != "банк":
        balance = bank_get("my_balance", DEFAULT_MY_BALANCE)
        if t == "income":
            bank_set("my_balance", balance - amount)
        elif t == "expense":
            bank_set("my_balance", balance + amount)
            rollback_credit_payment(category, amount)

    # Откат банковского процента
    if category == "банк" and t == "income" and "процент" in normalize_text(comment):
        account = bank_get("account", DEFAULT_BANK_ACCOUNT)
        bank_set("account", max(0, account - amount))

    type_ru = "приход" if t == "income" else "расход"

    await send(
        message,
        f"🗑 Удалено:\n"
        f"{type_ru} — {fmt_sum(amount)} сум\n"
        f"{category} — {comment}\n\n"
        f"💳 Мой баланс: {fmt_sum(bank_get('my_balance', DEFAULT_MY_BALANCE))} сум"
    )


# ================== HANDLERS ==================


@dp.message_handler(lambda m: m.from_user and m.from_user.id != ALLOWED_USER_ID, content_types=types.ContentTypes.ANY)
async def deny_access(message: types.Message):
    await send(message, "⛔ Доступ запрещён")


@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return

    await send(message, "💰 Финансовый бот готов", reply_markup=kb)


@dp.message_handler(content_types=types.ContentType.VOICE)
async def voice_handler(message: types.Message):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return

    await send(message, "🎙 Голос пока отключён. Пиши текстом или отправь SMS текстом.")


@dp.message_handler(content_types=types.ContentType.PHOTO)
async def photo_handler(message: types.Message):
    if not is_allowed(message):
        await send(message, "⛔ Доступ запрещён")
        return

    await send(message, "📷 Скрин пока не читаю. Скопируй SMS текстом и отправь сюда.")


@dp.message_handler(content_types=types.ContentType.TEXT)
async def text_handler(message: types.Message):
    await process_text(message, message.text)


# ================== WEBHOOK ==================


async def handle_index(request):
    return web.Response(text="Finance bot is running")


async def handle_health(request):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
    return web.Response(text="OK webhook refreshed")


async def handle_webhook(request):
    data = await request.json()
    update = types.Update.to_object(data)
    await dp.process_update(update)
    return web.Response(text="ok")


async def on_startup(app):
    if not WEBHOOK_URL:
        raise RuntimeError("RENDER_EXTERNAL_HOSTNAME не найден")
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_webhook(WEBHOOK_URL)
    print("Webhook set:", WEBHOOK_URL)


async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.close()


def main():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

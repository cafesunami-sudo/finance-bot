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

DEFAULT_BANK_DEPOSIT = 88288796
DEFAULT_BANK_ACCOUNT = 52005.73
DEFAULT_BANK_PERCENT = 52005.73

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
    ("expense", 40000, "здоровье", "аптека купили лекарство для брата", "2026-04-29 22:08:35"),
    ("expense", 40400, "магазин", "магазин купил молоко", "2026-04-29 22:33:24"),
    ("expense", 10000, "школа", "школа купил пирожки", "2026-04-30 15:19:51"),
    ("expense", 60000, "магазин", "магазин", "2026-04-30 20:44:50"),
    ("expense", 50000, "дом", "за мусор", "2026-05-01 11:43:48"),
    ("expense", 100000, "дом", "за свет", "2026-05-01 11:50:31"),
    ("expense", 13000, "школа", "купил пирожки", "2026-05-01 16:57:55"),
]

for item in RESTORED_DATA:
    t_type, amount, category, comment, date = item
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
    cursor.execute("INSERT OR REPLACE INTO bank (name, value) VALUES (?, ?)", (name, value))
    conn.commit()


bank_get("deposit", DEFAULT_BANK_DEPOSIT)
bank_get("account", DEFAULT_BANK_ACCOUNT)
bank_get("percent", DEFAULT_BANK_PERCENT)

# ================== КНОПКИ ==================

kb = ReplyKeyboardMarkup(resize_keyboard=True)
kb.row(KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход"))
kb.row(KeyboardButton("📊 Сегодня"), KeyboardButton("📅 Неделя"), KeyboardButton("🗓 Месяц"))
kb.row(KeyboardButton("💰 Остаток"), KeyboardButton("🏦 Банк"), KeyboardButton("🗑 Удалить"))

user_state = {}

# ================== ПОМОЩНИКИ ==================

def now_uz():
    return datetime.utcnow() + timedelta(hours=5)


def fmt_sum(value):
    value = float(value)
    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def normalize_text(text):
    text = (text or "").lower()
    text = text.replace("ё", "е")
    text = text.replace(",", ".")
    return text.strip()


def extract_number(text):
    text = normalize_text(text)
    match = re.search(r"\d+(?:[\s\d]*\d)?(?:[.,]\d+)?", text)
    if match:
        raw = match.group(0).replace(" ", "").replace(",", ".")
        return float(raw)
    return None


def detect_category(text):
    text = normalize_text(text)

    if any(x in text for x in ["такси", "яндекс"]):
        return "такси"
    if any(x in text for x in ["школа", "пирожки", "сыну"]):
        return "школа"
    if any(x in text for x in ["магазин", "маркет", "молоко"]):
        return "магазин"
    if any(x in text for x in ["аптека", "лекарство", "врач"]):
        return "здоровье"
    if any(x in text for x in ["мусор", "свет", "коммунал", "дом"]):
        return "дом"
    if any(x in text for x in ["банк", "процент", "счет", "счёт"]):
        return "банк"

    return "прочее"


def clean_comment(text):
    text = normalize_text(text)
    text = re.sub(r"\d+(?:[\s\d]*\d)?(?:[.,]\d+)?", "", text)
    text = text.replace("сум", "")
    return " ".join(text.split()) or "без комментария"


async def send(message: types.Message, text: str, reply_markup=None):
    await bot.send_message(message.chat.id, text, reply_markup=reply_markup)


def is_allowed(message: types.Message):
    return bool(message.from_user and message.from_user.id == ALLOWED_USER_ID)


def save_transaction(t_type, amount, category, comment):
    cursor.execute(
        "INSERT INTO transactions (type, amount, category, comment, date) VALUES (?, ?, ?, ?, ?)",
        (t_type, amount, category, comment, now_uz().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()


# ================== БАНК ==================

async def bank_report(message):
    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)
    percent = bank_get("percent", DEFAULT_BANK_PERCENT)

    await send(
        message,
        f"🏦 Банк\n\n"
        f"💼 Вклад: {fmt_sum(deposit)} сум\n"
        f"💳 На счёте: {fmt_sum(account)} сум\n"
        f"📈 Последний процент: {fmt_sum(percent)} сум\n\n"
        f"💰 Всего в банке: {fmt_sum(deposit + account)} сум\n\n"
        f"Можно написать:\n"
        f"• банк счет 52 005,73\n"
        f"• банк процент 52 005,73\n"
        f"• банк вклад 88 288 796"
    )


async def process_bank_command(message, text):
    text = normalize_text(text)
    amount = extract_number(text)

    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)
    percent = bank_get("percent", DEFAULT_BANK_PERCENT)

    if amount is None:
        await bank_report(message)
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
            f"💳 На счёте: {fmt_sum(account)} сум\n"
            f"➕ Также добавлено в приход"
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
        await send(message, f"🏦 Счёт обновлён\n\n💳 На счёте: {fmt_sum(account)} сум")
        return

    await bank_report(message)


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
        f"💰 Остаток: {fmt_sum(income - expense)} сум\n"
    )

    if categories:
        text += "\n🏷 Категории расходов:\n"
        for cat, total in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            text += f"• {cat}: {fmt_sum(total)} сум\n"

    if lines:
        text += "\n🧾 Последние записи:\n"
        for line in lines[-15:]:
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
        await process_bank_command(message, text)
        return

    if "удал" in text:
        await delete_last(message)
        return

    if "приход" in text:
        user_state[message.from_user.id] = "income"
        await send(message, "Введи приход\nНапример: 1 500 000 зарплата")
        return

    if "расход" in text:
        user_state[message.from_user.id] = "expense"
        await send(message, "Введи расход\nНапример: школа 20 000 купил пирожки")
        return

    state = user_state.get(message.from_user.id)

    if state in ["income", "expense"]:
        amount = extract_number(text)
        if amount is None:
            await send(message, "❌ Не понял сумму")
            return

        category = detect_category(text)
        comment = clean_comment(text)

        save_transaction(state, amount, category, comment)
        user_state[message.from_user.id] = None

        type_ru = "приход" if state == "income" else "расход"

        await send(
            message,
            f"✅ Сохранено:\n"
            f"{type_ru} — {fmt_sum(amount)} сум\n"
            f"Категория: {category}\n"
            f"Комментарий: {comment}"
        )
        return

    await send(
        message,
        "Используй кнопки или напиши:\n"
        "расход 20 000 такси\n"
        "приход 1 500 000 зарплата\n"
        "банк процент 52 005,73"
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

    type_ru = "приход" if t == "income" else "расход"

    await send(
        message,
        f"🗑 Удалено:\n"
        f"{type_ru} — {fmt_sum(amount)} сум\n"
        f"{category} — {comment}"
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

    await send(message, "🎙 Голос пока отключён. Пиши текстом: расход 20 000 такси")


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

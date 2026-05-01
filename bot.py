import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ================== TOKEN ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
RENDER_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
WEBHOOK_URL = f"https://{RENDER_HOSTNAME}{WEBHOOK_PATH}"

ALLOWED_USER_ID = 137602775

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
conn.commit()

# ================== КНОПКИ ==================

kb = ReplyKeyboardMarkup(resize_keyboard=True)
kb.row(KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход"))
kb.row(KeyboardButton("📊 Сегодня"), KeyboardButton("📅 Неделя"), KeyboardButton("🗓 Месяц"))
kb.row(KeyboardButton("💰 Остаток"))

user_state = {}

# ================== ФУНКЦИИ ==================

def extract_number(text):
    match = re.search(r"\d+", text)
    return float(match.group()) if match else None


def save_transaction(t_type, amount, comment):
    cursor.execute(
        "INSERT INTO transactions (type, amount, category, comment, date) VALUES (?, ?, ?, ?, ?)",
        (t_type, amount, "прочее", comment, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()


async def send(message, text):
    await bot.send_message(message.chat.id, text, reply_markup=kb)


# ================== ЛОГИКА ==================

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await send(message, "💰 Бот работает")


@dp.message_handler(lambda m: m.text == "➖ Расход")
async def expense_btn(message: types.Message):
    user_state[message.from_user.id] = "expense"
    await send(message, "Введи расход\nНапример: 50000 мусор")


@dp.message_handler(lambda m: m.text == "➕ Приход")
async def income_btn(message: types.Message):
    user_state[message.from_user.id] = "income"
    await send(message, "Введи приход\nНапример: 100000 зарплата")


@dp.message_handler()
async def handler(message: types.Message):
    text = message.text.lower()
    state = user_state.get(message.from_user.id)

    amount = extract_number(text)

    if state == "expense" and amount:
        save_transaction("expense", amount, text)
        await send(message, f"✅ Расход сохранён: {amount}")
        user_state[message.from_user.id] = None
        return

    if state == "income" and amount:
        save_transaction("income", amount, text)
        await send(message, f"✅ Приход сохранён: {amount}")
        user_state[message.from_user.id] = None
        return

    if text.startswith("расход"):
        amount = extract_number(text)
        if amount:
            save_transaction("expense", amount, text)
            await send(message, f"✅ Расход: {amount}")
            return

    if text.startswith("приход"):
        amount = extract_number(text)
        if amount:
            save_transaction("income", amount, text)
            await send(message, f"✅ Приход: {amount}")
            return


# ================== WEBHOOK ==================

async def handle_index(request):
    return web.Response(text="OK")


async def handle_health(request):
    await bot.set_webhook(WEBHOOK_URL)
    return web.Response(text="Webhook refreshed")


async def handle_webhook(request):
    data = await request.json()
    update = types.Update.to_object(data)
    await dp.process_update(update)
    return web.Response()


async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    print("Webhook set:", WEBHOOK_URL)


async def on_shutdown(app):
    await bot.delete_webhook()


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

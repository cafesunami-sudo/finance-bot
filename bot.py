import os
import re
import json
import wave
import sqlite3
import logging
import subprocess
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}{WEBHOOK_PATH}"

ALLOWED_USER_ID = 137602775
FFMPEG_PATH = "ffmpeg"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

conn = sqlite3.connect("finance.db")
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

kb = ReplyKeyboardMarkup(resize_keyboard=True)
kb.row(KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход"))
kb.row(KeyboardButton("📊 Сегодня"), KeyboardButton("📅 Неделя"), KeyboardButton("🗓 Месяц"))
kb.row(KeyboardButton("💰 Остаток"))

user_state = {}

def fmt_sum(value):
    return f"{int(value):,}".replace(",", " ")

def save_transaction(t_type, amount):
    cursor.execute(
        "INSERT INTO transactions (type, amount, category, comment, date) VALUES (?, ?, ?, ?, ?)",
        (t_type, amount, "", "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()

async def report(message):
    cursor.execute("SELECT type, amount FROM transactions")
    rows = cursor.fetchall()

    income = sum(a for t, a in rows if t == "income")
    expense = sum(a for t, a in rows if t == "expense")

    await bot.send_message(
        message.chat.id,
        f"💰 Остаток: {fmt_sum(income - expense)} сум"
    )

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if message.from_user.id != ALLOWED_USER_ID:
        await bot.send_message(message.chat.id, "⛔ Доступ запрещён")
        return

    await bot.send_message(
        message.chat.id,
        "💰 Финансовый бот готов",
        reply_markup=kb
    )

@dp.message_handler(lambda m: m.text == "➕ Приход")
async def income_btn(message: types.Message):
    user_state[message.from_user.id] = "income"
    await bot.send_message(message.chat.id, "Введи сумму")

@dp.message_handler(lambda m: m.text == "➖ Расход")
async def expense_btn(message: types.Message):
    user_state[message.from_user.id] = "expense"
    await bot.send_message(message.chat.id, "Введи сумму")

@dp.message_handler(lambda m: m.text == "💰 Остаток")
async def balance_btn(message: types.Message):
    await report(message)

@dp.message_handler()
async def text_handler(message: types.Message):
    state = user_state.get(message.from_user.id)
    if not state:
        return

    try:
        amount = float(message.text.replace(" ", ""))
    except:
        await bot.send_message(message.chat.id, "❌ Ошибка суммы")
        return

    save_transaction(state, amount)
    user_state[message.from_user.id] = None

    await bot.send_message(message.chat.id, "✅ Сохранено")

# ================= WEBHOOK =================

async def handle_webhook(request):
    data = await request.json()
    update = types.Update.to_object(data)
    await dp.process_update(update)
    return web.Response(text="ok")

async def on_startup(app):
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(app):
    await bot.delete_webhook()

def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()

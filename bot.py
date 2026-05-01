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
from aiogram.contrib.middlewares.logging import LoggingMiddleware

# ================== TOKEN ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}{WEBHOOK_PATH}"

# ================== НАСТРОЙКИ ==================
ALLOWED_USER_ID = 137602775

FFMPEG_PATH = "ffmpeg"

DEFAULT_BANK_DEPOSIT = 88288796
DEFAULT_BANK_ACCOUNT = 575556
DEFAULT_BANK_PERCENT = 52005.73

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# ================== БАЗА ==================
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

cursor.execute("""
CREATE TABLE IF NOT EXISTS bank (
    name TEXT PRIMARY KEY,
    value REAL
)
""")

conn.commit()

# ================== КНОПКИ ==================
kb = ReplyKeyboardMarkup(resize_keyboard=True)
kb.row(KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход"))
kb.row(KeyboardButton("📊 Сегодня"), KeyboardButton("📅 Неделя"), KeyboardButton("🗓 Месяц"))
kb.row(KeyboardButton("💰 Остаток"), KeyboardButton("🏦 Банк"), KeyboardButton("🗑 Удалить"))

user_state = {}

# ================== HANDLERS ==================
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if message.from_user.id != ALLOWED_USER_ID:
        await message.answer("⛔ Доступ запрещён")
        return
    await message.answer("💰 Финансовый бот работает через webhook", reply_markup=kb)

# ================== WEBHOOK ==================

async def handle_webhook(request):
    data = await request.json()
    update = types.Update.to_object(data)
    await dp.process_update(update)
    return web.Response(text="ok")


async def handle_index(request):
    return web.Response(text="Finance bot is running")


async def on_startup(app):
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    print("Webhook set:", WEBHOOK_URL)


async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.close()


def main():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

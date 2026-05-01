import os
import re
import json
import wave
import sqlite3
import logging
import subprocess
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from vosk import Model, KaldiRecognizer

from config import BOT_TOKEN

# ================== НАСТРОЙКИ ==================

ALLOWED_USER_ID = 137602775

FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
VOSK_MODEL_PATH = "models/vosk-model-small-ru-0.22"

DEFAULT_BANK_DEPOSIT = 88288796
DEFAULT_BANK_ACCOUNT = 575556
DEFAULT_BANK_PERCENT = 52005.73

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ================== ЗАЩИТА ==================

@dp.message_handler(lambda m: m.from_user.id != ALLOWED_USER_ID, content_types=types.ContentTypes.ANY)
async def deny_access(message: types.Message):
    await message.answer("⛔ Доступ запрещён")


def is_allowed(message: types.Message) -> bool:
    return message.from_user.id == ALLOWED_USER_ID


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


def bank_get(name, default=0):
    cursor.execute("SELECT value FROM bank WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("INSERT INTO bank (name, value) VALUES (?, ?)", (name, default))
        conn.commit()
        return default
    return row[0]


def bank_set(name, value):
    cursor.execute(
        "INSERT OR REPLACE INTO bank (name, value) VALUES (?, ?)",
        (name, value)
    )
    conn.commit()


bank_get("deposit", DEFAULT_BANK_DEPOSIT)
bank_get("account", DEFAULT_BANK_ACCOUNT)
bank_get("percent", DEFAULT_BANK_PERCENT)

# ================== ГОЛОС ==================

model = Model(VOSK_MODEL_PATH)

# ================== КНОПКИ ==================

kb = ReplyKeyboardMarkup(resize_keyboard=True)
kb.row(KeyboardButton("➕ Приход"), KeyboardButton("➖ Расход"))
kb.row(KeyboardButton("📊 Сегодня"), KeyboardButton("📅 Неделя"), KeyboardButton("🗓 Месяц"))
kb.row(KeyboardButton("💰 Остаток"), KeyboardButton("🏦 Банк"), KeyboardButton("🗑 Удалить"))

user_state = {}

# ================== ФОРМАТ ==================

def fmt_sum(value):
    value = float(value)
    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def normalize_text(text):
    text = text.lower()
    text = text.replace("ё", "е")
    text = text.replace(",", ".")
    return text


def extract_number(text):
    text = normalize_text(text)

    digit_match = re.search(r"\d+(?:[\s\d]*\d)?(?:[.,]\d+)?", text)
    if digit_match:
        raw = digit_match.group(0).replace(" ", "").replace(",", ".")
        try:
            return float(raw)
        except:
            pass

    words = {
        "ноль": 0,
        "один": 1, "одна": 1,
        "два": 2, "две": 2,
        "три": 3,
        "четыре": 4,
        "пять": 5,
        "шесть": 6,
        "семь": 7,
        "восемь": 8,
        "девять": 9,
        "десять": 10,
        "одиннадцать": 11,
        "двенадцать": 12,
        "тринадцать": 13,
        "четырнадцать": 14,
        "пятнадцать": 15,
        "шестнадцать": 16,
        "семнадцать": 17,
        "восемнадцать": 18,
        "девятнадцать": 19,
        "двадцать": 20,
        "тридцать": 30,
        "сорок": 40,
        "пятьдесят": 50,
        "шестьдесят": 60,
        "семьдесят": 70,
        "восемьдесят": 80,
        "девяносто": 90,
        "сто": 100,
        "двести": 200,
        "триста": 300,
        "четыреста": 400,
        "пятьсот": 500,
        "шестьсот": 600,
        "семьсот": 700,
        "восемьсот": 800,
        "девятьсот": 900,
    }

    total = 0
    current = 0

    for word in text.split():
        if word in words:
            current += words[word]
        elif word in ["тысяч", "тысяча", "тысячи"]:
            if current == 0:
                current = 1
            total += current * 1000
            current = 0
        elif word in ["миллион", "миллиона", "миллионов"]:
            if current == 0:
                current = 1
            total += current * 1000000
            current = 0

    total += current
    return float(total) if total > 0 else None


def detect_category(text):
    text = normalize_text(text)

    categories = {
        "такси": ["такси", "яндекс", "yandex"],
        "еда": ["еда", "обед", "ужин", "завтрак", "плов", "самса"],
        "кафе": ["кафе", "ресторан", "кофе"],
        "магазин": ["магазин", "корзинка", "маркет", "базар"],
        "заправка": ["заправка", "бензин", "газ", "топливо"],
        "школа": ["школа", "сыну", "дочке", "ребенку", "пирожки"],
        "интернет": ["интернет", "uztelecom", "wifi", "вайфай"],
        "дом": ["дом", "квартира", "коммуналка"],
        "офис": ["офис", "работа"],
        "одежда": ["одежда", "брюки", "рубашка", "обувь", "куртка"],
        "ремонт машины": ["ремонт машины", "машина", "авто", "сервис"],
        "благотворительность": ["благотворительность", "помощь"],
        "мечеть": ["мечеть", "садака", "пожертвование"],
        "здоровье": ["аптека", "лекарство", "врач", "больница"],
        "семья": ["семья", "жена", "мама", "папа"],
        "долг": ["долг", "занял", "вернул"],
        "банк": ["банк", "вклад", "счет", "процент"],
    }

    for category, keys in categories.items():
        for key in keys:
            if key in text:
                return category

    return "прочее"


def remove_amount_words(text):
    text = normalize_text(text)
    text = re.sub(r"\d+(?:[\s\d]*\d)?(?:[.,]\d+)?", "", text)
    amount_words = [
        "сум", "тысяч", "тысяча", "тысячи", "миллион", "миллиона", "миллионов",
        "один", "одна", "два", "две", "три", "четыре", "пять", "шесть",
        "семь", "восемь", "девять", "десять", "одиннадцать", "двенадцать",
        "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
        "семнадцать", "восемнадцать", "девятнадцать", "двадцать", "тридцать",
        "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят",
        "девяносто", "сто", "двести", "триста", "четыреста", "пятьсот",
        "шестьсот", "семьсот", "восемьсот", "девятьсот"
    ]
    for w in amount_words:
        text = re.sub(rf"\b{w}\b", "", text)
    return " ".join(text.split())


def save_transaction(t_type, amount, category, comment):
    cursor.execute(
        "INSERT INTO transactions (type, amount, category, comment, date) VALUES (?, ?, ?, ?, ?)",
        (t_type, amount, category, comment, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()


# ================== БАНК ==================

async def process_bank_command(message, text):
    text = normalize_text(text)

    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)
    percent = bank_get("percent", DEFAULT_BANK_PERCENT)

    amount = extract_number(text)

    if "процент" in text:
        if amount is None:
            await message.answer("❌ Не понял сумму процента")
            return

        percent = amount
        account += amount

        bank_set("percent", percent)
        bank_set("account", account)

        await message.answer(
            f"🏦 Процент начислен\n\n"
            f"📈 Процент: {fmt_sum(percent)} сум\n"
            f"💳 На счёте: {fmt_sum(account)} сум"
        )
        return

    if "вклад" in text:
        if amount is None:
            await message.answer("❌ Не понял сумму вклада")
            return

        if "плюс" in text or "добав" in text:
            deposit += amount
        elif "минус" in text or "снять" in text:
            deposit -= amount
        else:
            deposit = amount

        bank_set("deposit", deposit)

        await message.answer(
            f"🏦 Вклад обновлён\n\n"
            f"💼 Вклад: {fmt_sum(deposit)} сум"
        )
        return

    if "счет" in text or "счёт" in text:
        if amount is None:
            await message.answer("❌ Не понял сумму счёта")
            return

        if "плюс" in text or "добав" in text:
            account += amount
        elif "минус" in text or "снять" in text:
            account -= amount
        else:
            account = amount

        bank_set("account", account)

        await message.answer(
            f"🏦 Счёт обновлён\n\n"
            f"💳 На счёте: {fmt_sum(account)} сум"
        )
        return

    await bank_report(message)


async def bank_report(message):
    deposit = bank_get("deposit", DEFAULT_BANK_DEPOSIT)
    account = bank_get("account", DEFAULT_BANK_ACCOUNT)
    percent = bank_get("percent", DEFAULT_BANK_PERCENT)
    total = deposit + account

    await message.answer(
        f"🏦 Банк\n\n"
        f"💼 Вклад: {fmt_sum(deposit)} сум\n"
        f"💳 На счёте: {fmt_sum(account)} сум\n"
        f"📈 Последний процент: {fmt_sum(percent)} сум\n\n"
        f"💰 Всего в банке: {fmt_sum(total)} сум\n\n"
        f"Можно написать голосом или текстом:\n"
        f"• банк процент 52 005,73\n"
        f"• банк вклад плюс 500 000\n"
        f"• банк счет 575 556"
    )


# ================== ОТЧЁТЫ ==================

async def report(message, mode):
    now = datetime.now()

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

    cursor.execute("SELECT type, amount, category, comment, date FROM transactions")
    rows = cursor.fetchall()

    income = 0
    expense = 0
    categories = {}
    lines = []

    for t, amount, category, comment, date_str in rows:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except:
            continue

        if dt < start:
            continue

        if t == "income":
            income += amount
        else:
            expense += amount
            categories[category] = categories.get(category, 0) + amount

        sign = "➕" if t == "income" else "➖"
        lines.append(f"{sign} {fmt_sum(amount)} — {category} — {comment}")

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
        for line in lines[-10:]:
            text += line + "\n"

    await message.answer(text)


# ================== ОБРАБОТКА КОМАНД ==================

async def process_text(message, raw_text):
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён")
        return

    text = normalize_text(raw_text)

    if text.startswith("банк") or "банк" in text:
        await process_bank_command(message, text)
        return

    if text.startswith("приход"):
        amount = extract_number(text)
        if amount is None:
            await message.answer("❌ Не понял сумму прихода")
            return

        clean = remove_amount_words(text.replace("приход", ""))
        category = detect_category(clean)
        comment = clean or category

        save_transaction("income", amount, category, comment)

        await message.answer(
            f"✅ Сохранено:\n"
            f"приход — {fmt_sum(amount)} сум\n"
            f"Категория: {category}\n"
            f"Комментарий: {comment}"
        )
        return

    if text.startswith("расход"):
        amount = extract_number(text)
        if amount is None:
            await message.answer("❌ Не понял сумму расхода")
            return

        clean = remove_amount_words(text.replace("расход", ""))
        category = detect_category(clean)
        comment = clean or category

        save_transaction("expense", amount, category, comment)

        await message.answer(
            f"✅ Сохранено:\n"
            f"расход — {fmt_sum(amount)} сум\n"
            f"Категория: {category}\n"
            f"Комментарий: {comment}"
        )
        return

    state = user_state.get(message.from_user.id)

    if state in ["income", "expense"]:
        amount = extract_number(text)
        if amount is None:
            await message.answer("❌ Не понял сумму")
            return

        clean = remove_amount_words(text)
        category = detect_category(clean)
        comment = clean or category

        save_transaction(state, amount, category, comment)
        user_state[message.from_user.id] = None

        type_ru = "приход" if state == "income" else "расход"

        await message.answer(
            f"✅ Сохранено:\n"
            f"{type_ru} — {fmt_sum(amount)} сум\n"
            f"Категория: {category}\n"
            f"Комментарий: {comment}"
        )
        return

    await message.answer(
        "Используй кнопки или напиши:\n"
        "расход 20 000 такси\n"
        "приход 1 500 000 зарплата\n"
        "банк процент 52 005,73"
    )


# ================== HANDLERS ==================

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён")
        return

    await message.answer("💰 Финансовый бот готов", reply_markup=kb)


@dp.message_handler(lambda m: m.text == "➕ Приход")
async def income_btn(message: types.Message):
    if not is_allowed(message):
        return
    user_state[message.from_user.id] = "income"
    await message.answer("Введи приход\nНапример: 1 500 000 зарплата")


@dp.message_handler(lambda m: m.text == "➖ Расход")
async def expense_btn(message: types.Message):
    if not is_allowed(message):
        return
    user_state[message.from_user.id] = "expense"
    await message.answer("Введи расход или отправь голос\nНапример: школа 20 000 купил пирожки")


@dp.message_handler(lambda m: m.text == "📊 Сегодня")
async def today_btn(message: types.Message):
    if not is_allowed(message):
        return
    await report(message, "today")


@dp.message_handler(lambda m: m.text == "📅 Неделя")
async def week_btn(message: types.Message):
    if not is_allowed(message):
        return
    await report(message, "week")


@dp.message_handler(lambda m: m.text == "🗓 Месяц")
async def month_btn(message: types.Message):
    if not is_allowed(message):
        return
    await report(message, "month")


@dp.message_handler(lambda m: m.text == "💰 Остаток")
async def balance_btn(message: types.Message):
    if not is_allowed(message):
        return
    await report(message, "all")


@dp.message_handler(lambda m: m.text == "🏦 Банк")
async def bank_btn(message: types.Message):
    if not is_allowed(message):
        return
    await bank_report(message)


@dp.message_handler(lambda m: m.text == "🗑 Удалить")
async def delete_btn(message: types.Message):
    if not is_allowed(message):
        return

    cursor.execute("SELECT id, type, amount, category, comment FROM transactions ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()

    if not row:
        await message.answer("Удалять нечего")
        return

    record_id, t, amount, category, comment = row
    cursor.execute("DELETE FROM transactions WHERE id = ?", (record_id,))
    conn.commit()

    type_ru = "приход" if t == "income" else "расход"

    await message.answer(
        f"🗑 Удалено:\n"
        f"{type_ru} — {fmt_sum(amount)} сум\n"
        f"{category} — {comment}"
    )


@dp.message_handler(content_types=types.ContentType.VOICE)
async def voice_handler(message: types.Message):
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён")
        return

    try:
        os.makedirs("voice/ogg", exist_ok=True)
        os.makedirs("voice/wav", exist_ok=True)

        file = await bot.get_file(message.voice.file_id)

        ogg_path = f"voice/ogg/{message.voice.file_id}.ogg"
        wav_path = f"voice/wav/{message.voice.file_id}.wav"

        await bot.download_file(file.file_path, ogg_path)

        subprocess.run(
            [
                FFMPEG_PATH,
                "-y",
                "-i", ogg_path,
                "-ar", "16000",
                "-ac", "1",
                wav_path
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )

        wf = wave.open(wav_path, "rb")
        rec = KaldiRecognizer(model, wf.getframerate())

        text = ""

        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break

            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text += " " + result.get("text", "")

        result = json.loads(rec.FinalResult())
        text += " " + result.get("text", "")
        text = text.strip()

        await message.answer(f"🎙 Распознано: {text}")

        if not text:
            await message.answer("❌ Не смог распознать голос")
            return

        await process_text(message, text)

    except Exception as e:
        logging.error(e)
        await message.answer("❌ Ошибка при обработке голоса")


@dp.message_handler()
async def text_handler(message: types.Message):
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён")
        return

    await process_text(message, message.text)


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
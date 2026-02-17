import os
import logging
import sqlite3
import random
import requests
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
import uvicorn

# === НАСТРОЙКИ ===
API_TOKEN = os.getenv("8533386323:AAE4ztLPhnBguDvJjaSM-dcKVRAsW4m-pzQ"
WALLET_ADDRESS = "TV8V9k6FsydVRzHwgtYXoNVTTcqF1UvFyk"
ADMIN_ID = 8333494757
ENTRY_FEE = 5

# Логирование
logging.basicConfig(level=logging.INFO)

# === ИНИЦИАЛИЗАЦИЯ БОТА ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# === БАЗА ДАННЫХ ===
conn = sqlite3.connect("/data/crypto_fortuna.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE
    )
""")
conn.commit()

# === КЛАВИАТУРА ===
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard.add(
    KeyboardButton("🎟 Участвовать"),
    KeyboardButton("💰 Банк"),
    KeyboardButton("👥 Участники")
)
keyboard.add(KeyboardButton("🎲 Выбрать победителя"))

# === ХЕНДЛЕРЫ (ОБРАБОТЧИКИ КОМАНД) ===

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    await message.answer(
        "🚀 Добро пожаловать в Crypto Fortuna Bot!\n"
        f"💰 Взнос: {ENTRY_FEE} USDT\n\n"
        "Выбери действие 👇",
        reply_markup=keyboard
    )

@dp.message_handler(lambda message: message.text == "🎟 Участвовать")
async def participate(message: types.Message):
    await message.answer(
        f"🔹 Для участия переведи {ENTRY_FEE} USDT\n"
        f"🔹 Сеть: TRC20\n"
        f"🔹 Адрес:\n`{WALLET_ADDRESS}`\n\n"
        "📤 После оплаты отправь сюда TXID (хэш транзакции)",
        parse_mode="Markdown"
    )

@dp.message_handler(lambda message: message.text == "💰 Банк")
async def bank(message: types.Message):
    cursor.execute("SELECT COUNT(*) FROM participants")
    count = cursor.fetchone()[0]
    total_bank = count * ENTRY_FEE
    await message.answer(f"💰 Текущий банк: {total_bank} USDT")

@dp.message_handler(lambda message: message.text == "👥 Участники")
async def members(message: types.Message):
    cursor.execute("SELECT COUNT(*) FROM participants")
    count = cursor.fetchone()[0]
    await message.answer(f"👥 Всего участников: {count}")

@dp.message_handler(lambda message: message.text == "🎲 Выбрать победителя")
async def choose_winner(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    cursor.execute("SELECT username FROM participants")
    users = cursor.fetchall()
    
    if not users:
        await message.answer("❌ Нет участников для розыгрыша")
        return
    
    winner = random.choice(users)[0]
    total_users = len(users)
    bank = total_users * ENTRY_FEE
    commission = bank * 0.10
    winner_prize = bank - commission
    
    await message.answer(
        f"🏆 **Победитель:** {winner}\n\n"
        f"👥 Участников: {total_users}\n"
        f"💰 Общий банк: {bank} USDT\n"
        f"💸 Комиссия (10%): {commission:.2f} USDT\n"
        f"🎁 Выигрыш: {winner_prize:.2f} USDT",
        parse_mode="Markdown"
    )
    
    cursor.execute("DELETE FROM participants")
    conn.commit()
    await message.answer("🔄 Раунд завершён. Банк обнулён.")

@dp.message_handler(commands=['add'])
async def add_participant(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    username = message.get_args()
    if not username:
        await message.answer("Используй: /add @username")
        return
    
    try:
        cursor.execute("INSERT INTO participants (username) VALUES (?)", (username,))
        conn.commit()
        await message.answer(f"✅ Участник {username} добавлен!")
    except sqlite3.IntegrityError:
        await message.answer("⚠️ Этот участник уже добавлен")

# === ОБРАБОТЧИК TXID (ВРЕМЕННЫЙ) ===
@dp.message_handler()
async def handle_txid(message: types.Message):
    # Позже здесь будет проверка через Tronscan API
    await message.answer("📝 Твой TXID получен. После проверки ты будешь добавлен в розыгрыш.")

# === WEBHOOK ЧАСТЬ ===
app = FastAPI()

@app.post(f"/webhook/{API_TOKEN}")
async def telegram_webhook(request: Request):
    """Сюда Telegram будет присылать все обновления"""
    update_data = await request.json()
    update = types.Update.to_object(update_data)
    await dp.process_update(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "Crypto Fortuna Bot is running"}

@app.on_event("startup")
async def on_startup():
    """При запуске устанавливаем webhook"""
    webhook_url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}/webhook/{API_TOKEN}"
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook установлен на {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    """При остановке удаляем webhook"""
    await bot.delete_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

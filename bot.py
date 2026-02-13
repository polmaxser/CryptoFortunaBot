from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import logging
import sqlite3
import random

API_TOKEN = "8533386323:AAE4ztLPhnBguDvJjaSM-dcKVRAsW4m-pzQ"
WALLET_ADDRESS = "TV8V9k6FsydVRzHwgtYXoNVTTcqF1UvFyk"

ADMIN_ID = 8333494757

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

conn = sqlite3.connect("crypto_fortuna.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT
)
""")
conn.commit()

keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard.add(
    KeyboardButton("🎟 Участвовать"),
    KeyboardButton("📊 Банк"),
    KeyboardButton("👥 Участники")
)
keyboard.add(
    KeyboardButton("🏆 Выбрать победителя")
)

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    await message.answer(
        "🍀 Добро пожаловать в Crypto Fortuna Bot!\n"
        "💰 Взнос: 5 USDT\n\n"
        "Выбери действие 👇",
        reply_markup=keyboard
    )

@dp.message_handler(lambda message: message.text == "🎟 Участвовать")
async def participate(message: types.Message):
    await message.answer(
        f"🎟 Для участия переведи 5 USDT\n\n"
        f"💳 Сеть: TRC20\n"
        f"📍 Адрес:\n{WALLET_ADDRESS}\n\n"
        f"После оплаты ожидай подтверждения 🍀"
    )

@dp.message_handler(lambda message: message.text == "📊 Банк")
async def bank(message: types.Message):
    cursor.execute("SELECT COUNT(*) FROM participants")
    count = cursor.fetchone()[0]
    total_bank = count * 5
    await message.answer(f"📊 Текущий банк: {total_bank} USDT")

@dp.message_handler(lambda message: message.text == "👥 Участники")
async def members(message: types.Message):
    cursor.execute("SELECT COUNT(*) FROM participants")
    count = cursor.fetchone()[0]
    await message.answer(f"👥 Всего участников: {count}")

@dp.message_handler(commands=['add'])
async def add_participant(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    username = message.get_args()

    if not username:
        await message.answer("Укажите пользователя: /add @username")
        return

    cursor.execute("SELECT * FROM participants WHERE username = ?", (username,))
    existing_user = cursor.fetchone()

    if existing_user:
        await message.answer("❌ Этот участник уже добавлен.")
        return

    cursor.execute("INSERT INTO participants (username) VALUES (?)", (username,))
    conn.commit()

    await message.answer(f"✅ Участник {username} добавлен!")

@dp.message_handler(lambda message: message.text == "🏆 Выбрать победителя")
async def choose_winner(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute("SELECT username FROM participants")
    users = cursor.fetchall()

    if not users:
        await message.answer("Нет участников для розыгрыша.")
        return

    winner = random.choice(users)[0]
    total_users = len(users)
    bank = total_users * 5

    commission = bank * 0.10
    winner_prize = bank - commission

    await message.answer(
        f"🏆 Победитель: {winner}\n\n"
        f"👥 Участников: {total_users}\n"
        f"🏦 Общий банк: {bank} USDT\n"
        f"💸 Комиссия организатора (10%): {commission:.2f} USDT\n"
        f"💰 Выигрыш победителя: {winner_prize:.2f} USDT 🎉"
    )

    cursor.execute("DELETE FROM participants")
    conn.commit()

    await message.answer("🔄 Раунд завершён. Банк обнулён.")

    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute("SELECT username FROM participants")
    users = cursor.fetchall()

    if not users:
        await message.answer("Нет участников для розыгрыша.")
        return

    winner = random.choice(users)[0]

    await message.answer(f"🏆 Победитель: {winner} 🎉")

    cursor.execute("DELETE FROM participants")
    conn.commit()

    await message.answer("Банк обнулён. Начинаем новый раунд 🚀")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)


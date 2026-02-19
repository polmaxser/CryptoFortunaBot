import os
import logging
import sqlite3
import random
import requests
import time
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import uvicorn

def check_trc20_payment(txid, expected_amount=5, expected_address=None):
    """Проверяет транзакцию USDT TRC20 через Tronscan API"""
    if expected_address is None:
        expected_address = WALLET_ADDRESS  # берём глобальную переменную
    
    try:
        # Ждём 10 секунд, чтобы транзакция точно попала в блокчейн
        time.sleep(10)
        
        url = f"https://apilist.tronscan.org/api/transaction-info?hash={txid}"
        response = requests.get(url)
        
        if response.status_code != 200:
            return False, "Ошибка при обращении к блокчейну"
        
        data = response.json()
        
        # Проверяем, что это перевод токена
        if 'tokenTransfer' not in data or not data['tokenTransfer']:
            return False, "Это не транзакция с токеном"
        
        transfer = data['tokenTransfer']
        
        # Проверяем, что это USDT (contract address)
        usdt_contract = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        if transfer.get('contract') != usdt_contract:
            return False, "Это не USDT"
        
        # Проверяем адрес получателя
        if transfer.get('to_address') != expected_address:
            return False, f"Неверный адрес получателя. Ожидался: {expected_address}, получен: {transfer.get('to_address')}"
        
        # Проверяем сумму (в USDT 6 знаков после запятой)
        amount = int(transfer.get('amount', 0)) / 1_000_000
        if amount < expected_amount:
            return False, f"Недостаточно средств: {amount} USDT (нужно {expected_amount})"
        
        return True, f"OK: {amount} USDT"
        
    except Exception as e:
        return False, f"Ошибка: {str(e)}"

# === НАСТРОЙКИ ===
API_TOKEN = os.getenv("BOT_TOKEN")
WALLET_ADDRESS = "TV8V9k6FsydVRzHwgtYXoNVTTcqF1UvFyk"
ADMIN_ID = 8333494757
ENTRY_FEE = 5

# Логирование
logging.basicConfig(level=logging.INFO)

CHANNEL_ID = "@real_crypto_fortuna"

# === ИНИЦИАЛИЗАЦИЯ БОТА ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# === БАЗА ДАННЫХ ===
conn = sqlite3.connect("crypto_fortuna.db", check_same_thread=False)
cursor = conn.cursor()

# Таблица участников
cursor.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        txid TEXT PRIMARY KEY,
        user_id INTEGER,
        username TEXT,
        amount REAL,
        status TEXT DEFAULT 'confirmed',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

conn.commit()

# === КЛАВИАТУРА ===
keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard.add(
    KeyboardButton("🎟 Участвовать"),
    KeyboardButton("💰 Банк"),
    KeyboardButton("👥 Участники")
)
keyboard.add(KeyboardButton("🎲 Выбрать победителя"))

# === ХЕНДЛЕРЫ ===
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

@dp.message_handler(commands=['start_draw'])
async def cmd_start_draw(message: types.Message):
    """Запускает прозрачный розыгрыш (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Эта команда только для админа")
        return
    
    # Получаем список участников
    cursor.execute("SELECT username FROM participants")
    participants = [f"@{row[0]}" for row in cursor.fetchall()]
    
    if len(participants) < 2:
        await message.answer("❌ Для розыгрыша нужно минимум 2 участника")
        return
    
    # Определяем номер раунда
    round_number = random.randint(1000, 9999)
    
    # Получаем текущий блок и вычисляем целевой
    current_block = get_current_tron_block()
    if not current_block:
        await message.answer("❌ Не удалось получить номер блока TRON")
        return
    
    target_block = current_block + 20
    
    # Публикуем информацию в канал
    await publish_round_info(CHANNEL_ID, round_number, participants, target_block)
    await message.answer(f"✅ Информация о розыгрыше #{round_number} опубликована в канале")
    
    await message.answer(f"⏳ Розыгрыш состоится через 2 минуты (блок #{target_block})")
    
    # Ждём 2 минуты
    import asyncio
    await asyncio.sleep(120)
    
    # Проводим розыгрыш
    winner = await execute_provable_draw(CHANNEL_ID, round_number, participants, target_block)
    
    # Очищаем участников
    cursor.execute("DELETE FROM participants")
    conn.commit()
    
    await message.answer(f"✅ Розыгрыш #{round_number} завершён! Победитель: {winner}")

@dp.message_handler()
async def handle_txid(message: types.Message):
    txid = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    # Проверяем, не использовался ли уже этот TXID
    cursor.execute("SELECT * FROM transactions WHERE txid = ?", (txid,))
    if cursor.fetchone():
        await message.answer("❌ Этот TXID уже был использован")
        return
    
    # Отправляем сообщение о начале проверки
    wait_msg = await message.answer("🔄 Проверяю транзакцию... Это может занять до 20 секунд")
    
    # Проверяем транзакцию
    success, msg = check_trc20_payment(txid)
    
    if success:
        # Добавляем пользователя в participants
        try:
            cursor.execute(
                "INSERT INTO participants (username) VALUES (?)", 
                (f"@{username}",)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Пользователь уже есть в participants
            pass
        
        # Сохраняем TXID в базу
        cursor.execute(
            "INSERT INTO transactions (txid, user_id, username, amount) VALUES (?, ?, ?, ?)",
            (txid, user_id, username, 5)
        )
        conn.commit()
        
        await message.answer(f"✅ Транзакция подтверждена!\n"
                            f"Ты добавлен в розыгрыш 🎟")
    else:
        await message.answer(f"❌ Ошибка: {msg}")
    
    # Удаляем сообщение о проверке
    await wait_msg.delete()

@dp.message_handler(commands=['start_draw'])
async def cmd_start_draw(message: types.Message):
    """Запускает прозрачный розыгрыш (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    # Получаем список участников
    cursor.execute("SELECT username FROM participants")
    participants = [f"@{row[0]}" for row in cursor.fetchall()]
    
    if len(participants) < 2:
        await message.answer("❌ Для розыгрыша нужно минимум 2 участника")
        return
    
    # Определяем номер раунда
    round_number = random.randint(1000, 9999)  # можно хранить в БД, но пока так
    
    # Получаем текущий блок и вычисляем целевой (через ~2 минуты)
    current_block = get_current_tron_block()
    if not current_block:
        await message.answer("❌ Не удалось получить номер блока TRON")
        return
    
    target_block = current_block + 20  # +20 блоков = ~1 минута
    
    # Публикуем информацию в канал
    await publish_round_info(CHANNEL_ID, round_number, participants, target_block)
    await message.answer(f"✅ Информация о розыгрыше #{round_number} опубликована в канале")
    
    # Запускаем розыгрыш через 2 минуты
    await message.answer(f"⏳ Розыгрыш состоится через 2 минуты (блок #{target_block})")
    
    # Ждём до блока (упрощённо - просто 2 минуты)
    import asyncio
    await asyncio.sleep(120)
    
    # Проводим розыгрыш
    winner = await execute_provable_draw(CHANNEL_ID, round_number, participants, target_block)
    
    # Очищаем участников
    cursor.execute("DELETE FROM participants")
    conn.commit()
    
    await message.answer(f"✅ Розыгрыш #{round_number} завершён! Победитель: {winner}")

# === PROVABLY FAIR РОЗЫГРЫШ ===
import hashlib
import requests
import time

def get_current_tron_block():
    """Получает номер последнего блока TRON"""
    try:
        url = "https://api.trongrid.io/v1/blocks?limit=1"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data['data'][0]['block_number']
    except Exception as e:
        logging.error(f"Ошибка получения блока: {e}")
    return None

def get_tron_block_hash(block_number):
    """Получает хэш блока TRON по номеру"""
    try:
        url = f"https://api.trongrid.io/v1/blocks/{block_number}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data['blockID']
    except Exception as e:
        logging.error(f"Ошибка получения хэша: {e}")
    return None

async def publish_round_info(chat_id, round_number, participants, target_block):
    """Публикует информацию о раунде перед розыгрышем"""
    
    # Формируем список участников с номерами
    tickets = []
    for i, user in enumerate(participants, 1):
        tickets.append(f"{i}. {user}")
    
    tickets_text = "\n".join(tickets[:20])  # покажем только 20 первых, если много
    if len(participants) > 20:
        tickets_text += f"\n... и ещё {len(participants) - 20}"
    
    message = (
        f"🎲 **РОЗЫГРЫШ #{round_number}**\n\n"
        f"🎟 **Всего билетов:** {len(participants)}\n\n"
        f"**Список участников:**\n{tickets_text}\n\n"
        f"🔐 **Прозрачный выбор победителя:**\n"
        f"1️⃣ Будет взят хэш блока TRON **#{target_block}**\n"
        f"2️⃣ Победитель = хэш % {len(participants)}\n"
        f"3️⃣ Результат появится здесь сразу после получения блока\n\n"
        f"⏳ Ожидайте розыгрыша..."
    )
    
    await bot.send_message(chat_id, message, parse_mode="Markdown")

async def execute_provable_draw(chat_id, round_number, participants, target_block):
    """Проводит provably fair розыгрыш и публикует результат"""
    
    # Отправляем сообщение о начале
    wait_msg = await bot.send_message(chat_id, "⏳ **Получаю хэш блока TRON...**", parse_mode="Markdown")
    
    # Ждём блок (максимум 3 минуты)
    block_hash = None
    for attempt in range(36):  # 36 * 5 сек = 3 минуты
        time.sleep(5)
        block_hash = get_tron_block_hash(target_block)
        if block_hash:
            break
    
    if not block_hash:
        await bot.edit_message_text(
            "❌ **Ошибка:** не удалось получить хэш блока. Попробуйте позже.",
            chat_id, wait_msg.message_id, parse_mode="Markdown"
        )
        return
    
    # Вычисляем победителя
    hash_int = int(block_hash, 16)
    winner_index = hash_int % len(participants)
    winner = participants[winner_index]
    
    # Формируем результат
    result = (
        f"🏆 **РОЗЫГРЫШ #{round_number} ЗАВЕРШЁН!**\n\n"
        f"✅ **Блок TRON:** #{target_block}\n"
        f"🔗 **Хэш блока:**\n`{block_hash[:32]}...`\n\n"
        f"**Расчёт:**\n"
        f"`{block_hash[:16]}...` (хэш) % {len(participants)} = **{winner_index + 1}**\n\n"
        f"🎉 **Победитель: Билет №{winner_index + 1} — {winner}**\n\n"
        f"🔍 **[Проверить на Tronscan](https://tronscan.org/#/block/{target_block})**"
    )
    
    # Обновляем сообщение с результатом
    await bot.edit_message_text(
        result, chat_id, wait_msg.message_id,
        parse_mode="Markdown", disable_web_page_preview=True
    )
    
    # Отправляем результат в канал
    await bot.send_message(
        CHANNEL_ID, 
        f"🎲 Результат розыгрыша #{round_number}: {winner}",
        parse_mode="Markdown"
    )
    
    return winner

# === WEBHOOK ЧАСТЬ ===
app = FastAPI()

@app.post(f"/webhook/{API_TOKEN}")
async def telegram_webhook(request: Request):
    update_data = await request.json()
    update = types.Update.to_object(update_data)
    Bot.set_current(bot)
    await dp.process_update(update)
    return {"ok": True}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "time": time.time()}

@app.get("/")
async def root():
    return {"status": "Crypto Fortuna Bot is running on Render"}

@app.on_event("startup")
async def on_startup():
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url:
        logging.error("❌ RENDER_EXTERNAL_URL не найден!")
        return
    webhook_url = f"{render_url}/webhook/{API_TOKEN}"
    await bot.set_webhook(webhook_url)
    logging.info(f"✅ Webhook установлен на {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
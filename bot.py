import os
import logging
import sqlite3
import random
import requests
import time
import asyncio
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import uvicorn

# === НАСТРОЙКИ ===
API_TOKEN = os.getenv("BOT_TOKEN")
WALLET_ADDRESS = "0xFd434c30aCeF2815fE895a2144b11122e31c0B93"
ADMIN_ID = 8333494757
ENTRY_FEE = 5
CHANNEL_ID = "@real_crypto_fortuna"

# Логирование
logging.basicConfig(level=logging.INFO)

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

# === ФУНКЦИИ ПРОВЕРКИ ПЛАТЕЖЕЙ BSC ===
def check_bsc_payment(txid, expected_amount=5, expected_address=None):
    """Проверяет транзакцию USDT BEP-20 через BSCTrace API (MegaNode)"""
    if expected_address is None:
        expected_address = WALLET_ADDRESS
    
    # Делаем несколько попыток с увеличивающейся задержкой
    for attempt in range(1, 4):
        try:
            time.sleep(10 * attempt)
            
            # BSCTrace использует JSON-RPC формат [citation:2]
            api_key = os.getenv("MEGANODE_API_KEY")
            url = f"https://bsc-mainnet.nodereal.io/v1/{api_key}"
            
            # Получаем детали транзакции через eth_getTransactionByHash
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_getTransactionByHash",
                "params": [txid],
                "id": 1
            }
            response = requests.post(url, json=payload)
            
            if response.status_code != 200:
                if attempt < 3:
                    continue
                return False, "Ошибка при обращении к BSCTrace"
            
            data = response.json()
            
            if 'result' not in data or not data['result']:
                if attempt < 3:
                    continue
                return False, "Транзакция не найдена"
            
            tx = data['result']
            
            # Проверяем, что это перевод токена (USDT)
            # Для токенов нужно дополнительно проверить лог транзакции
            receipt_payload = {
                "jsonrpc": "2.0",
                "method": "eth_getTransactionReceipt",
                "params": [txid],
                "id": 2
            }
            receipt_response = requests.post(url, json=receipt_payload)
            
            if receipt_response.status_code != 200:
                return False, "Не удалось получить подтверждение транзакции"
            
            receipt_data = receipt_response.json()
            if 'result' not in receipt_data or not receipt_data['result']:
                return False, "Транзакция не подтверждена"
            
            receipt = receipt_data['result']
            
            # Проверяем адрес получателя
            if tx['to'].lower() != expected_address.lower():
                return False, "Неверный адрес получателя"
            
            # Контракт USDT в BSC
            usdt_contract = "0x55d398326f99059ff775485246999027b3197955"
            if tx['to'].lower() != usdt_contract.lower():
                # Если это не прямой вызов контракта, проверяем логи
                found_transfer = False
                if 'logs' in receipt:
                    for log in receipt['logs']:
                        if log['address'].lower() == usdt_contract.lower():
                            # Парсим данные перевода
                            # topics[0] = Transfer event signature
                            # topics[1] = from address
                            # topics[2] = to address
                            # data = amount
                            if len(log['topics']) >= 3:
                                to_address = '0x' + log['topics'][2][-40:]
                                if to_address.lower() == expected_address.lower():
                                    amount = int(log['data'], 16) / 10**18
                                    if amount >= expected_amount:
                                        return True, f"OK: {amount} USDT"
            
            return False, "Это не перевод USDT"
            
        except Exception as e:
            if attempt == 3:
                return False, f"Ошибка: {str(e)}"
            continue
    
    return False, "Не удалось проверить транзакцию после нескольких попыток"

# === ФУНКЦИИ ДЛЯ ПОЛУЧЕНИЯ БЛОКОВ BSC ===
def get_current_bsc_block():
    """Получает номер последнего блока BSC"""
    try:
        api_key = os.getenv("BSCSCAN_API_KEY")
        url = f"https://api.etherscan.io/v2/api?chainid=56&module=block&action=getblocknobytime&timestamp=latest&closest=before&apikey={api_key}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return int(data['result'])
    except Exception as e:
        logging.error(f"Ошибка получения блока BSC: {e}")
    return None

def get_bsc_block_hash(block_number):
    """Получает хэш блока BSC через BSCTrace API"""
    try:
        api_key = os.getenv("MEGANODE_API_KEY")
        url = f"https://bsc-mainnet.nodereal.io/v1/{api_key}"
        
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [hex(block_number), False],
            "id": 1
        }
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if 'result' in data and data['result']:
                return data['result']['hash']
    except Exception as e:
        logging.error(f"Ошибка получения хэша BSC: {e}")
    return None

# === ФУНКЦИИ ДЛЯ ПРОВЕДЕНИЯ РОЗЫГРЫША ===
async def publish_round_info(chat_id, round_number, participants, target_block):
    """Публикует информацию о раунде перед розыгрышем"""
    tickets = []
    for i, user in enumerate(participants, 1):
        tickets.append(f"{i}. {user}")
    
    tickets_text = "\n".join(tickets[:20])
    if len(participants) > 20:
        tickets_text += f"\n... и ещё {len(participants) - 20}"
    
    message = (
        f"🎲 **РОЗЫГРЫШ #{round_number}**\n\n"
        f"🎟 **Всего билетов:** {len(participants)}\n\n"
        f"**Список участников:**\n{tickets_text}\n\n"
        f"🔐 **Прозрачный выбор победителя:**\n"
        f"1️⃣ Будет взят хэш блока BSC **#{target_block}**\n"
        f"2️⃣ Победитель = хэш % {len(participants)}\n"
        f"3️⃣ Результат появится здесь сразу после получения блока\n\n"
        f"⏳ Ожидайте розыгрыша..."
    )
    await bot.send_message(chat_id, message, parse_mode="Markdown")

async def execute_provable_draw_bsc(chat_id, round_number, participants, target_block):
    """Проводит provably fair розыгрыш на BSC"""
    wait_msg = await bot.send_message(chat_id, "⏳ **Получаю хэш блока BSC...**", parse_mode="Markdown")
    
    block_hash = None
    for attempt in range(36):
        await asyncio.sleep(5)
        block_hash = get_bsc_block_hash(target_block)
        if block_hash:
            break
        if attempt % 6 == 0 and attempt > 0:
            await bot.edit_message_text(
                f"⏳ **Получаю хэш блока BSC...** (попытка {attempt//6+1}/6)",
                chat_id, wait_msg.message_id, parse_mode="Markdown"
            )
    
    if not block_hash:
        await bot.edit_message_text(
            "❌ **Ошибка:** не удалось получить хэш блока. Попробуйте позже.",
            chat_id, wait_msg.message_id, parse_mode="Markdown"
        )
        return None
    
    hash_int = int(block_hash, 16)
    winner_index = hash_int % len(participants)
    winner = participants[winner_index]
    
    result = (
        f"🏆 **РОЗЫГРЫШ #{round_number} ЗАВЕРШЁН!**\n\n"
        f"✅ **Блок BSC:** #{target_block}\n"
        f"🔗 **Хэш блока:**\n`{block_hash[:32]}...`\n\n"
        f"**Расчёт:**\n"
        f"`{block_hash[:16]}...` (хэш) % {len(participants)} = **{winner_index + 1}**\n\n"
        f"🎉 **Победитель: Билет №{winner_index + 1} — {winner}**\n\n"
        f"🔍 **[Проверить на BscScan](https://bscscan.com/block/{target_block})**"
    )
    
    await bot.edit_message_text(
        result, chat_id, wait_msg.message_id,
        parse_mode="Markdown", disable_web_page_preview=True
    )
    
    await bot.send_message(
        CHANNEL_ID,
        f"🎲 Результат розыгрыша #{round_number}: {winner}",
        parse_mode="Markdown"
    )
    return winner

# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
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
        f"🔹 Сеть: BSC (BEP-20)\n"
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
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Эта команда только для админа")
        return
    
    cursor.execute("SELECT username FROM participants")
    participants = [f"@{row[0]}" for row in cursor.fetchall()]
    
    if len(participants) < 2:
        await message.answer("❌ Для розыгрыша нужно минимум 2 участника")
        return
    
    round_number = random.randint(1000, 9999)
    
    current_block = get_current_bsc_block()
    if not current_block:
        await message.answer("❌ Не удалось получить номер блока BSC")
        return
    
    target_block = current_block + 20
    
    await publish_round_info(CHANNEL_ID, round_number, participants, target_block)
    await message.answer(f"✅ Информация о розыгрыше #{round_number} опубликована в канале")
    await message.answer(f"⏳ Розыгрыш состоится через 2 минуты (блок #{target_block})")
    
    await asyncio.sleep(120)
    
    winner = await execute_provable_draw_bsc(CHANNEL_ID, round_number, participants, target_block)
    
    if winner:
        cursor.execute("DELETE FROM participants")
        conn.commit()
        await message.answer(f"✅ Розыгрыш #{round_number} завершён! Победитель: {winner}")
    else:
        await message.answer(f"❌ Розыгрыш #{round_number} не удался. Участники сохранены.")

@dp.message_handler()
async def handle_txid(message: types.Message):
    txid = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    cursor.execute("SELECT * FROM transactions WHERE txid = ?", (txid,))
    if cursor.fetchone():
        await message.answer("❌ Этот TXID уже был использован")
        return
    
    wait_msg = await message.answer(
    "🔄 **Проверяю транзакцию...**\n"
    "⏱ Это может занять до 30-40 секунд из-за задержек API\n"
    "Пожалуйста, подожди...",
    parse_mode="Markdown"
)
    
    success, msg = check_bsc_payment(txid)
    
    if success:
        try:
            cursor.execute(
                "INSERT INTO participants (username) VALUES (?)", 
                (f"@{username}",)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        
        cursor.execute(
            "INSERT INTO transactions (txid, user_id, username, amount) VALUES (?, ?, ?, ?)",
            (txid, user_id, username, 5)
        )
        conn.commit()
        
        await message.answer(f"✅ Транзакция подтверждена!\nТы добавлен в розыгрыш 🎟")
    else:
        await message.answer(f"❌ Ошибка: {msg}")
    
    await wait_msg.delete()

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
import os
import logging
import random
import requests
import time
import asyncio
import hashlib
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import uvicorn
import psycopg2
from psycopg2.extras import RealDictCursor

draw_in_progress = False

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

# === ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ (SUPABASE) ===
DATABASE_URL = os.getenv("DATABASE_URL")
conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
conn.autocommit = True
cursor = conn.cursor()

# Таблица участников (с номерами билетов)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        id SERIAL PRIMARY KEY,
        ticket_number INTEGER UNIQUE,
        username TEXT UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

# Таблица транзакций
cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        txid TEXT PRIMARY KEY,
        user_id BIGINT,
        username TEXT,
        amount REAL,
        status TEXT DEFAULT 'confirmed',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

# Таблица для хранения истории розыгрышей
cursor.execute("""
    CREATE TABLE IF NOT EXISTS draw_history (
        id SERIAL PRIMARY KEY,
        round_number INTEGER,
        draw_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        participants_count INTEGER,
        total_bank REAL,
        winner_username TEXT,
        winner_ticket INTEGER,
        winner_prize REAL,
        commission REAL,
        target_block INTEGER,
        block_hash TEXT
    )
""")

# Таблица для отслеживания источников переходов
cursor.execute("""
    CREATE TABLE IF NOT EXISTS referral_sources (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        source TEXT,
        medium TEXT,
        campaign TEXT,
        invited_by BIGINT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

# === КЛАВИАТУРА ===
keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard.add(
    KeyboardButton("🎟 Участвовать"),
    KeyboardButton("💰 Банк"),
    KeyboardButton("👥 Участники")
)
keyboard.add(
    KeyboardButton("📊 Статистика"),
    KeyboardButton("📜 История"),
    KeyboardButton("📆 Неделя")
)

# === ФУНКЦИИ ПРОВЕРКИ ПЛАТЕЖЕЙ BSC ===
def check_bsc_payment(txid, expected_amount=5, expected_address=None):
    """Проверяет транзакцию USDT BEP-20 через BSCTrace API (MegaNode)"""
    if expected_address is None:
        expected_address = WALLET_ADDRESS
    
    for attempt in range(1, 4):
        try:
            time.sleep(10 * attempt)
            
            api_key = os.getenv("MEGANODE_API_KEY")
            url = f"https://bsc-mainnet.nodereal.io/v1/{api_key}"
            
            tx_payload = {
                "jsonrpc": "2.0",
                "method": "eth_getTransactionByHash",
                "params": [txid],
                "id": 1
            }
            tx_response = requests.post(url, json=tx_payload)
            
            if tx_response.status_code != 200:
                if attempt < 3:
                    continue
                return False, "Ошибка при обращении к BSCTrace"
            
            tx_data = tx_response.json()
            if 'result' not in tx_data or not tx_data['result']:
                if attempt < 3:
                    continue
                return False, "Транзакция не найдена"
            
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
            
            usdt_contract = "0x55d398326f99059ff775485246999027b3197955"
            found_transfer = False
            
            if 'logs' in receipt:
                for log in receipt['logs']:
                    if log['address'].lower() == usdt_contract.lower():
                        if len(log['topics']) >= 3 and log['topics'][0] == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef":
                            to_address_hex = log['topics'][2][2:]
                            if len(to_address_hex) > 40:
                                to_address_hex = to_address_hex[-40:]
                            to_address = '0x' + to_address_hex
                            
                            print(f"🔍 Найден Transfer: Получатель: {to_address}, Ожидаемый: {expected_address}")
                            
                            if to_address.lower() == expected_address.lower():
                                amount = int(log['data'], 16) / 10**18
                                if amount >= expected_amount:
                                    return True, f"OK: {amount} USDT"
                                else:
                                    return False, f"Недостаточно средств: {amount} USDT"
            
            if not found_transfer:
                return False, "Не найден перевод USDT в этой транзакции"
            
        except Exception as e:
            if attempt == 3:
                return False, f"Ошибка: {str(e)}"
            continue
    
    return False, "Не удалось проверить транзакцию после нескольких попыток"

# === ФУНКЦИИ ДЛЯ ПОЛУЧЕНИЯ БЛОКОВ BSC ===
def get_current_bsc_block():
    """Получает номер последнего блока BSC через MegaNode JSON-RPC"""
    try:
        api_key = os.getenv("MEGANODE_API_KEY")
        url = f"https://bsc-mainnet.nodereal.io/v1/{api_key}"
        
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1
        }
        
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if 'result' in data:
                block_number = int(data['result'], 16)
                logging.info(f"✅ Текущий блок BSC: {block_number}")
                return block_number
        else:
            logging.error(f"Ошибка HTTP: {response.status_code}")
            logging.error(f"Ответ: {response.text[:200]}")
            
    except Exception as e:
        logging.error(f"Ошибка получения блока BSC: {str(e)}")
    
    return None

def get_bsc_block_hash(block_number):
    """Получает хэш блока BSC по номеру через MegaNode JSON-RPC"""
    try:
        api_key = os.getenv("MEGANODE_API_KEY")
        url = f"https://bsc-mainnet.nodereal.io/v1/{api_key}"
        
        block_hex = hex(block_number)
        
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [block_hex, False],
            "id": 1
        }
        
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if 'result' in data and data['result']:
                block_hash = data['result']['hash']
                logging.info(f"✅ Хэш блока {block_number}: {block_hash[:32]}...")
                return block_hash
        else:
            logging.error(f"Ошибка HTTP: {response.status_code}")
            logging.error(f"Ответ: {response.text[:200]}")
            
    except Exception as e:
        logging.error(f"Ошибка получения хэша BSC: {str(e)}")
    
    return None

async def publish_round_info(chat_id, round_number, participants_with_tickets, target_block):
    """Публикует информацию о раунде перед розыгрышем"""
    tickets_text = "\n".join(participants_with_tickets[:20])
    if len(participants_with_tickets) > 20:
        tickets_text += f"\n... и ещё {len(participants_with_tickets) - 20}"
    
    message = (
        f"🎲 **РОЗЫГРЫШ #{round_number}**\n\n"
        f"🎟 **Всего билетов:** {len(participants_with_tickets)}\n\n"
        f"**Список участников:**\n{tickets_text}\n\n"
        f"🔐 **Прозрачный выбор победителя:**\n"
        f"1️⃣ Будет взят хэш блока BSC **#{target_block}**\n"
        f"2️⃣ Победитель = хэш % {len(participants_with_tickets)}\n"
        f"3️⃣ Результат появится здесь сразу после получения блока\n\n"
        f"⏳ Ожидайте розыгрыша..."
    )
    await bot.send_message(chat_id, message, parse_mode="Markdown")

# === ФУНКЦИИ ДЛЯ ПРОВЕДЕНИЯ РОЗЫГРЫША ===
async def execute_provable_draw_bsc(chat_id, round_number, participants_with_tickets, target_block):
    """Проводит provably fair розыгрыш на BSC и публикует красивый пост"""
    
    if hasattr(execute_provable_draw_bsc, f"completed_{round_number}"):
        return None
    
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
    winner_index = hash_int % len(participants_with_tickets)
    winner_line = participants_with_tickets[winner_index]
    
    # Извлекаем номер билета и username
    winner_parts = winner_line.split('. ')
    winner_ticket = int(winner_parts[0])
    winner_username = winner_parts[1] if len(winner_parts) > 1 else "Неизвестно"
    
    # Рассчитываем данные
    total_users = len(participants_with_tickets)
    bank = total_users * ENTRY_FEE
    commission = bank * 0.10
    winner_prize = bank - commission
    
    # Формируем красивый пост
    result = (
        f"🏆 **РОЗЫГРЫШ #{round_number} ЗАВЕРШЁН!** 🏆\n\n"
        f"📅 **Дата:** {time.strftime('%d.%m.%Y %H:%M')} (UTC)\n"
        f"🔗 **Блок BSC:** [#{target_block}](https://bscscan.com/block/{target_block})\n"
        f"🔐 **Хэш блока:**\n`{block_hash[:32]}...`\n\n"
        f"📊 **Детали розыгрыша:**\n"
        f"👥 Участников: **{total_users}**\n"
        f"💰 Общий банк: **{bank:.2f} USDT**\n"
        f"💸 Комиссия (10%): **{commission:.2f} USDT**\n"
        f"🎁 Приз победителю: **{winner_prize:.2f} USDT**\n\n"
        f"🧮 **Расчёт победителя:**\n"
        f"`{block_hash[:16]}...` (хэш) % {total_users} = **{winner_ticket}**\n\n"
        f"🎉 **Победитель: Билет №{winner_ticket} — {winner_username}**\n\n"
        f"🔍 **[Проверить на BscScan](https://bscscan.com/block/{target_block})**\n\n"
        f"Следующий розыгрыш уже скоро! 🚀"
    )
    
    # Отправляем в канал
    await bot.send_message(
        CHANNEL_ID, 
        result,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    
    return winner_username, winner_ticket, winner_prize

# === СТАТИСТИКА И ИСТОРИЯ ===
@dp.message_handler(commands=['stats'])
async def cmd_stats(message: types.Message):
    """Показывает общую статистику бота"""
    
    cursor.execute("SELECT COUNT(*) FROM draw_history")
    total_draws = cursor.fetchone()['count'] or 0
    
    cursor.execute("SELECT SUM(participants_count) FROM draw_history")
    total_participants = cursor.fetchone()['sum'] or 0
    
    cursor.execute("SELECT SUM(total_bank) FROM draw_history")
    total_bank_all = cursor.fetchone()['sum'] or 0
    
    cursor.execute("SELECT SUM(commission) FROM draw_history")
    total_commission = cursor.fetchone()['sum'] or 0
    
    cursor.execute("SELECT MAX(winner_prize) FROM draw_history")
    max_prize = cursor.fetchone()['max'] or 0
    
    cursor.execute("SELECT MAX(total_bank) FROM draw_history")
    max_bank = cursor.fetchone()['max'] or 0
    
    stats_text = (
        f"📊 **ОБЩАЯ СТАТИСТИКА**\n\n"
        f"🎲 Всего розыгрышей: **{total_draws}**\n"
        f"👥 Всего участников: **{total_participants}**\n"
        f"💰 Общий банк: **{total_bank_all:.2f} USDT**\n"
        f"💸 Твоя комиссия (10%): **{total_commission:.2f} USDT**\n\n"
        f"🏆 **Рекорды:**\n"
        f"• Самый крупный банк: **{max_bank:.2f} USDT**\n"
        f"• Самый крупный выигрыш: **{max_prize:.2f} USDT**"
    )
    
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message_handler(commands=['history'])
async def cmd_history(message: types.Message):
    """Показывает историю последних 10 розыгрышей"""
    
    cursor.execute("""
        SELECT round_number, draw_date, participants_count, total_bank, winner_username, winner_ticket, winner_prize 
        FROM draw_history 
        ORDER BY draw_date DESC 
        LIMIT 10
    """)
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 История розыгрышей пока пуста")
        return
    
    text = "📜 **ПОСЛЕДНИЕ РОЗЫГРЫШИ**\n\n"
    
    for row in rows:
        date_str = row['draw_date'].strftime("%d.%m.%Y %H:%M") if row['draw_date'] else "неизвестно"
        text += (
            f"🎲 **#{row['round_number']}** — {date_str}\n"
            f"👥 {row['participants_count']} уч. | 💰 {row['total_bank']:.2f} USDT\n"
            f"🏆 Билет №{row['winner_ticket']} — {row['winner_username']} — {row['winner_prize']:.2f} USDT\n\n"
        )
    
    await message.answer(text, parse_mode="Markdown")

@dp.message_handler(commands=['weekly'])
async def cmd_weekly(message: types.Message):
    """Показывает статистику за последние 7 дней"""
    
    cursor.execute("""
        SELECT COUNT(*) as draws, 
               SUM(participants_count) as participants,
               SUM(total_bank) as total_bank,
               SUM(commission) as total_commission,
               MAX(winner_prize) as max_prize
        FROM draw_history 
        WHERE draw_date > NOW() - INTERVAL '7 days'
    """)
    stats = cursor.fetchone()
    
    cursor.execute("""
        SELECT winner_username, COUNT(*) as wins
        FROM draw_history 
        WHERE draw_date > NOW() - INTERVAL '7 days'
        GROUP BY winner_username
        ORDER BY wins DESC
        LIMIT 1
    """)
    top_winner = cursor.fetchone()
    
    week_text = (
        f"📆 **СТАТИСТИКА ЗА НЕДЕЛЮ**\n\n"
        f"🎲 Розыгрышей: **{stats['draws'] or 0}**\n"
        f"👥 Участников: **{stats['participants'] or 0}**\n"
        f"💰 Общий банк: **{stats['total_bank'] or 0:.2f} USDT**\n"
        f"💸 Комиссия: **{stats['total_commission'] or 0:.2f} USDT**\n"
        f"🏆 Макс. выигрыш: **{stats['max_prize'] or 0:.2f} USDT**\n"
    )
    
    if top_winner:
        week_text += f"👑 Лучший игрок: {top_winner['winner_username']} ({top_winner['wins']} побед)\n"
    
    await bot.send_message(message.chat.id, week_text, parse_mode="Markdown")

# === РЕФЕРАЛЬНАЯ СИСТЕМА И АНАЛИТИКА ===
@dp.message_handler(commands=['gen_link'])
async def generate_referral_link(message: types.Message):
    """Генерирует ссылку для рекламного канала (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args().split()
    if len(args) < 2:
        await message.answer(
            "❌ Используй: /gen_link [название_канала] [название_кампании]\n"
            "Пример: /gen_link crypto_channel march_2026"
        )
        return
    
    channel_name = args[0]
    campaign = args[1]
    
    unique_code = hashlib.md5(f"{channel_name}_{campaign}_{time.time()}".encode()).hexdigest()[:8]
    
    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{channel_name}_{campaign}_{unique_code}"
    
    await message.answer(
        f"✅ **Ссылка для канала {channel_name}:**\n\n"
        f"`{ref_link}`\n\n"
        f"📊 **Параметры:**\n"
        f"• Канал: {channel_name}\n"
        f"• Кампания: {campaign}\n"
        f"• Код: {unique_code}\n\n"
        f"Отправь эту ссылку администратору канала для размещения.",
        parse_mode="Markdown"
    )

@dp.message_handler(commands=['sources'])
async def show_sources_stats(message: types.Message):
    """Показывает статистику по источникам переходов (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    cursor.execute("""
        SELECT 
            source, 
            COUNT(*) as users,
            COUNT(DISTINCT user_id) as unique_users,
            COUNT(CASE WHEN invited_by IS NOT NULL THEN 1 END) as referrals
        FROM referral_sources 
        GROUP BY source 
        ORDER BY users DESC
    """)
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("📭 Нет данных по источникам")
        return
    
    text = "📊 **СТАТИСТИКА ПО ИСТОЧНИКАМ**\n\n"
    
    for row in rows:
        cursor.execute("""
            SELECT COUNT(*) FROM participants p 
            JOIN referral_sources rs ON p.username = '@' || rs.user_id::text
            WHERE rs.source = %s
        """, (row['source'],))
        participants = cursor.fetchone()['count'] or 0
        conversion_rate = (participants / row['users']) * 100 if row['users'] > 0 else 0
        
        text += (
            f"📌 **{row['source']}**\n"
            f"   👥 Переходов: {row['users']}\n"
            f"   🎟 Участников: {participants}\n"
            f"   📈 Конверсия: {conversion_rate:.1f}%\n"
            f"   🔗 Рефералов: {row['referrals']}\n\n"
        )
    
    await message.answer(text, parse_mode="Markdown")

# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    args = message.get_args()
    source_info = {
        'source': 'direct',
        'medium': 'direct',
        'campaign': 'direct',
        'invited_by': None
    }
    
    if args and args.startswith('ref_'):
        parts = args.split('_')
        if len(parts) >= 4:
            source_info['source'] = parts[1]
            source_info['campaign'] = parts[2]
            source_info['medium'] = 'post'
            
            if len(parts) > 4 and parts[4].isdigit():
                source_info['invited_by'] = int(parts[4])
    
    cursor.execute("""
        INSERT INTO referral_sources 
        (user_id, source, medium, campaign, invited_by) 
        VALUES (%s, %s, %s, %s, %s)
    """, (
        message.from_user.id,
        source_info['source'],
        source_info['medium'],
        source_info['campaign'],
        source_info['invited_by']
    ))
    conn.commit()
    
    await message.answer(
        "🚀 Добро пожаловать в Crypto Fortuna Bot!\n"
        f"💰 Взнос: {ENTRY_FEE} USDT\n\n"
        "Выбери действие 👇",
        reply_markup=keyboard
    )

@dp.message_handler(lambda message: message.text == "🎟 Участвовать")
async def participate(message: types.Message):
    await message.answer(
        f"🔹 **Для участия переведи {ENTRY_FEE} USDT**\n"
        f"🔹 Сеть: **BSC (BEP-20)**\n\n"
        f"👇 **Адрес для перевода:**",
        parse_mode="Markdown"
    )
    
    await message.answer(
        f"`{WALLET_ADDRESS}`",
        parse_mode="Markdown"
    )
    
    await message.answer(
        "📤 **После оплаты отправь сюда TXID** (хэш транзакции)\n"
        "Он выглядит как длинный набор букв и цифр, начинается с 0x",
        parse_mode="Markdown"
    )

@dp.message_handler(lambda message: message.text == "💰 Банк")
async def bank(message: types.Message):
    cursor.execute("SELECT COUNT(*) FROM participants")
    result = cursor.fetchone()
    count = result['count'] if result else 0
    total_bank = count * ENTRY_FEE
    await message.answer(f"💰 Текущий банк: {total_bank} USDT")

@dp.message_handler(lambda message: message.text == "👥 Участники")
async def members(message: types.Message):
    cursor.execute("SELECT ticket_number, username FROM participants ORDER BY ticket_number")
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("👥 Пока нет участников. Ты можешь стать первым!")
        return
    
    count = len(rows)
    text = f"👥 **Всего участников: {count}**\n\n"
    text += "**Текущие билеты:**\n"
    
    for row in rows[:20]:
        text += f"#{row['ticket_number']} — {row['username']}\n"
    
    if count > 20:
        text += f"\n... и ещё {count - 20}"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message_handler(lambda message: message.text == "📊 Статистика")
async def stats_button(message: types.Message):
    await cmd_stats(message)

@dp.message_handler(lambda message: message.text == "📜 История")
async def history_button(message: types.Message):
    await cmd_history(message)

@dp.message_handler(lambda message: message.text == "📆 Неделя")
async def week_button(message: types.Message):
    await cmd_weekly(message)
    
@dp.message_handler(commands=['add'])
async def add_participant(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    username = message.get_args()
    if not username:
        await message.answer("Используй: /add @username")
        return
    
    cursor.execute("SELECT COALESCE(MAX(ticket_number), 0) + 1 FROM participants")
    next_number = cursor.fetchone()['coalesce']
    
    try:
        cursor.execute(
            "INSERT INTO participants (ticket_number, username) VALUES (%s, %s)", 
            (next_number, username)
        )
        conn.commit()
        await message.answer(f"✅ Участник {username} добавлен! Билет №{next_number}")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        await message.answer("⚠️ Этот участник уже добавлен")

@dp.message_handler(commands=['reset_db'])
async def cmd_reset_db(message: types.Message):
    """Сброс базы данных (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    cursor.execute("DELETE FROM participants")
    cursor.execute("DELETE FROM transactions")
    cursor.execute("DELETE FROM draw_history")
    cursor.execute("DELETE FROM referral_sources")
    conn.commit()
    await message.answer("✅ База данных очищена! Все TXID теперь будут считаться новыми.")

@dp.message_handler(commands=['find_txid'])
async def cmd_find_txid(message: types.Message):
    """Поиск TXID в базе данных (можно указать TXID или посмотреть последние)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.get_args()
    
    if args:
        search_txid = args.strip().lower()
        
        cursor.execute("SELECT * FROM transactions WHERE txid = %s", (search_txid,))
        result = cursor.fetchone()
        
        if not result and search_txid.startswith('0x'):
            search_txid_no_prefix = search_txid[2:]
            cursor.execute("SELECT * FROM transactions WHERE txid LIKE %s", (f'%{search_txid_no_prefix}%',))
            result = cursor.fetchone()
        
        if not result:
            short_txid = search_txid[-20:] if len(search_txid) > 20 else search_txid
            cursor.execute("SELECT * FROM transactions WHERE txid LIKE %s", (f'%{short_txid}%',))
            result = cursor.fetchone()
        
        if result:
            await message.answer(
                f"✅ TXID **НАЙДЕН** в базе!\n\nЗапись: {result}",
                parse_mode="Markdown"
            )
        else:
            await message.answer(
                f"❌ TXID **НЕ НАЙДЕН** в базе ни по одному из форматов.\n"
                f"Искали: {search_txid[:20]}...{search_txid[-10:]}",
                parse_mode="Markdown"
            )
    
    cursor.execute("SELECT txid, username, created_at FROM transactions ORDER BY created_at DESC LIMIT 10")
    rows = cursor.fetchall()
    
    if rows:
        text = "📋 **Последние 10 TXID в базе:**\n\n"
        for row in rows:
            short_tx = row['txid'][:15] + "..." + row['txid'][-10:]
            text += f"• `{short_tx}` — {row['username']} — {row['created_at']}\n"
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer("📭 База транзакций пуста.")

@dp.message_handler(commands=['announce'])
async def cmd_announce(message: types.Message):
    """Публикует красивый пост-анонс о начале розыгрыша (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    cursor.execute("SELECT COUNT(*) FROM participants")
    count = cursor.fetchone()['count'] or 0
    current_bank = count * ENTRY_FEE
    
    cursor.execute("""
        SELECT winner_username, winner_prize, winner_ticket FROM draw_history 
        ORDER BY draw_date DESC LIMIT 1
    """)
    last_winner = cursor.fetchone()
    
    last_winner_text = f"@{last_winner['winner_username']}" if last_winner else "пока нет"
    last_ticket_text = f"№{last_winner['winner_ticket']}" if last_winner else ""
    last_prize_text = f"{last_winner['winner_prize']:.2f}" if last_winner else "0"
    
    post = (
        f"🎲 **CRYPTO FORTUNA — НОВЫЙ РОЗЫГРЫШ!** 🎲\n\n"
        f"💰 **Банк уже собран:** {current_bank} USDT\n"
        f"👥 **Участников:** {count}\n"
        f"🎟 **Взнос:** {ENTRY_FEE} USDT (BSC)\n\n"
        f"🔐 **Почему нам можно верить:**\n"
        f"• Победитель определяется хэшем блока BSC (проверяемо!)\n"
        f"• Все транзакции публичны\n"
        f"• История розыгрышей открыта\n\n"
        f"🚀 **Как участвовать:**\n"
        f"1️⃣ Перейди в бота: @RealCryptoFortunaBot\n"
        f"2️⃣ Нажми «Участвовать»\n"
        f"3️⃣ Отправь {ENTRY_FEE} USDT на указанный адрес\n"
        f"4️⃣ Отправь TXID боту — и ты в игре!\n\n"
        f"⏳ **Розыгрыш состоится:** через 24 часа\n"
        f"🏆 **Предыдущий победитель:** {last_winner_text} (билет {last_ticket_text}) — {last_prize_text} USDT\n\n"
        f"Не упусти свой шанс! Удача любит смелых 🔥"
    )
    
    await bot.send_message(CHANNEL_ID, post, parse_mode="Markdown")
    await message.answer("✅ Пост-анонс опубликован в канале!")

@dp.message_handler(commands=['start_draw'])
async def cmd_start_draw(message: types.Message):
    global draw_in_progress
    
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Эта команда только для админа")
        return
    
    if draw_in_progress:
        await message.answer("⚠️ **Розыгрыш уже запущен!** Подождите завершения.")
        return
    
    cursor.execute("SELECT ticket_number, username FROM participants ORDER BY ticket_number")
    rows = cursor.fetchall()
    participants_with_tickets = [f"{row['ticket_number']}. {row['username']}" for row in rows]
    
    if len(participants_with_tickets) < 2:
        await message.answer("❌ Для розыгрыша нужно минимум 2 участника")
        return
    
    draw_in_progress = True
    
    try:
        round_number = random.randint(1000, 9999)
        
        current_block = get_current_bsc_block()
        if not current_block:
            await message.answer("❌ Не удалось получить номер блока BSC")
            draw_in_progress = False
            return
        
        target_block = current_block + 20
        
        await publish_round_info(CHANNEL_ID, round_number, participants_with_tickets, target_block)
        await message.answer(f"✅ Информация о розыгрыше #{round_number} опубликована в канале")
        await message.answer(f"⏳ Розыгрыш состоится через 2 минуты (блок #{target_block})")
        
        await asyncio.sleep(120)
        
        result = await execute_provable_draw_bsc(CHANNEL_ID, round_number, participants_with_tickets, target_block)
        
        if result:
            winner_username, winner_ticket, winner_prize = result
            
            cursor.execute("""
                INSERT INTO draw_history 
                (round_number, participants_count, total_bank, winner_username, winner_ticket, winner_prize, commission, target_block, block_hash) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                round_number, 
                len(participants_with_tickets), 
                len(participants_with_tickets) * ENTRY_FEE,
                winner_username,
                winner_ticket,
                winner_prize,
                len(participants_with_tickets) * ENTRY_FEE * 0.1,
                target_block,
                "saved_in_post"
            ))
            
            cursor.execute("DELETE FROM participants")
            conn.commit()
            await message.answer(f"✅ Розыгрыш #{round_number} завершён! Победитель: билет {winner_ticket} — {winner_username}")
        else:
            await message.answer(f"❌ Розыгрыш #{round_number} не удался. Участники сохранены.")
    
    finally:
        draw_in_progress = False

@dp.message_handler()
async def handle_txid(message: types.Message):
    if message.text.startswith('/'):
        return
    
    button_texts = ["🎟 Участвовать", "💰 Банк", "👥 Участники", "📊 Статистика", "📜 История", "📆 Неделя"]
    if message.text in button_texts:
        return
    
    if len(message.text) < 60 or len(message.text) > 70:
        await message.answer("❌ Это не похоже на TXID. Отправь хэш транзакции (64-66 символов).")
        return
    
    txid = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    cursor.execute("SELECT * FROM transactions WHERE txid = %s", (txid,))
    if cursor.fetchone():
        await message.answer("❌ Этот TXID уже был использован")
        return

    cursor.execute("SELECT * FROM participants WHERE username = %s", (f"@{username}",))
    if cursor.fetchone():
        await message.answer("❌ Вы уже участвуете в текущем розыгрыше")
        return
    
    wait_msg = await message.answer(
        "🔄 **Проверяю транзакцию...**\n"
        "⏱ Это может занять до 30-40 секунд из-за задержек API\n"
        "Пожалуйста, подожди...",
        parse_mode="Markdown"
    )
    
    success, msg = check_bsc_payment(txid)
    
    if success:
        cursor.execute("SELECT COALESCE(MAX(ticket_number), 0) + 1 FROM participants")
        next_number = cursor.fetchone()['coalesce']
        
        cursor.execute(
            "INSERT INTO transactions (txid, user_id, username, amount) VALUES (%s, %s, %s, %s) ON CONFLICT (txid) DO NOTHING",
            (txid, user_id, username, 5)
        )
        
        try:
            cursor.execute(
                "INSERT INTO participants (ticket_number, username) VALUES (%s, %s)", 
                (next_number, f"@{username}")
            )
            conn.commit()
            
            await message.answer(
                f"✅ **Транзакция подтверждена!**\n"
                f"🎟 **Твой номер билета: {next_number}**\n"
                f"Ты добавлен в розыгрыш. Удачи! 🍀",
                parse_mode="Markdown"
            )
        except psycopg2.errors.UniqueViolation:
            conn.commit()
            await message.answer("⚠️ Вы уже участвуете в этом розыгрыше")
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
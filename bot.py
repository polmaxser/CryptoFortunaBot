#!/usr/bin/env python3
"""
Crypto Fortuna Club Bot  v2.1
══════════════════════════════════════════════════════════════════
FIX LIST vs v2.0:
  1. Webhook returns 200 IMMEDIATELY (fire-and-forget) → fixes infinite retry loop
  2. drop_pending_updates=True on startup → clears stale queue on restart
  3. ParseMode.MARKDOWN_V2  (was MARKDOWN — all strings are MarkdownV2 formatted)
  4. Per-user verification semaphore → one active check per user at a time
  5. /cancel command → user can abort a stuck verification
  6. escape_md() helper for raw strings sent outside t()
  7. asyncio.Lock created inside startup (avoids deprecation warning)
  8. handle_txid: guard against None text / non-text messages
  9. Admin decorator fixed for aiogram 3
  10. draw_lock released properly on any exception inside run_full_draw
  11. Referral insert uses ON CONFLICT DO NOTHING (no crash on repeat /start)
  12. random imported at top level
══════════════════════════════════════════════════════════════════
"""

import os
import re
import random
import logging
import asyncio
import hashlib
import time
from datetime import datetime, timezone
from functools import wraps

import aiohttp
import asyncpg
import uvicorn
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, Message
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

# ══════════════════════════════════════════════════════════════
#  CONFIG  —  all secrets via env vars
# ══════════════════════════════════════════════════════════════
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
DATABASE_URL      = os.getenv("DATABASE_URL", "")
MEGANODE_KEY      = os.getenv("MEGANODE_API_KEY", "")
WALLET_ADDRESS    = os.getenv("WALLET_ADDRESS", "0xFd434c30aCeF2815fE895a2144b11122e31c0B93")
ADMIN_ID          = int(os.getenv("ADMIN_ID", "8333494757"))
CHANNEL_ID        = os.getenv("CHANNEL_ID", "@realcryptofortuna")
ENTRY_FEE         = int(os.getenv("ENTRY_FEE", "5"))
PARTICIPANT_LIMIT = int(os.getenv("PARTICIPANT_LIMIT", "100"))
RENDER_URL        = os.getenv("RENDER_EXTERNAL_URL", "")
USDT_CONTRACT     = "0x55d398326f99059ff775485246999027b3197955"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("fortuna")

# ══════════════════════════════════════════════════════════════
#  GLOBALS  (draw_lock created in on_startup to avoid deprecation)
# ══════════════════════════════════════════════════════════════
db_pool: asyncpg.Pool | None = None
draw_lock: asyncio.Lock | None = None
_lang_cache: dict[int, str] = {}

# Per-user active verification tasks.
# Prevents duplicate verifications and allows /cancel.
_pending: dict[int, asyncio.Task] = {}

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
)
dp = Dispatcher(storage=MemoryStorage())


# ══════════════════════════════════════════════════════════════
#  MARKDOWN V2 HELPER
# ══════════════════════════════════════════════════════════════
_MD2_SPECIAL = re.compile(r'([_*\[\]()~`>#+=|{}.!\\-\\])')

def esc(text: str) -> str:
    """Escape a plain string for use inside a MarkdownV2 message."""
    return _MD2_SPECIAL.sub(r'\\\1', str(text))


# ══════════════════════════════════════════════════════════════
#  TRANSLATIONS
# ══════════════════════════════════════════════════════════════
T: dict[str, dict[str, str]] = {
    # ── Keyboard buttons ──────────────────────────────────────
    "btn_participate": {"en": "🎟 Participate",  "ru": "🎟 Участвовать"},
    "btn_bank":        {"en": "💰 Pool",          "ru": "💰 Банк"},
    "btn_members":     {"en": "👥 Members",       "ru": "👥 Участники"},
    "btn_stats":       {"en": "📊 Statistics",    "ru": "📊 Статистика"},
    "btn_history":     {"en": "📜 History",       "ru": "📜 История"},
    "btn_week":        {"en": "📆 This Week",     "ru": "📆 Неделя"},
    "btn_language":    {"en": "🌐 Русский",        "ru": "🌐 English"},

    # ── /start ────────────────────────────────────────────────
    "welcome": {
        "en": (
            "🏛 Welcome to *Crypto Fortuna Club*\\!\n\n"
            "A private community where fund distribution is *100% transparent* "
            "and verifiable on the BSC blockchain\\.\n\n"
            "📢 Subscribe to our channel: {channel}\n\n"
            "💎 Voluntary contribution: *{fee} USDT* \\(BSC BEP\\-20\\)\n"
            "🔐 Winner selected by open BSC block hash algorithm\n"
            "🏆 90% of the pool goes to the winner\n\n"
            "Choose an action below 👇"
        ),
        "ru": (
            "🏛 Добро пожаловать в *Crypto Fortuna Club*\\!\n\n"
            "Частное сообщество, где распределение фонда *100% прозрачно* "
            "и проверяемо в блокчейне BSC\\.\n\n"
            "📢 Подпишись на наш канал: {channel}\n\n"
            "💎 Добровольный взнос: *{fee} USDT* \\(BSC BEP\\-20\\)\n"
            "🔐 Победитель определяется открытым алгоритмом хэша блока BSC\n"
            "🏆 90% банка достаётся победителю\n\n"
            "Выбери действие ниже 👇"
        ),
    },

    # ── Participate ───────────────────────────────────────────
    "participate_info": {
        "en": (
            "🎟 *How to join the current round:*\n\n"
            "1️⃣ Send *{fee} USDT* \\(BEP\\-20, BSC network\\) to:\n\n"
            "`{wallet}`\n\n"
            "2️⃣ Copy your *transaction hash \\(TXID\\)*\n"
            "3️⃣ Paste it here — the bot verifies it automatically\n\n"
            "⚠️ *Important:* USDT BEP\\-20 on BSC only\\. "
            "Other networks will not be detected\\.\n\n"
            "👥 Current participants: *{count}/{limit}*"
        ),
        "ru": (
            "🎟 *Как участвовать в текущем раунде:*\n\n"
            "1️⃣ Отправь *{fee} USDT* \\(BEP\\-20, сеть BSC\\) на адрес:\n\n"
            "`{wallet}`\n\n"
            "2️⃣ Скопируй *хэш транзакции \\(TXID\\)*\n"
            "3️⃣ Вставь его сюда — бот проверит автоматически\n\n"
            "⚠️ *Важно:* Только USDT BEP\\-20 в сети BSC\\. "
            "Другие сети не будут обнаружены\\.\n\n"
            "👥 Текущих участников: *{count}/{limit}*"
        ),
    },

    # ── Bank / Pool ───────────────────────────────────────────
    "bank_info": {
        "en": (
            "💰 *Current Round Pool*\n\n"
            "👥 Participants: *{count}/{limit}*\n"
            "💵 Total pool: *{bank} USDT*\n"
            "🏆 Winner's reward \\(90%\\): *{prize:.2f} USDT*\n"
            "📋 Open slots: *{slots}*"
        ),
        "ru": (
            "💰 *Банк текущего раунда*\n\n"
            "👥 Участников: *{count}/{limit}*\n"
            "💵 Общий банк: *{bank} USDT*\n"
            "🏆 Приз победителю \\(90%\\): *{prize:.2f} USDT*\n"
            "📋 Свободных мест: *{slots}*"
        ),
    },

    # ── Members list ──────────────────────────────────────────
    "members_header":  {"en": "👥 *Participants — {count} total*\n\n",
                        "ru": "👥 *Участники — {count} всего*\n\n"},
    "members_empty":   {"en": "👥 No participants yet\\. You could be first\\!",
                        "ru": "👥 Пока нет участников\\. Ты можешь стать первым\\!"},
    "members_more":    {"en": "\n_\\.\\.\\. and {n} more_",
                        "ru": "\n_\\.\\.\\. и ещё {n}_"},

    # ── TXID processing ───────────────────────────────────────
    "txid_invalid": {
        "en": "❌ That doesn't look like a valid TXID\\. Please send a transaction hash \\(64\\-66 chars, starting with `0x`\\)\\.",
        "ru": "❌ Это не похоже на TXID\\. Отправь хэш транзакции \\(64\\-66 символов, начинается с `0x`\\)\\.",
    },
    "txid_checking": {
        "en": (
            "🔄 *Verifying your transaction\\.\\.\\.*\n\n"
            "This can take up to 60 seconds\\. I'll notify you when done\\.\n"
            "To cancel: /cancel"
        ),
        "ru": (
            "🔄 *Проверяю транзакцию\\.\\.\\.*\n\n"
            "Это может занять до 60 секунд\\. Сообщу о результате\\.\n"
            "Для отмены: /cancel"
        ),
    },
    "txid_duplicate": {
        "en": "❌ This TXID has already been used\\.",
        "ru": "❌ Этот TXID уже был использован\\.",
    },
    "txid_already_in": {
        "en": "❌ You are already in the current round\\.",
        "ru": "❌ Вы уже участвуете в текущем розыгрыше\\.",
    },
    "txid_already_verifying": {
        "en": "⏳ You already have a verification in progress\\. Please wait, or use /cancel to abort it\\.",
        "ru": "⏳ У вас уже идёт проверка транзакции\\. Подождите или используйте /cancel для отмены\\.",
    },
    "txid_cancelled": {
        "en": "🚫 Transaction verification cancelled\\.",
        "ru": "🚫 Проверка транзакции отменена\\.",
    },
    "txid_nothing_to_cancel": {
        "en": "ℹ️ Nothing to cancel — no active verification\\.",
        "ru": "ℹ️ Нечего отменять — нет активной проверки\\.",
    },
    "txid_success": {
        "en": (
            "✅ *Transaction confirmed\\!*\n\n"
            "🎟 Your ticket number: *\\#{ticket}*\n"
            "💰 Pool: *{bank} USDT* \\({count} participants\\)\n\n"
            "Good luck\\! 🍀"
        ),
        "ru": (
            "✅ *Транзакция подтверждена\\!*\n\n"
            "🎟 Твой номер билета: *\\#{ticket}*\n"
            "💰 Банк: *{bank} USDT* \\({count} участников\\)\n\n"
            "Удачи\\! 🍀"
        ),
    },
    "txid_error": {
        "en": (
            "❌ *Verification failed:* {msg}\n\n"
            "Make sure you sent *{fee} USDT* \\(BEP\\-20\\) to the correct address\\.\n"
            "Press 🎟 Participate to see the address again\\."
        ),
        "ru": (
            "❌ *Ошибка проверки:* {msg}\n\n"
            "Убедись, что отправил *{fee} USDT* \\(BEP\\-20\\) на правильный адрес\\.\n"
            "Нажми 🎟 Участвовать, чтобы увидеть адрес ещё раз\\."
        ),
    },
    "round_full": {
        "en": "⚠️ Round is full \\({limit} participants\\)\\. Please wait for the next round\\!",
        "ru": "⚠️ Раунд заполнен \\({limit} участников\\)\\. Ожидай следующего\\!",
    },

    # ── Draw ──────────────────────────────────────────────────
    "draw_announce": {
        "en": (
            "🎲 *DRAW \\#{round}*\n\n"
            "🎟 Total tickets: *{count}*\n\n"
            "*Participants:*\n{tickets}\n\n"
            "🔐 *How the winner is selected:*\n"
            "1️⃣ BSC block *\\#{block}* hash will be used\n"
            "2️⃣ `hash % {count}` = winner ticket index\n"
            "3️⃣ Result published right after the block is mined\n\n"
            "⏳ Drawing in \\~2 minutes\\.\\.\\."
        ),
        "ru": (
            "🎲 *РОЗЫГРЫШ \\#{round}*\n\n"
            "🎟 Всего билетов: *{count}*\n\n"
            "*Список участников:*\n{tickets}\n\n"
            "🔐 *Как определяется победитель:*\n"
            "1️⃣ Будет взят хэш блока BSC *\\#{block}*\n"
            "2️⃣ `хэш % {count}` = индекс билета победителя\n"
            "3️⃣ Результат публикуется сразу после получения блока\n\n"
            "⏳ Розыгрыш через \\~2 минуты\\.\\.\\."
        ),
    },
    "draw_fetching":   {"en": "⏳ *Fetching BSC block hash\\.\\.\\.*",
                        "ru": "⏳ *Получаю хэш блока BSC\\.\\.\\.*"},
    "draw_fetching_n": {"en": "⏳ *Fetching BSC block hash\\.\\.\\.* \\(attempt {n}/6\\)",
                        "ru": "⏳ *Получаю хэш блока BSC\\.\\.\\.* \\(попытка {n}/6\\)"},
    "draw_hash_failed":{"en": "❌ Could not retrieve block hash\\. Participants preserved\\. Use `/start_draw` again\\.",
                        "ru": "❌ Не удалось получить хэш блока\\. Участники сохранены\\. Повторите `/start_draw`\\."},
    "draw_result": {
        "en": (
            "🏆 *DRAW \\#{round} COMPLETE\\!* 🏆\n\n"
            "📅 *Date:* {date} \\(UTC\\)\n"
            "🔗 *BSC Block:* [\\#{block}](https://bscscan.com/block/{block})\n"
            "🔐 *Block Hash:* `{hash_short}\\.\\.\\.`\n\n"
            "📊 *Round Details:*\n"
            "👥 Participants: *{count}*\n"
            "💰 Total pool: *{bank:.2f} USDT*\n"
            "💸 Commission \\(10%\\): *{commission:.2f} USDT*\n"
            "🎁 Winner's prize: *{prize:.2f} USDT*\n\n"
            "🧮 *Calculation:*\n"
            "`{hash_short}\\.\\.\\.` % {count} = ticket *\\#{ticket}*\n\n"
            "🎉 *Winner: Ticket \\#{ticket} — {winner}*\n\n"
            "🔍 [Verify on BscScan](https://bscscan.com/block/{block})\n\n"
            "Next round starting soon\\! 🚀"
        ),
        "ru": (
            "🏆 *РОЗЫГРЫШ \\#{round} ЗАВЕРШЁН\\!* 🏆\n\n"
            "📅 *Дата:* {date} \\(UTC\\)\n"
            "🔗 *Блок BSC:* [\\#{block}](https://bscscan.com/block/{block})\n"
            "🔐 *Хэш блока:* `{hash_short}\\.\\.\\.`\n\n"
            "📊 *Детали раунда:*\n"
            "👥 Участников: *{count}*\n"
            "💰 Общий банк: *{bank:.2f} USDT*\n"
            "💸 Комиссия \\(10%\\): *{commission:.2f} USDT*\n"
            "🎁 Приз победителю: *{prize:.2f} USDT*\n\n"
            "🧮 *Расчёт:*\n"
            "`{hash_short}\\.\\.\\.` % {count} = билет *\\#{ticket}*\n\n"
            "🎉 *Победитель: Билет \\#{ticket} — {winner}*\n\n"
            "🔍 [Проверить на BscScan](https://bscscan.com/block/{block})\n\n"
            "Следующий раунд уже скоро\\! 🚀"
        ),
    },

    # ── Statistics ────────────────────────────────────────────
    "stats": {
        "en": (
            "📊 *Overall Statistics*\n\n"
            "🎲 Total draws: *{draws}*\n"
            "👥 Total participants: *{participants}*\n"
            "💰 Total volume: *{bank:.2f} USDT*\n"
            "💸 Total commission: *{commission:.2f} USDT*\n\n"
            "🏆 *Records:*\n"
            "• Largest pool: *{max_bank:.2f} USDT*\n"
            "• Largest prize: *{max_prize:.2f} USDT*"
        ),
        "ru": (
            "📊 *Общая статистика*\n\n"
            "🎲 Всего розыгрышей: *{draws}*\n"
            "👥 Всего участников: *{participants}*\n"
            "💰 Общий объём: *{bank:.2f} USDT*\n"
            "💸 Общая комиссия: *{commission:.2f} USDT*\n\n"
            "🏆 *Рекорды:*\n"
            "• Самый крупный банк: *{max_bank:.2f} USDT*\n"
            "• Самый крупный выигрыш: *{max_prize:.2f} USDT*"
        ),
    },
    "stats_empty": {"en": "📭 No draws have taken place yet\\.",
                    "ru": "📭 Розыгрышей пока не было\\."},

    # ── History ───────────────────────────────────────────────
    "history_header": {"en": "📜 *Last 10 Draws*\n\n",
                       "ru": "📜 *Последние 10 розыгрышей*\n\n"},
    "history_empty":  {"en": "📭 Draw history is empty\\.",
                       "ru": "📭 История розыгрышей пока пуста\\."},
    "history_row": {
        "en": "🎲 *\\#{round}* — {date}\n👥 {count} members \\| 💰 {bank:.2f} USDT\n🏆 Ticket \\#{ticket} — {winner} — {prize:.2f} USDT\n\n",
        "ru": "🎲 *\\#{round}* — {date}\n👥 {count} уч\\. \\| 💰 {bank:.2f} USDT\n🏆 Билет \\#{ticket} — {winner} — {prize:.2f} USDT\n\n",
    },

    # ── Weekly ────────────────────────────────────────────────
    "weekly": {
        "en": (
            "📆 *This Week*\n\n"
            "🎲 Draws: *{draws}*\n"
            "👥 Participants: *{participants}*\n"
            "💰 Total pool: *{bank:.2f} USDT*\n"
            "💸 Commission: *{commission:.2f} USDT*\n"
            "🏆 Biggest prize: *{max_prize:.2f} USDT*"
        ),
        "ru": (
            "📆 *За эту неделю*\n\n"
            "🎲 Розыгрышей: *{draws}*\n"
            "👥 Участников: *{participants}*\n"
            "💰 Общий банк: *{bank:.2f} USDT*\n"
            "💸 Комиссия: *{commission:.2f} USDT*\n"
            "🏆 Макс\\. выигрыш: *{max_prize:.2f} USDT*"
        ),
    },
    "weekly_top":   {"en": "\n👑 Top winner: {winner} \\({wins} win\\(s\\)\\)",
                     "ru": "\n👑 Лучший игрок: {winner} \\({wins} побед\\)"},
    "weekly_empty": {"en": "\n📭 No draws this week yet\\.",
                     "ru": "\n📭 Розыгрышей за эту неделю ещё не было\\."},

    # ── Language ──────────────────────────────────────────────
    "lang_now_en": {"en": "🌐 Language set to *English*\\.",
                    "ru": "🌐 Language set to *English*\\."},
    "lang_now_ru": {"en": "🌐 Язык изменён на *Русский*\\.",
                    "ru": "🌐 Язык изменён на *Русский*\\."},

    # ── Announce post ─────────────────────────────────────────
    "announce_post": {
        "en": (
            "🎲 *CRYPTO FORTUNA — NEW ROUND\\!* 🎲\n\n"
            "💰 *Current pool:* {bank} USDT\n"
            "👥 *Participants:* {count}/{limit}\n"
            "🎟 *Entry:* {fee} USDT \\(BSC BEP\\-20\\)\n\n"
            "🔐 *Why you can trust us:*\n"
            "• Winner picked by BSC block hash — publicly verifiable\\!\n"
            "• All transactions visible on BSCScan\n"
            "• Full draw history is open\n\n"
            "🚀 *How to join:*\n"
            "1️⃣ Open the bot: @RealCryptoFortunaBot\n"
            "2️⃣ Press «Participate»\n"
            "3️⃣ Send {fee} USDT to the shown address\n"
            "4️⃣ Send the TXID to the bot — you're in\\!\n\n"
            "🏆 *Last winner:* {last_winner} \\(ticket {last_ticket}\\) — {last_prize} USDT\n\n"
            "Fortune favours the bold 🔥"
        ),
        "ru": (
            "🎲 *CRYPTO FORTUNA — НОВЫЙ РОЗЫГРЫШ\\!* 🎲\n\n"
            "💰 *Банк:* {bank} USDT\n"
            "👥 *Участников:* {count}/{limit}\n"
            "🎟 *Взнос:* {fee} USDT \\(BSC BEP\\-20\\)\n\n"
            "🔐 *Почему нам доверяют:*\n"
            "• Победитель определяется хэшем блока BSC — проверяемо\\!\n"
            "• Все транзакции видны на BSCScan\n"
            "• История розыгрышей открыта\n\n"
            "🚀 *Как участвовать:*\n"
            "1️⃣ Открой бота: @RealCryptoFortunaBot\n"
            "2️⃣ Нажми «Участвовать»\n"
            "3️⃣ Отправь {fee} USDT на указанный адрес\n"
            "4️⃣ Отправь TXID боту — и ты в игре\\!\n\n"
            "🏆 *Предыдущий победитель:* {last_winner} \\(билет {last_ticket}\\) — {last_prize} USDT\n\n"
            "Удача любит смелых 🔥"
        ),
    },
    "no_winner_yet":   {"en": "none yet",                "ru": "пока нет"},
    "admin_published": {"en": "✅ Published to channel\\!", "ru": "✅ Опубликовано в канале\\!"},
    "admin_reset":     {"en": "✅ Database cleared\\.",     "ru": "✅ База данных очищена\\."},
    "sources_empty":   {"en": "📭 No referral data yet\\.", "ru": "📭 Данных по источникам пока нет\\."},
    "sources_header":  {"en": "📊 *Traffic Sources*\n\n", "ru": "📊 *Источники трафика*\n\n"},
    "gen_link_ok": {
        "en": "✅ *Link for {channel}:*\n\n`{link}`\n\n• Campaign: `{campaign}`\n• Code: `{code}`",
        "ru": "✅ *Ссылка для {channel}:*\n\n`{link}`\n\n• Кампания: `{campaign}`\n• Код: `{code}`",
    },
}


def t(key: str, lang: str = "en", **kwargs) -> str:
    entry = T.get(key, {})
    text = entry.get(lang) or entry.get("en") or key
    return text.format(**kwargs) if kwargs else text


# ══════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id  BIGINT PRIMARY KEY,
    username     TEXT,
    lang         TEXT        DEFAULT 'en',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS participants (
    id            SERIAL PRIMARY KEY,
    ticket_number INTEGER     NOT NULL,
    telegram_id   BIGINT      NOT NULL,
    username      TEXT        NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (telegram_id),
    UNIQUE (ticket_number)
);

CREATE TABLE IF NOT EXISTS transactions (
    txid       TEXT PRIMARY KEY,
    user_id    BIGINT  NOT NULL,
    username   TEXT    NOT NULL,
    amount     REAL    NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS draw_history (
    id                 SERIAL PRIMARY KEY,
    round_number       INTEGER     NOT NULL,
    draw_date          TIMESTAMPTZ DEFAULT NOW(),
    participants_count INTEGER     NOT NULL,
    total_bank         REAL        NOT NULL,
    winner_username    TEXT        NOT NULL,
    winner_ticket      INTEGER     NOT NULL,
    winner_prize       REAL        NOT NULL,
    commission         REAL        NOT NULL,
    target_block       BIGINT      NOT NULL,
    block_hash         TEXT        NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_sources (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT  NOT NULL,
    source     TEXT    DEFAULT 'direct',
    medium     TEXT    DEFAULT 'direct',
    campaign   TEXT    DEFAULT 'direct',
    invited_by BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""


async def init_db() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
        server_settings={"application_name": "fortuna_bot"},
    )
    async with db_pool.acquire() as conn:
        await conn.execute(SCHEMA)
    log.info("✅ DB pool ready")


async def keep_db_alive() -> None:
    """Ping DB every 30 min so Supabase free tier doesn't sleep."""
    while True:
        await asyncio.sleep(1800)
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            log.info("✅ DB keep-alive OK")
        except Exception as exc:
            log.error("❌ DB keep-alive failed: %s", exc)


# ── User helpers ───────────────────────────────────────────────
async def get_lang(uid: int) -> str:
    if uid in _lang_cache:
        return _lang_cache[uid]
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT lang FROM users WHERE telegram_id = $1", uid)
    lang = row["lang"] if row else "en"
    _lang_cache[uid] = lang
    return lang


async def set_lang(uid: int, lang: str) -> None:
    _lang_cache[uid] = lang
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, lang) VALUES ($1,$2) "
            "ON CONFLICT (telegram_id) DO UPDATE SET lang=$2",
            uid, lang,
        )


async def upsert_user(uid: int, username: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (telegram_id, username) VALUES ($1,$2) "
            "ON CONFLICT (telegram_id) DO UPDATE SET username=$2",
            uid, username,
        )


# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════
def make_keyboard(lang: str) -> ReplyKeyboardMarkup:
    rows = [
        [t("btn_participate", lang), t("btn_bank", lang), t("btn_members", lang)],
        [t("btn_stats", lang),       t("btn_history", lang), t("btn_week", lang)],
        [t("btn_language", lang)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=c) for c in row] for row in rows],
        resize_keyboard=True,
    )


ALL_BUTTON_TEXTS: set[str] = {
    v for key in T if key.startswith("btn_") for v in T[key].values()
}


# ══════════════════════════════════════════════════════════════
#  BSC API  — fully async, non-blocking
# ══════════════════════════════════════════════════════════════
def _rpc_url() -> str:
    return f"https://bsc-mainnet.nodereal.io/v1/{MEGANODE_KEY}"


async def bsc_get_current_block() -> int | None:
    payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
                return int(data["result"], 16)
    except Exception as exc:
        log.error("bsc_get_current_block: %s", exc)
    return None


async def bsc_get_block_hash(block_number: int) -> str | None:
    payload = {
        "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
        "params": [hex(block_number), False], "id": 1,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
                result = data.get("result")
                if result:
                    return result["hash"]
    except Exception as exc:
        log.error("bsc_get_block_hash: %s", exc)
    return None


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


async def bsc_verify_usdt_payment(
    txid: str,
    expected_address: str = WALLET_ADDRESS,
    expected_amount: float = ENTRY_FEE,
) -> tuple[bool, str]:
    """
    Verify USDT BEP-20 transfer on BSC.
    3 attempts, 10 / 20 / 30 s back-off (all async — non-blocking).
    Returns (success, message).
    """
    receipt_payload = {
        "jsonrpc": "2.0", "method": "eth_getTransactionReceipt",
        "params": [txid], "id": 1,
    }
    for attempt in range(1, 4):
        await asyncio.sleep(10 * attempt)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    _rpc_url(), json=receipt_payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        if attempt < 3:
                            continue
                        return False, "BSC API unreachable"
                    data = await r.json()

            receipt = data.get("result") or {}
            if not receipt:
                if attempt < 3:
                    continue
                return False, "Transaction not found or not yet confirmed"

            for log_entry in receipt.get("logs", []):
                if log_entry.get("address", "").lower() != USDT_CONTRACT.lower():
                    continue
                topics = log_entry.get("topics", [])
                if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
                    continue
                to_addr = "0x" + topics[2][2:][-40:]
                if to_addr.lower() != expected_address.lower():
                    continue
                amount = int(log_entry.get("data", "0x0"), 16) / 10**18
                if amount >= expected_amount:
                    return True, f"{amount:.4f} USDT"
                return False, f"Amount too low: {amount:.4f} USDT \\(need {expected_amount}\\)"

            if attempt < 3:
                continue
            return False, "No matching USDT transfer found in this transaction"

        except asyncio.CancelledError:
            raise                      # propagate cancellation cleanly
        except asyncio.TimeoutError:
            if attempt < 3:
                continue
            return False, "BSC API timeout"
        except Exception as exc:
            if attempt < 3:
                continue
            return False, esc(str(exc))

    return False, "Verification failed after 3 attempts"


# ══════════════════════════════════════════════════════════════
#  BACKGROUND TXID VERIFICATION TASK
# ══════════════════════════════════════════════════════════════
async def _verify_task(uid: int, txid: str, reply_to: Message, lang: str) -> None:
    """
    Runs as a background asyncio.Task.
    Verifies the TXID, then sends the result to the user.
    Cancellation (via /cancel) is handled cleanly.
    """
    try:
        success, msg = await bsc_verify_usdt_payment(txid)
    except asyncio.CancelledError:
        # /cancel was issued — message already sent by cmd_cancel
        return
    finally:
        _pending.pop(uid, None)

    if success:
        uname = reply_to.from_user.username or f"user_{uid}"
        async with db_pool.acquire() as conn:
            # Guard: re-check inside a serializable context
            already_in = await conn.fetchval(
                "SELECT 1 FROM participants WHERE telegram_id = $1", uid
            )
            if already_in:
                await reply_to.answer(t("txid_already_in", lang))
                return

            count = await conn.fetchval("SELECT COUNT(*) FROM participants")
            if count >= PARTICIPANT_LIMIT:
                await reply_to.answer(t("round_full", lang, limit=PARTICIPANT_LIMIT))
                return

            ticket = (await conn.fetchval(
                "SELECT COALESCE(MAX(ticket_number), 0) FROM participants"
            )) + 1
            try:
                await conn.execute(
                    "INSERT INTO transactions (txid, user_id, username, amount)"
                    " VALUES ($1,$2,$3,$4)",
                    txid, uid, uname, float(ENTRY_FEE),
                )
                await conn.execute(
                    "INSERT INTO participants (ticket_number, telegram_id, username)"
                    " VALUES ($1,$2,$3)",
                    ticket, uid, f"@{uname}",
                )
                new_count = count + 1
            except asyncpg.UniqueViolationError:
                await reply_to.answer(t("txid_already_in", lang))
                return

        await reply_to.answer(
            t("txid_success", lang,
              ticket=ticket, bank=new_count * ENTRY_FEE, count=new_count)
        )
        log.info("✅ Ticket #%d issued to @%s (pool %d/%d)",
                 ticket, uname, new_count, PARTICIPANT_LIMIT)

        # Auto-draw if round is full
        if new_count >= PARTICIPANT_LIMIT and not draw_lock.locked():
            asyncio.create_task(run_full_draw())
    else:
        await reply_to.answer(t("txid_error", lang, msg=msg, fee=ENTRY_FEE))


# ══════════════════════════════════════════════════════════════
#  DRAW LOGIC
# ══════════════════════════════════════════════════════════════
async def publish_draw_announce(
    round_number: int, participants: list[str], target_block: int
) -> None:
    tickets_text = "\n".join(participants[:25])
    if len(participants) > 25:
        tickets_text += f"\n_\\.\\.\\.  and {len(participants) - 25} more_"
    for lang in ("en", "ru"):
        msg = t("draw_announce", lang,
                round=round_number, count=len(participants),
                tickets=tickets_text, block=target_block)
        await bot.send_message(CHANNEL_ID, msg, disable_web_page_preview=True)


async def execute_draw(
    round_number: int, participants: list[str], target_block: int
) -> tuple[str, int, float] | None:
    wait_msg = await bot.send_message(CHANNEL_ID, t("draw_fetching", "en"))

    block_hash: str | None = None
    for attempt in range(36):       # ~3 min max
        await asyncio.sleep(5)
        block_hash = await bsc_get_block_hash(target_block)
        if block_hash:
            break
        if attempt > 0 and attempt % 6 == 0:
            try:
                await bot.edit_message_text(
                    t("draw_fetching_n", "en", n=attempt // 6 + 1),
                    CHANNEL_ID, wait_msg.message_id,
                )
            except Exception:
                pass

    if not block_hash:
        try:
            await bot.edit_message_text(
                t("draw_hash_failed", "en"), CHANNEL_ID, wait_msg.message_id
            )
        except Exception:
            pass
        return None

    hash_int      = int(block_hash, 16)
    winner_idx    = hash_int % len(participants)
    winner_parts  = participants[winner_idx].split(". ", 1)
    winner_ticket = int(winner_parts[0])
    winner_uname  = winner_parts[1] if len(winner_parts) > 1 else "unknown"

    count      = len(participants)
    bank       = count * ENTRY_FEE
    commission = bank * 0.10
    prize      = bank - commission
    now_str    = datetime.now(timezone.utc).strftime("%d\\.%m\\.%Y %H:%M")

    for lang in ("en", "ru"):
        await bot.send_message(
            CHANNEL_ID,
            t("draw_result", lang,
              round=round_number, date=now_str,
              block=target_block, hash_short=block_hash[:24],
              count=count, bank=bank, commission=commission, prize=prize,
              ticket=winner_ticket, winner=winner_uname),
            disable_web_page_preview=True,
        )

    try:
        await bot.delete_message(CHANNEL_ID, wait_msg.message_id)
    except Exception:
        pass

    return winner_uname, winner_ticket, prize


async def run_full_draw() -> None:
    """Full draw lifecycle. Uses draw_lock — safe as a background task."""
    async with draw_lock:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT ticket_number, username FROM participants ORDER BY ticket_number"
                )
            if len(rows) < 2:
                return

            participants  = [f"{r['ticket_number']}. {r['username']}" for r in rows]
            round_number  = random.randint(1000, 9999)
            current_block = await bsc_get_current_block()

            if not current_block:
                await bot.send_message(ADMIN_ID, "❌ Could not fetch BSC block number\\.",
                                       parse_mode="MarkdownV2")
                return

            target_block = current_block + 20
            await publish_draw_announce(round_number, participants, target_block)
            await asyncio.sleep(120)

            result = await execute_draw(round_number, participants, target_block)

            async with db_pool.acquire() as conn:
                if result:
                    winner_uname, winner_ticket, prize = result
                    bank = len(participants) * ENTRY_FEE
                    await conn.execute(
                        """INSERT INTO draw_history
                           (round_number, participants_count, total_bank,
                            winner_username, winner_ticket, winner_prize,
                            commission, target_block, block_hash)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                        round_number, len(participants), bank,
                        winner_uname, winner_ticket, prize,
                        bank * 0.10, target_block, "see channel post",
                    )
                    await conn.execute("DELETE FROM participants")
                    log.info("🏆 Draw #%d done — winner @%s ticket #%d",
                             round_number, winner_uname, winner_ticket)
                else:
                    log.warning("⚠️  Draw #%d failed — participants kept", round_number)
        except Exception as exc:
            log.exception("💥 run_full_draw crashed: %s", exc)


# ══════════════════════════════════════════════════════════════
#  ADMIN DECORATOR
# ══════════════════════════════════════════════════════════════
def admin_only(func):
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != ADMIN_ID:
            await message.answer("❌ Admin only\\.", parse_mode="MarkdownV2")
            return
        await func(message, *args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════

# ── /start ────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    uid   = message.from_user.id
    uname = message.from_user.username or f"user_{uid}"
    await upsert_user(uid, uname)

    args = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ""
    source, medium, campaign, invited_by = "direct", "direct", "direct", None
    if args.startswith("ref_"):
        parts = args.split("_")
        if len(parts) >= 3:
            source, campaign, medium = parts[1], parts[2], "post"
            if len(parts) > 3 and parts[3].isdigit():
                invited_by = int(parts[3])

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO referral_sources (user_id, source, medium, campaign, invited_by)"
            " VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
            uid, source, medium, campaign, invited_by,
        )

    lang = await get_lang(uid)
    await message.answer(
        t("welcome", lang, channel=CHANNEL_ID, fee=ENTRY_FEE),
        reply_markup=make_keyboard(lang),
    )


# ── /cancel ───────────────────────────────────────────────────
@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    task = _pending.pop(uid, None)
    if task and not task.done():
        task.cancel()
        await message.answer(t("txid_cancelled", lang))
    else:
        await message.answer(t("txid_nothing_to_cancel", lang))


# ── Language toggle ───────────────────────────────────────────
BTN_LANGUAGE_TEXTS = {T["btn_language"]["en"], T["btn_language"]["ru"]}

@dp.message(F.text.in_(BTN_LANGUAGE_TEXTS))
async def handle_language(message: Message) -> None:
    uid     = message.from_user.id
    current = await get_lang(uid)
    new     = "ru" if current == "en" else "en"
    await set_lang(uid, new)
    await message.answer(
        t("lang_now_ru" if new == "ru" else "lang_now_en", new),
        reply_markup=make_keyboard(new),
    )


# ── 🎟 Participate ────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_participate"]["en"], T["btn_participate"]["ru"]}))
async def handle_participate(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM participants")
    await message.answer(
        t("participate_info", lang,
          fee=ENTRY_FEE, wallet=WALLET_ADDRESS,
          count=count, limit=PARTICIPANT_LIMIT)
    )


# ── 💰 Pool ───────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_bank"]["en"], T["btn_bank"]["ru"]}))
async def handle_bank(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM participants")
    bank  = count * ENTRY_FEE
    await message.answer(
        t("bank_info", lang,
          count=count, limit=PARTICIPANT_LIMIT,
          bank=bank, prize=bank * 0.90,
          slots=max(PARTICIPANT_LIMIT - count, 0))
    )


# ── 👥 Members ────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_members"]["en"], T["btn_members"]["ru"]}))
async def handle_members(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticket_number, username FROM participants ORDER BY ticket_number"
        )
    if not rows:
        await message.answer(t("members_empty", lang))
        return
    count = len(rows)
    text  = t("members_header", lang, count=count)
    for row in rows[:25]:
        text += f"\\#{row['ticket_number']} — {esc(row['username'])}\n"
    if count > 25:
        text += t("members_more", lang, n=count - 25)
    await message.answer(text)


# ── 📊 Statistics ─────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_stats"]["en"], T["btn_stats"]["ru"]}) | Command("stats"))
async def handle_stats(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(*)                          AS draws,
                   COALESCE(SUM(participants_count),0) AS participants,
                   COALESCE(SUM(total_bank),0)         AS bank,
                   COALESCE(SUM(commission),0)         AS commission,
                   COALESCE(MAX(winner_prize),0)       AS max_prize,
                   COALESCE(MAX(total_bank),0)         AS max_bank
            FROM draw_history
        """)
    if not row or row["draws"] == 0:
        await message.answer(t("stats_empty", lang))
        return
    await message.answer(t("stats", lang, **dict(row)))


# ── 📜 History ────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_history"]["en"], T["btn_history"]["ru"]}) | Command("history"))
async def handle_history(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT round_number, draw_date, participants_count,
                   total_bank, winner_username, winner_ticket, winner_prize
            FROM draw_history ORDER BY draw_date DESC LIMIT 10
        """)
    if not rows:
        await message.answer(t("history_empty", lang))
        return
    text = t("history_header", lang)
    for row in rows:
        date_str = row["draw_date"].strftime("%d\\.%m\\.%Y %H:%M") if row["draw_date"] else "—"
        text += t("history_row", lang,
                  round=row["round_number"], date=date_str,
                  count=row["participants_count"], bank=row["total_bank"],
                  ticket=row["winner_ticket"],
                  winner=esc(row["winner_username"]),
                  prize=row["winner_prize"])
    # Split if too long (Telegram 4096-char limit)
    if len(text) > 4000:
        text = text[:4000] + "\n\\.\\.\\."
    await message.answer(text)


# ── 📆 Weekly ─────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_week"]["en"], T["btn_week"]["ru"]}) | Command("weekly"))
async def handle_weekly(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT COUNT(*)                          AS draws,
                   COALESCE(SUM(participants_count),0) AS participants,
                   COALESCE(SUM(total_bank),0)         AS bank,
                   COALESCE(SUM(commission),0)         AS commission,
                   COALESCE(MAX(winner_prize),0)       AS max_prize
            FROM draw_history WHERE draw_date > NOW() - INTERVAL '7 days'
        """)
        top = await conn.fetchrow("""
            SELECT winner_username, COUNT(*) AS wins
            FROM draw_history WHERE draw_date > NOW() - INTERVAL '7 days'
            GROUP BY winner_username ORDER BY wins DESC LIMIT 1
        """)
    text = t("weekly", lang, **dict(stats))
    if stats["draws"] == 0:
        text += t("weekly_empty", lang)
    elif top:
        text += t("weekly_top", lang,
                  winner=esc(top["winner_username"]), wins=top["wins"])
    await message.answer(text)


# ══════════════════════════════════════════════════════════════
#  TXID HANDLER  — fires background task, returns immediately
# ══════════════════════════════════════════════════════════════
@dp.message(lambda message: message.text and not message.text.startswith("/") and message.text not in ALL_BUTTON_TEXTS)
async def handle_txid(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    raw  = (message.text or "").strip()

    # Basic TXID shape check
    if len(raw) < 60 or len(raw) > 70 or not raw.startswith("0x"):
        await message.answer(t("txid_invalid", lang))
        return

    txid = raw.lower()

    # Quick pre-checks before firing the slow task
    async with db_pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM transactions WHERE txid=$1", txid):
            await message.answer(t("txid_duplicate", lang))
            return
        if await conn.fetchval("SELECT 1 FROM participants WHERE telegram_id=$1", uid):
            await message.answer(t("txid_already_in", lang))
            return
        count = await conn.fetchval("SELECT COUNT(*) FROM participants")
    if count >= PARTICIPANT_LIMIT:
        await message.answer(t("round_full", lang, limit=PARTICIPANT_LIMIT))
        return

    # Block duplicate concurrent verifications from same user
    existing = _pending.get(uid)
    if existing and not existing.done():
        await message.answer(t("txid_already_verifying", lang))
        return

    # Ack immediately — webhook will return 200 right after this handler exits
    await message.answer(t("txid_checking", lang))

    # Fire the slow verification as a background task
    task = asyncio.create_task(_verify_task(uid, txid, message, lang))
    _pending[uid] = task


# ══════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════
@dp.message(Command("add"))
@admin_only
async def cmd_add(message: Message) -> None:
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: `/add @username`", parse_mode="MarkdownV2")
        return
    username = args[1].strip()
    async with db_pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM participants WHERE username=$1", username):
            await message.answer(f"⚠️ {esc(username)} already participating\\.",
                                 parse_mode="MarkdownV2")
            return
        ticket = (await conn.fetchval(
            "SELECT COALESCE(MAX(ticket_number),0) FROM participants"
        )) + 1
        await conn.execute(
            "INSERT INTO participants (ticket_number, telegram_id, username) VALUES ($1,0,$2)",
            ticket, username,
        )
    await message.answer(f"✅ {esc(username)} added\\! Ticket \\#{ticket}",
                         parse_mode="MarkdownV2")


@dp.message(Command("reset_db"))
@admin_only
async def cmd_reset_db(message: Message) -> None:
    async with db_pool.acquire() as conn:
        for tbl in ("participants", "transactions", "draw_history", "referral_sources"):
            await conn.execute(f"DELETE FROM {tbl}")
    await message.answer(t("admin_reset", "en"))


@dp.message(Command("find_txid"))
@admin_only
async def cmd_find_txid(message: Message) -> None:
    args   = message.text.split(maxsplit=1)
    needle = args[1].strip().lower() if len(args) > 1 else ""
    async with db_pool.acquire() as conn:
        if needle:
            row = await conn.fetchrow(
                "SELECT * FROM transactions WHERE LOWER(txid) LIKE $1", f"%{needle}%"
            )
            txt = f"✅ Found:\n`{esc(str(dict(row)))}`" if row else f"❌ Not found: `{esc(needle[:40])}`"
            await message.answer(txt, parse_mode="MarkdownV2")

        rows = await conn.fetch(
            "SELECT txid, username, created_at FROM transactions ORDER BY created_at DESC LIMIT 10"
        )
    if rows:
        lines = "\n".join(
            f"• `{r['txid'][:14]}…{r['txid'][-8:]}` — {esc(r['username'])}"
            for r in rows
        )
        await message.answer(f"📋 *Last 10 TXIDs:*\n{lines}", parse_mode="MarkdownV2")
    else:
        await message.answer("📭 Transaction table is empty\\.", parse_mode="MarkdownV2")


@dp.message(Command("announce"))
@admin_only
async def cmd_announce(message: Message) -> None:
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM participants")
        last  = await conn.fetchrow(
            "SELECT winner_username, winner_ticket, winner_prize "
            "FROM draw_history ORDER BY draw_date DESC LIMIT 1"
        )
    bank        = count * ENTRY_FEE
    last_winner = esc(f"@{last['winner_username']}") if last else t("no_winner_yet", "en")
    last_ticket = f"\\#{last['winner_ticket']}" if last else "—"
    last_prize  = f"{last['winner_prize']:.2f}" if last else "0"

    for lang in ("en", "ru"):
        await bot.send_message(
            CHANNEL_ID,
            t("announce_post", lang,
              bank=bank, count=count, limit=PARTICIPANT_LIMIT, fee=ENTRY_FEE,
              last_winner=last_winner, last_ticket=last_ticket, last_prize=last_prize),
            disable_web_page_preview=True,
        )
    await message.answer(t("admin_published", "en"))


@dp.message(Command("start_draw"))
@admin_only
async def cmd_start_draw(message: Message) -> None:
    if draw_lock.locked():
        await message.answer("⚠️ Draw already running\\.", parse_mode="MarkdownV2")
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT 1 FROM participants ORDER BY ticket_number LIMIT 2"
        )
    if len(rows) < 2:
        await message.answer("❌ Need at least 2 participants\\.",
                             parse_mode="MarkdownV2")
        return
    asyncio.create_task(run_full_draw())
    await message.answer("✅ Draw task launched\\! Results will be posted in the channel\\.",
                         parse_mode="MarkdownV2")


@dp.message(Command("gen_link"))
@admin_only
async def cmd_gen_link(message: Message) -> None:
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Usage: `/gen_link channel_name campaign`",
                             parse_mode="MarkdownV2")
        return
    channel_name, campaign = parts[1], parts[2]
    code    = hashlib.md5(f"{channel_name}_{campaign}_{time.time()}".encode()).hexdigest()[:8]
    bot_me  = await bot.get_me()
    link    = f"https://t.me/{bot_me.username}?start=ref_{channel_name}_{campaign}_{code}"
    await message.answer(
        t("gen_link_ok", "en",
          channel=esc(channel_name), link=link,
          campaign=esc(campaign), code=code)
    )


@dp.message(Command("sources"))
@admin_only
async def cmd_sources(message: Message) -> None:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT source, COUNT(*) AS users,
                   COUNT(CASE WHEN invited_by IS NOT NULL THEN 1 END) AS referrals
            FROM referral_sources GROUP BY source ORDER BY users DESC
        """)
    if not rows:
        await message.answer(t("sources_empty", "en"))
        return
    text = t("sources_header", "en")
    for row in rows:
        text += f"📌 *{esc(row['source'])}* — {row['users']} users, {row['referrals']} referrals\n"
    await message.answer(text)


# ══════════════════════════════════════════════════════════════
#  FASTAPI + WEBHOOK
# ══════════════════════════════════════════════════════════════
app = FastAPI(title="Crypto Fortuna Bot")


@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    """
    ⚡ CRITICAL FIX: return 200 IMMEDIATELY, process in background.
    Previous version awaited dp.feed_update() — Telegram retried after 5s
    because it never got a timely response → caused the infinite loop.
    """
    data   = await request.json()
    update = types.Update.model_validate(data)
    asyncio.create_task(dp.feed_update(bot, update))   # fire-and-forget
    return {"ok": True}                                 # instant 200


@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.time()}


@app.get("/")
async def root():
    return {"service": "Crypto Fortuna Bot v2.1", "status": "running"}


@app.on_event("startup")
async def on_startup() -> None:
    global draw_lock
    draw_lock = asyncio.Lock()   # created inside the running event loop

    await init_db()

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook/{BOT_TOKEN}"
        await bot.set_webhook(
            webhook_url,
            drop_pending_updates=True,   # ← clears stale queue on restart
        )
        log.info("✅ Webhook set: %s (pending updates dropped)", webhook_url)
    else:
        log.warning("⚠️  RENDER_EXTERNAL_URL not set — webhook not registered")

    asyncio.create_task(keep_db_alive())
    log.info("✅ Bot v2.1 started")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await bot.delete_webhook()
    if db_pool:
        await db_pool.close()
    log.info("Bot shut down cleanly")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)

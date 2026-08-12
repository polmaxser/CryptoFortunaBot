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
from urllib.parse import quote

import aiohttp
import asyncpg
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, Message, WebAppInfo,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
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
REFERRALS_PER_FREE_TICKET = int(os.getenv("REFERRALS_PER_FREE_TICKET", "5"))
PAYMENT_REMINDER_HOURS = float(os.getenv("PAYMENT_REMINDER_HOURS", "3"))
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

# Last BSC block scanned by payment_watcher() for incoming USDT transfers.
_last_scanned_block: int | None = None

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
#  ADMIN ALERTS  — throttled so a flaky RPC doesn't spam the admin
# ══════════════════════════════════════════════════════════════
_last_alert: dict[str, float] = {}
ALERT_COOLDOWN_SEC = 1800  # 30 min between repeats of the same alert kind


async def alert_admin(kind: str, message: str) -> None:
    now = time.time()
    if now - _last_alert.get(kind, 0) < ALERT_COOLDOWN_SEC:
        return
    _last_alert[kind] = now
    try:
        await bot.send_message(ADMIN_ID, f"⚠️ {esc(message)}", parse_mode="MarkdownV2")
    except Exception as exc:
        log.error("Could not alert admin: %s", exc)


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
    "btn_invite":      {"en": "🔗 Invite",        "ru": "🔗 Пригласить"},
    "btn_app":         {"en": "📱 Open App",      "ru": "📱 Открыть приложение"},
    "btn_profile":     {"en": "👤 Profile",       "ru": "👤 Профиль"},
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
            "1️⃣ Send *exactly {fee} USDT* \\(BEP\\-20, BSC network\\) to the address below\\.\n"
            "The amount is unique to you — sending the exact figure lets the bot detect "
            "and confirm your entry *automatically*, usually within \\~30 seconds\\.\n\n"
            "2️⃣ Didn't get confirmed in a few minutes? Paste your *transaction hash \\(TXID\\)* "
            "here and the bot will verify it manually\\.\n\n"
            "⚠️ *Important:* USDT BEP\\-20 on BSC only, and the amount must match exactly\\. "
            "Other networks will not be detected\\.\n\n"
            "👥 Current participants: *{count}/{limit}*\n\n"
            "👇 Tap either value below to copy it\\."
        ),
        "ru": (
            "🎟 *Как участвовать в текущем раунде:*\n\n"
            "1️⃣ Отправь *ровно {fee} USDT* \\(BEP\\-20, сеть BSC\\) на адрес ниже\\.\n"
            "Сумма уникальна именно для тебя — если отправить её точно, бот обнаружит "
            "и подтвердит участие *автоматически*, обычно за \\~30 секунд\\.\n\n"
            "2️⃣ Не подтвердилось за пару минут? Вставь сюда *хэш транзакции \\(TXID\\)*, "
            "и бот проверит вручную\\.\n\n"
            "⚠️ *Важно:* Только USDT BEP\\-20 в сети BSC, и сумма должна совпадать точно\\. "
            "Другие сети не будут обнаружены\\.\n\n"
            "👥 Текущих участников: *{count}/{limit}*\n\n"
            "👇 Нажми на любое значение ниже, чтобы скопировать его\\."
        ),
    },

    # ── Bank / Pool ───────────────────────────────────────────
    "bank_info": {
        "en": (
            "💰 *Current Round Pool*\n\n"
            "👥 Participants: *{count}/{limit}*\n"
            "{bar}\n"
            "💵 Total pool: *{bank} USDT*\n"
            "🏆 Winner's reward \\(90%\\): *{prize:.2f} USDT*\n"
            "📋 Open slots: *{slots}*\n"
            "⏳ Est\\. time to fill: *{eta}*"
        ),
        "ru": (
            "💰 *Банк текущего раунда*\n\n"
            "👥 Участников: *{count}/{limit}*\n"
            "{bar}\n"
            "💵 Общий банк: *{bank} USDT*\n"
            "🏆 Приз победителю \\(90%\\): *{prize:.2f} USDT*\n"
            "📋 Свободных мест: *{slots}*\n"
            "⏳ Ожидаемое время до заполнения: *{eta}*"
        ),
    },
    "eta_unknown": {"en": "not enough data yet", "ru": "пока недостаточно данных"},
    "eta_full":    {"en": "round is full",        "ru": "раунд заполнен"},

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

    # ── Referral (user-facing) ───────────────────────────────────
    "invite_info": {
        "en": (
            "🔗 *Invite friends, earn free tickets*\n\n"
            "Your personal link:\n`{link}`\n\n"
            "👥 Friends invited who joined a round: *{count}*\n"
            "🎁 Free tickets available: *{free}*\n\n"
            "For every *{goal}* friends who make a real entry, you get *1 free ticket* "
            "\\— redeem it with /free\\_ticket\\."
        ),
        "ru": (
            "🔗 *Приглашай друзей — получай бесплатные билеты*\n\n"
            "Твоя личная ссылка:\n`{link}`\n\n"
            "👥 Приглашённых друзей, вступивших в раунд: *{count}*\n"
            "🎁 Доступно бесплатных билетов: *{free}*\n\n"
            "За каждые *{goal}* друзей, сделавших реальный взнос, ты получаешь *1 бесплатный билет* "
            "\\— используй командой /free\\_ticket\\."
        ),
    },
    "free_ticket_none": {
        "en": "🎁 You don't have any free tickets yet\\. Invite friends with 🔗 Invite to earn one\\.",
        "ru": "🎁 У тебя пока нет бесплатных билетов\\. Приглашай друзей через 🔗 Пригласить, чтобы заработать\\.",
    },
    "free_ticket_used": {
        "en": (
            "🎁 *Free ticket redeemed\\!*\n\n"
            "🎟 Your ticket number: *\\#{ticket}*\n"
            "💰 Pool: *{bank} USDT* \\({count} participants\\)\n\n"
            "Good luck\\! 🍀"
        ),
        "ru": (
            "🎁 *Бесплатный билет использован\\!*\n\n"
            "🎟 Твой номер билета: *\\#{ticket}*\n"
            "💰 Банк: *{bank} USDT* \\({count} участников\\)\n\n"
            "Удачи\\! 🍀"
        ),
    },
    "share_win_dm": {
        "en": (
            "🏆 *You won Draw \\#{round}\\!*\n\n"
            "🎟 Ticket \\#{ticket}\n"
            "🎁 Prize: *{prize} USDT*\n\n"
            "Congratulations\\! Share the news \\— it's fully verifiable on BSC\\."
        ),
        "ru": (
            "🏆 *Ты выиграл в розыгрыше \\#{round}\\!*\n\n"
            "🎟 Билет \\#{ticket}\n"
            "🎁 Приз: *{prize} USDT*\n\n"
            "Поздравляем\\! Поделись новостью \\— всё проверяемо в BSC\\."
        ),
    },
    "share_win_button": {"en": "📤 Share the win", "ru": "📤 Поделиться результатом"},
    "share_win_text": {
        "en": "🏆 I just won {prize:.2f} USDT in Crypto Fortuna Club! Ticket #{ticket}, Draw #{round} — transparent and verifiable on BSC 🔐",
        "ru": "🏆 Я выиграл {prize:.2f} USDT в Crypto Fortuna Club! Билет #{ticket}, розыгрыш #{round} — прозрачно и проверяемо в BSC 🔐",
    },
    "participate_reminder": {
        "en": (
            "👋 *Still there?*\n\n"
            "You started joining the current round but we haven't seen your payment yet\\.\n\n"
            "💎 Send *exactly {fee} USDT* \\(BEP\\-20, BSC\\) to:\n`{wallet}`\n\n"
            "The bot will pick it up automatically \\— no rush, your spot is still open\\."
        ),
        "ru": (
            "👋 *Всё ещё здесь?*\n\n"
            "Ты начал вступление в текущий раунд, но мы пока не увидели оплату\\.\n\n"
            "💎 Отправь *ровно {fee} USDT* \\(BEP\\-20, BSC\\) на адрес:\n`{wallet}`\n\n"
            "Бот подхватит платёж автоматически \\— место всё ещё за тобой\\."
        ),
    },
    "profile_info": {
        "en": (
            "👤 *Your profile*\n\n"
            "🎟 Rounds joined: *{joined}*\n"
            "🏆 Rounds won: *{wins}*\n"
            "💰 Total winnings: *{winnings} USDT*\n"
            "💸 Total contributed: *{spent} USDT*\n\n"
            "🔗 Friends invited: *{referrals}*\n"
            "🎁 Free tickets available: *{free}*"
        ),
        "ru": (
            "👤 *Твой профиль*\n\n"
            "🎟 Раундов сыграно: *{joined}*\n"
            "🏆 Раундов выиграно: *{wins}*\n"
            "💰 Всего выиграно: *{winnings} USDT*\n"
            "💸 Всего внесено: *{spent} USDT*\n\n"
            "🔗 Приглашено друзей: *{referrals}*\n"
            "🎁 Доступно бесплатных билетов: *{free}*"
        ),
    },
    "referral_reward_earned": {
        "en": (
            "🎉 *Referral reward\\!*\n\n"
            "One of your friends just made their entry \\— you've now invited *{count}* people who joined\\.\n"
            "🎁 You earned a *free ticket*\\! Redeem it with /free\\_ticket\\."
        ),
        "ru": (
            "🎉 *Реферальная награда\\!*\n\n"
            "Один из твоих друзей только что вступил в раунд \\— теперь на твоём счету *{count}* приглашённых\\.\n"
            "🎁 Ты заработал *бесплатный билет*\\! Используй командой /free\\_ticket\\."
        ),
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

CREATE TABLE IF NOT EXISTS pending_payments (
    telegram_id BIGINT        PRIMARY KEY,
    amount      NUMERIC(20,6) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ   DEFAULT NOW()
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

ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_count INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS free_tickets INTEGER DEFAULT 0;
ALTER TABLE referral_sources ADD COLUMN IF NOT EXISTS rewarded_at TIMESTAMPTZ;
ALTER TABLE participants ADD COLUMN IF NOT EXISTS is_free BOOLEAN DEFAULT FALSE;
ALTER TABLE draw_history ADD COLUMN IF NOT EXISTS winner_telegram_id BIGINT;
ALTER TABLE pending_payments ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ;
ALTER TABLE pending_payments ALTER COLUMN amount TYPE NUMERIC(20,8);
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
            await alert_admin("db_down", f"DB keep-alive failed: {exc}")


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


async def get_pool_counts(conn) -> tuple[int, int]:
    """Returns (total_participants, paid_participants). Free (referral) tickets
    take a slot in the draw but contribute 0 USDT to the payable bank."""
    total = await conn.fetchval("SELECT COUNT(*) FROM participants")
    paid = await conn.fetchval("SELECT COUNT(*) FROM participants WHERE is_free = FALSE")
    return total, paid


async def grant_referral_reward(conn, referred_uid: int) -> None:
    """Called once, the first time a referred user's payment is confirmed.
    Credits the referrer's referral_count and, every REFERRALS_PER_FREE_TICKET,
    a free ticket — then notifies them."""
    ref_row = await conn.fetchrow(
        "SELECT invited_by FROM referral_sources "
        "WHERE user_id=$1 AND invited_by IS NOT NULL AND rewarded_at IS NULL",
        referred_uid,
    )
    if not ref_row:
        return
    referrer_uid = ref_row["invited_by"]

    await conn.execute(
        "UPDATE referral_sources SET rewarded_at = NOW() "
        "WHERE user_id=$1 AND invited_by IS NOT NULL AND rewarded_at IS NULL",
        referred_uid,
    )
    new_count = await conn.fetchval(
        "UPDATE users SET referral_count = referral_count + 1 "
        "WHERE telegram_id=$1 RETURNING referral_count",
        referrer_uid,
    )
    if new_count is None:
        return

    if new_count % REFERRALS_PER_FREE_TICKET == 0:
        await conn.execute(
            "UPDATE users SET free_tickets = free_tickets + 1 WHERE telegram_id=$1",
            referrer_uid,
        )
        try:
            referrer_lang = await get_lang(referrer_uid)
            await bot.send_message(
                referrer_uid,
                t("referral_reward_earned", referrer_lang, count=new_count),
            )
        except Exception as exc:
            log.warning("Could not notify referrer %s: %s", referrer_uid, exc)


def render_bar(count: int, limit: int, width: int = 14) -> str:
    if limit <= 0:
        return ""
    pct = min(1.0, count / limit)
    filled = round(pct * width)
    return "▓" * filled + "░" * (width - filled) + f" {round(pct * 100)}%"


def format_eta(count: int, limit: int, joined_at: list[datetime], lang: str) -> str:
    remaining = limit - count
    if remaining <= 0:
        return t("eta_full", lang)
    if len(joined_at) < 2:
        return t("eta_unknown", lang)

    span_sec = (joined_at[-1] - joined_at[0]).total_seconds()
    if span_sec <= 0:
        return t("eta_unknown", lang)

    avg_interval = span_sec / (len(joined_at) - 1)
    eta_sec = avg_interval * remaining

    unit = "мин" if lang == "ru" else "min"
    if eta_sec < 3600:
        return f"~{max(1, round(eta_sec / 60))} {unit}"
    unit = "ч" if lang == "ru" else "h"
    if eta_sec < 86400 * 2:
        return f"~{round(eta_sec / 3600)} {unit}"
    unit = "дн" if lang == "ru" else "d"
    return f"~{round(eta_sec / 86400)} {unit}"


# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════
def make_keyboard(lang: str) -> ReplyKeyboardMarkup:
    rows = [
        [t("btn_participate", lang), t("btn_bank", lang), t("btn_members", lang)],
        [t("btn_stats", lang),       t("btn_history", lang), t("btn_week", lang)],
        [t("btn_invite", lang),      t("btn_profile", lang), t("btn_language", lang)],
    ]
    keyboard = [[KeyboardButton(text=c) for c in row] for row in rows]

    # WebApp buttons need a real HTTPS URL — only offered when deployed.
    if RENDER_URL:
        keyboard.append([
            KeyboardButton(text=t("btn_app", lang), web_app=WebAppInfo(url=f"{RENDER_URL}/app/"))
        ])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


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
        await alert_admin("rpc_down", f"BSC RPC unreachable (bsc_get_current_block): {exc}")
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
        await alert_admin("rpc_down", f"BSC RPC unreachable (bsc_get_block_hash): {exc}")
    return None


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


async def bsc_get_logs(from_block: int, to_block: int) -> list[dict]:
    """USDT Transfer events landing on our wallet in [from_block, to_block]."""
    padded_wallet = "0x" + "0" * 24 + WALLET_ADDRESS[2:].lower()
    payload = {
        "jsonrpc": "2.0", "method": "eth_getLogs",
        "params": [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": USDT_CONTRACT,
            "topics": [TRANSFER_TOPIC, None, padded_wallet],
        }],
        "id": 1,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _rpc_url(), json=payload, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                data = await r.json()
                return data.get("result") or []
    except Exception as exc:
        log.error("bsc_get_logs: %s", exc)
        return []


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
                return False, esc(f"Amount too low: {amount:.4f} USDT (need {expected_amount})")

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


async def _generate_unique_amount(conn) -> float:
    """A near-invisible offset (< 1 cent, e.g. 5.00437182) unique to one pending
    payer, so incoming transfers can be auto-matched without a memo/TXID paste.
    Kept in the 7th-8th decimal so the fee still reads as "5 USDT" at a glance."""
    for _ in range(20):
        offset = random.randint(1, 999999)
        amount = round(ENTRY_FEE + offset / 10**8, 8)
        taken = await conn.fetchval(
            "SELECT 1 FROM pending_payments WHERE amount = $1", amount
        )
        if not taken:
            return amount
    raise RuntimeError("Could not allocate a unique payment amount")


# ══════════════════════════════════════════════════════════════
#  AUTOMATIC PAYMENT DETECTION  — no TXID paste required
# ══════════════════════════════════════════════════════════════
async def payment_watcher() -> None:
    """
    Polls the wallet for incoming USDT transfers and auto-credits whichever
    user was assigned that exact amount via 🎟 Participate. Manual TXID paste
    (see handle_txid below) remains available as a fallback.
    """
    global _last_scanned_block
    while True:
        await asyncio.sleep(20)
        try:
            latest = await bsc_get_current_block()
            if not latest:
                continue
            if _last_scanned_block is None:
                _last_scanned_block = latest
                continue
            if latest <= _last_scanned_block:
                continue

            from_block = _last_scanned_block + 1
            to_block = min(latest, from_block + 2000)
            logs = await bsc_get_logs(from_block, to_block)

            for entry in logs:
                txid = entry.get("transactionHash")
                if not txid:
                    continue
                amount = round(int(entry.get("data", "0x0"), 16) / 10**18, 8)

                async with db_pool.acquire() as conn:
                    if await conn.fetchval("SELECT 1 FROM transactions WHERE txid=$1", txid):
                        continue
                    pending = await conn.fetchrow(
                        "SELECT telegram_id FROM pending_payments WHERE amount = $1", amount
                    )
                    if not pending:
                        continue
                    uid = pending["telegram_id"]

                    already_in = await conn.fetchval(
                        "SELECT 1 FROM participants WHERE telegram_id = $1", uid
                    )
                    count, _ = await get_pool_counts(conn)
                    if already_in or count >= PARTICIPANT_LIMIT:
                        await conn.execute(
                            "DELETE FROM pending_payments WHERE telegram_id = $1", uid
                        )
                        continue

                    try:
                        chat = await bot.get_chat(uid)
                        uname = chat.username or f"user_{uid}"
                    except Exception:
                        uname = f"user_{uid}"

                    ticket = (await conn.fetchval(
                        "SELECT COALESCE(MAX(ticket_number), 0) FROM participants"
                    )) + 1
                    try:
                        await conn.execute(
                            "INSERT INTO transactions (txid, user_id, username, amount)"
                            " VALUES ($1,$2,$3,$4)",
                            txid, uid, uname, amount,
                        )
                        await conn.execute(
                            "INSERT INTO participants (ticket_number, telegram_id, username)"
                            " VALUES ($1,$2,$3)",
                            ticket, uid, f"@{uname}",
                        )
                    except asyncpg.UniqueViolationError:
                        continue

                    await conn.execute(
                        "DELETE FROM pending_payments WHERE telegram_id = $1", uid
                    )
                    await grant_referral_reward(conn, uid)
                    _, new_paid = await get_pool_counts(conn)
                    new_count = count + 1

                lang = await get_lang(uid)
                try:
                    await bot.send_message(
                        uid,
                        t("txid_success", lang,
                          ticket=ticket, bank=new_paid * ENTRY_FEE, count=new_count),
                    )
                except Exception as exc:
                    log.warning("Could not notify auto-credited user %s: %s", uid, exc)
                log.info("✅ Auto-detected payment: ticket #%d for @%s (%.6f USDT, tx %s)",
                         ticket, uname, amount, txid)

                if new_count >= PARTICIPANT_LIMIT and not draw_lock.locked():
                    asyncio.create_task(run_full_draw())

            _last_scanned_block = to_block
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("payment_watcher crashed: %s", exc)
            await alert_admin("payment_watcher_down", f"payment_watcher crashed: {exc}")


async def abandoned_payment_reminder() -> None:
    """Nudges users who pressed 🎟 Participate but never sent the payment."""
    while True:
        await asyncio.sleep(1800)
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT telegram_id, amount FROM pending_payments "
                    "WHERE reminded_at IS NULL "
                    "AND created_at < NOW() - ($1 || ' hours')::interval",
                    str(PAYMENT_REMINDER_HOURS),
                )
                for row in rows:
                    await conn.execute(
                        "UPDATE pending_payments SET reminded_at = NOW() WHERE telegram_id = $1",
                        row["telegram_id"],
                    )

            for row in rows:
                uid = row["telegram_id"]
                lang = await get_lang(uid)
                try:
                    await bot.send_message(
                        uid,
                        t("participate_reminder", lang,
                          fee=esc(f"{float(row['amount']):.8f}"), wallet=WALLET_ADDRESS),
                    )
                except Exception as exc:
                    log.warning("Could not send reminder to %s: %s", uid, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("abandoned_payment_reminder crashed: %s", exc)
            await alert_admin("reminder_down", f"abandoned_payment_reminder crashed: {exc}")


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

            count, paid = await get_pool_counts(conn)
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
                new_paid = paid + 1
            except asyncpg.UniqueViolationError:
                await reply_to.answer(t("txid_already_in", lang))
                return

            await conn.execute("DELETE FROM pending_payments WHERE telegram_id = $1", uid)
            await grant_referral_reward(conn, uid)

        await reply_to.answer(
            t("txid_success", lang,
              ticket=ticket, bank=new_paid * ENTRY_FEE, count=new_count)
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
        await bot.send_message(CHANNEL_ID, msg, disable_web_page_preview=True, parse_mode=None)


async def execute_draw(
    round_number: int, participants: list[str], target_block: int, paid_count: int,
    ticket_to_uid: dict[int, int],
) -> tuple[str, int, float, int | None] | None:
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
    winner_uid    = ticket_to_uid.get(winner_ticket)

    count      = len(participants)
    bank       = paid_count * ENTRY_FEE
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
            parse_mode=None,
        )

    try:
        await bot.delete_message(CHANNEL_ID, wait_msg.message_id)
    except Exception:
        pass

    return winner_uname, winner_ticket, prize, winner_uid


async def notify_winner(winner_uid: int, round_number: int, ticket: int, prize: float) -> None:
    """DMs the winner a congrats message with a native Telegram share button."""
    try:
        lang = await get_lang(winner_uid)
        bot_me = await bot.get_me()
        share_text = t("share_win_text", lang, prize=prize, ticket=ticket, round=round_number)
        share_url = (
            f"https://t.me/share/url?url={quote(f'https://t.me/{bot_me.username}')}"
            f"&text={quote(share_text)}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("share_win_button", lang), url=share_url)
        ]])
        await bot.send_message(
            winner_uid,
            t("share_win_dm", lang, round=round_number, ticket=ticket, prize=esc(f"{prize:.2f}")),
            reply_markup=keyboard,
        )
    except Exception as exc:
        log.warning("Could not notify winner %s: %s", winner_uid, exc)


async def run_full_draw() -> None:
    """Full draw lifecycle. Uses draw_lock — safe as a background task."""
    async with draw_lock:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT ticket_number, username, telegram_id, is_free"
                    " FROM participants ORDER BY ticket_number"
                )
            if len(rows) < 2:
                return

            participants  = [f"{r['ticket_number']}. {r['username']}" for r in rows]
            paid_count    = sum(1 for r in rows if not r["is_free"])
            ticket_to_uid = {r["ticket_number"]: r["telegram_id"] for r in rows}
            round_number  = random.randint(1000, 9999)
            current_block = await bsc_get_current_block()

            if not current_block:
                await bot.send_message(ADMIN_ID, "❌ Could not fetch BSC block number\\.",
                                       parse_mode="MarkdownV2")
                return

            target_block = current_block + 20
            await publish_draw_announce(round_number, participants, target_block)
            await asyncio.sleep(120)

            result = await execute_draw(
                round_number, participants, target_block, paid_count, ticket_to_uid
            )

            async with db_pool.acquire() as conn:
                if result:
                    winner_uname, winner_ticket, prize, winner_uid = result
                    bank = paid_count * ENTRY_FEE
                    await conn.execute(
                        """INSERT INTO draw_history
                           (round_number, participants_count, total_bank,
                            winner_username, winner_ticket, winner_prize,
                            commission, target_block, block_hash, winner_telegram_id)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                        round_number, len(participants), bank,
                        winner_uname, winner_ticket, prize,
                        bank * 0.10, target_block, "see channel post", winner_uid,
                    )
                    await conn.execute("DELETE FROM participants")
                    log.info("🏆 Draw #%d done — winner @%s ticket #%d",
                             round_number, winner_uname, winner_ticket)

                    if winner_uid:
                        await notify_winner(winner_uid, round_number, winner_ticket, prize)
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
        if len(parts) == 2 and parts[1].isdigit():
            # personal referral link: ref_<telegram_id> (from 🔗 Invite)
            ref_uid = int(parts[1])
            if ref_uid != uid:
                source, medium, campaign, invited_by = "referral", "friend", "direct", ref_uid
        elif len(parts) >= 3:
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
        if await conn.fetchval("SELECT 1 FROM participants WHERE telegram_id=$1", uid):
            await message.answer(t("txid_already_in", lang))
            return
        count = await conn.fetchval("SELECT COUNT(*) FROM participants")
        if count >= PARTICIPANT_LIMIT:
            await message.answer(t("round_full", lang, limit=PARTICIPANT_LIMIT))
            return

        amount = await conn.fetchval(
            "SELECT amount FROM pending_payments WHERE telegram_id=$1", uid
        )
        if amount is None:
            amount = await _generate_unique_amount(conn)
            await conn.execute(
                "INSERT INTO pending_payments (telegram_id, amount) VALUES ($1,$2) "
                "ON CONFLICT (telegram_id) DO UPDATE SET amount=$2, created_at=NOW()",
                uid, amount,
            )

    # Первое сообщение — инструкция (без адреса)
    await message.answer(
        t("participate_info", lang,
          fee=esc(f"{amount:.8f}"), wallet=WALLET_ADDRESS,
          count=count, limit=PARTICIPANT_LIMIT)
    )

    # Второе сообщение — ТОЛЬКО адрес кошелька, без лишнего текста
    # (чтобы копирование сообщения целиком копировало именно адрес)
    await message.answer(f"`{WALLET_ADDRESS}`", parse_mode="MarkdownV2")

    # Третье сообщение — ТОЛЬКО точная сумма, без лишнего текста
    await message.answer(f"`{amount:.8f}`", parse_mode="MarkdownV2")


# ── 💰 Pool ───────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_bank"]["en"], T["btn_bank"]["ru"]}))
async def handle_bank(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        count, paid = await get_pool_counts(conn)
        joined_at = [
            r["created_at"] for r in
            await conn.fetch("SELECT created_at FROM participants ORDER BY created_at")
        ]
    bank = paid * ENTRY_FEE
    await message.answer(
        t("bank_info", lang,
          count=count, limit=PARTICIPANT_LIMIT,
          bar=render_bar(count, PARTICIPANT_LIMIT),
          bank=bank, prize=bank * 0.90,
          slots=max(PARTICIPANT_LIMIT - count, 0),
          eta=format_eta(count, PARTICIPANT_LIMIT, joined_at, lang)),
        parse_mode=None
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
        await message.answer(t("members_empty", lang), parse_mode=None)
        return
    count = len(rows)
    text  = t("members_header", lang, count=count)
    for row in rows[:25]:
        text += f"\\#{row['ticket_number']} — {esc(row['username'])}\n"
    if count > 25:
        text += t("members_more", lang, n=count - 25)
    await message.answer(text, parse_mode=None)


# ── 📊 Statistics ─────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_stats"]["en"], T["btn_stats"]["ru"]}))
@dp.message(Command("stats"))
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
        await message.answer(t("stats_empty", lang), parse_mode=None)
        return
    await message.answer(t("stats", lang, **dict(row)), parse_mode=None)


# ── 📜 History ────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_history"]["en"], T["btn_history"]["ru"]}))
@dp.message(Command("history"))
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
        await message.answer(t("history_empty", lang), parse_mode=None)
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
    if len(text) > 4000:
        text = text[:4000] + "\n\\.\\.\\."
    await message.answer(text, parse_mode=None)


# ── 📆 Weekly ─────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_week"]["en"], T["btn_week"]["ru"]}))
@dp.message(Command("weekly"))
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
    await message.answer(text, parse_mode=None)


# ── 👤 Profile ────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_profile"]["en"], T["btn_profile"]["ru"]}))
@dp.message(Command("profile"))
async def handle_profile(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        joined = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE user_id=$1", uid
        )
        spent = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id=$1", uid
        )
        wins = await conn.fetchval(
            "SELECT COUNT(*) FROM draw_history WHERE winner_telegram_id=$1", uid
        )
        winnings = await conn.fetchval(
            "SELECT COALESCE(SUM(winner_prize), 0) FROM draw_history WHERE winner_telegram_id=$1",
            uid,
        )
        user_row = await conn.fetchrow(
            "SELECT referral_count, free_tickets FROM users WHERE telegram_id=$1", uid
        )
    await message.answer(
        t("profile_info", lang,
          joined=joined, wins=wins,
          winnings=esc(f"{float(winnings):.2f}"), spent=esc(f"{float(spent):.2f}"),
          referrals=user_row["referral_count"] if user_row else 0,
          free=user_row["free_tickets"] if user_row else 0),
    )


# ── 🔗 Invite ─────────────────────────────────────────────────
@dp.message(F.text.in_({T["btn_invite"]["en"], T["btn_invite"]["ru"]}))
async def handle_invite(message: Message) -> None:
    uid  = message.from_user.id
    lang = await get_lang(uid)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT referral_count, free_tickets FROM users WHERE telegram_id=$1", uid
        )
    bot_me = await bot.get_me()
    link = f"https://t.me/{bot_me.username}?start=ref_{uid}"
    await message.answer(
        t("invite_info", lang,
          link=link,
          count=row["referral_count"] if row else 0,
          free=row["free_tickets"] if row else 0,
          goal=REFERRALS_PER_FREE_TICKET),
    )


# ── /free_ticket ──────────────────────────────────────────────
@dp.message(Command("free_ticket"))
async def cmd_free_ticket(message: Message) -> None:
    uid   = message.from_user.id
    lang  = await get_lang(uid)
    uname = message.from_user.username or f"user_{uid}"
    async with db_pool.acquire() as conn:
        credits = await conn.fetchval("SELECT free_tickets FROM users WHERE telegram_id=$1", uid)
        if not credits:
            await message.answer(t("free_ticket_none", lang))
            return
        if await conn.fetchval("SELECT 1 FROM participants WHERE telegram_id=$1", uid):
            await message.answer(t("txid_already_in", lang))
            return
        count, paid = await get_pool_counts(conn)
        if count >= PARTICIPANT_LIMIT:
            await message.answer(t("round_full", lang, limit=PARTICIPANT_LIMIT))
            return
        ticket = (await conn.fetchval(
            "SELECT COALESCE(MAX(ticket_number), 0) FROM participants"
        )) + 1
        try:
            await conn.execute(
                "UPDATE users SET free_tickets = free_tickets - 1 WHERE telegram_id=$1", uid
            )
            await conn.execute(
                "INSERT INTO participants (ticket_number, telegram_id, username, is_free)"
                " VALUES ($1,$2,$3,TRUE)",
                ticket, uid, f"@{uname}",
            )
            # amount=0 synthetic record, kept only so /profile can count this
            # round towards "rounds joined" — never matched by payment logic
            # (real TXIDs always start with 0x).
            await conn.execute(
                "INSERT INTO transactions (txid, user_id, username, amount) VALUES ($1,$2,$3,0)",
                f"free_{uid}_{ticket}_{int(time.time())}", uid, uname,
            )
            new_count = count + 1
        except asyncpg.UniqueViolationError:
            await message.answer(t("txid_already_in", lang))
            return

    # a free ticket is an extra shot at winning — it does not add real USDT
    # to the payable bank, so the bank shown here is unchanged (paid-only).
    await message.answer(
        t("free_ticket_used", lang,
          ticket=ticket, bank=paid * ENTRY_FEE, count=new_count)
    )
    log.info("🎁 Free ticket #%d issued to @%s (pool %d/%d)",
             ticket, uname, new_count, PARTICIPANT_LIMIT)

    if new_count >= PARTICIPANT_LIMIT and not draw_lock.locked():
        asyncio.create_task(run_full_draw())


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
        for tbl in ("participants", "transactions", "draw_history", "referral_sources", "pending_payments"):
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
            parse_mode=None,
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://cryptofortuna.online", "https://www.cryptofortuna.online"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/app", StaticFiles(directory="static", html=True), name="webapp")


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


@app.get("/stats")
async def public_stats():
    """Public read-only stats for the landing page live counters."""
    async with db_pool.acquire() as conn:
        current_count, current_paid = await get_pool_counts(conn)
        agg = await conn.fetchrow("""
            SELECT COUNT(*)                            AS draws,
                   COALESCE(SUM(participants_count), 0) AS participants,
                   COALESCE(SUM(commission), 0)         AS commission
            FROM draw_history
        """)
    return {
        "current_participants": current_count,
        "current_limit": PARTICIPANT_LIMIT,
        "current_bank": current_paid * ENTRY_FEE,
        "total_draws": agg["draws"],
        "total_participants": agg["participants"] + current_count,
        "total_commission": float(agg["commission"]),
        "entry_fee": ENTRY_FEE,
        "wallet_address": WALLET_ADDRESS,
    }


@app.get("/api/history")
async def public_history():
    """Last 20 completed draws — real, verifiable results for the website."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT round_number, draw_date, participants_count, total_bank,
                   winner_username, winner_ticket, winner_prize, target_block
            FROM draw_history ORDER BY draw_date DESC LIMIT 20
        """)
    return [
        {
            "round": r["round_number"],
            "date": r["draw_date"].isoformat() if r["draw_date"] else None,
            "participants": r["participants_count"],
            "bank": r["total_bank"],
            "winner": r["winner_username"],
            "ticket": r["winner_ticket"],
            "prize": r["winner_prize"],
            "block": r["target_block"],
        }
        for r in rows
    ]


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
    asyncio.create_task(payment_watcher())
    asyncio.create_task(abandoned_payment_reminder())
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
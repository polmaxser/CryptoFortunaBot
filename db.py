import sqlite3

conn = sqlite3.connect("crypto_fortuna.db")
c = conn.cursor()

# таблицы
c.execute('''CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT
)''')

c.execute('''CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT,
    bank REAL,
    winner_id INTEGER
)''')

c.execute('''CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    round_id INTEGER,
    txid TEXT,
    status TEXT
)''')

conn.commit()


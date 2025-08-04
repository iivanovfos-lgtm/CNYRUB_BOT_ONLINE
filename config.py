import os

# --- Tinkoff API ---
# Если переменные заданы в Environment Variables (Render или локально через set/export) — будут взяты оттуда
# Если нет — возьмёт значения, которые ты укажешь ниже

TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN", "t.ТВОЙ_БОЕВОЙ_ТОКЕН")  # боевой токен
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "2183827266")                 # твой account_id
TINKOFF_FIGI = os.getenv("TINKOFF_FIGI", "BBG0013HRTL0")           # FIGI RUB/CNY TOM

# --- Telegram ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "твой_токен_бота")
CHAT_ID = os.getenv("CHAT_ID", "твой_chat_id")

# --- Торговые параметры ---
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 0.1))     # в процентах
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 0.2)) # в процентах

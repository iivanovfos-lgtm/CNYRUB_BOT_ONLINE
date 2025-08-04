import os

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Торговые параметры
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.1"))      # % стоп-лосса
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.2"))  # % тейк-профита

# Tinkoff Invest API
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")   # боевой токен Tinkoff Invest API
TINKOFF_FIGI = os.getenv("TINKOFF_FIGI")     # FIGI для RUB/CNY, например BBG0013HRTL0

# Проверка, что токены загружены
if not TELEGRAM_TOKEN:
    raise ValueError("Ошибка: переменная TELEGRAM_TOKEN не задана")
if not CHAT_ID:
    raise ValueError("Ошибка: переменная CHAT_ID не задана")
if not TINKOFF_TOKEN:
    raise ValueError("Ошибка: переменная TINKOFF_TOKEN не задана")
if not TINKOFF_FIGI:
    raise ValueError("Ошибка: переменная TINKOFF_FIGI не задана")

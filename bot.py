import sys, os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import *
import pandas as pd
import ta
import time
import asyncio
import uuid
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from aiogram.types import FSInputFile
from tinkoff.invest import Client, OrderDirection, OrderType, CandleInterval

# === Настройки торговли из Environment Variables ===
TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))
TRADE_RUB_LIMIT = float(os.getenv("TRADE_RUB_LIMIT", 10000))
MIN_POSITION_THRESHOLD = 0.5  # Минимум CNY, чтобы считать позицию открытой

moscow_tz = pytz.timezone("Europe/Moscow")
current_position = None
entry_price = None

# ===== Получаем остатки прямо из API =====
def get_balances():
    """Возвращает баланс RUB и CNY."""
    rub_balance = 0
    cny_balance = 0
    with Client(TINKOFF_TOKEN) as client:
        positions = client.operations.get_positions(account_id=ACCOUNT_ID)
        for cur in positions.money:
            if cur.currency == "rub":
                rub_balance = float(cur.units)
            elif cur.currency == "cny":
                cny_balance = float(cur.units)
    return rub_balance, cny_balance

# ===== Загрузка истории цен =====
def load_initial_prices():
    try:
        with Client(TINKOFF_TOKEN) as client:
            now = datetime.now(pytz.UTC)
            candles = client.market_data.get_candles(
                figi=TINKOFF_FIGI,
                from_=now - timedelta(hours=1),
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN
            )
            return [c.close.units + c.close.nano / 1e9 for c in candles.candles]
    except:
        return []

# ===== Получение текущей цены =====
def get_price():
    try:
        with Client(TINKOFF_TOKEN) as client:
            now = datetime.now(pytz.UTC)
            candles = client.market_data.get_candles(
                figi=TINKOFF_FIGI,
                from_=now - timedelta(minutes=5),
                to=now,
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN
            )
            if not candles.candles:
                return None
            last = candles.candles[-1]
            return last.close.units + last.close.nano / 1e9
    except:
        return None

# ===== Генерация сигнала =====
def generate_signal(prices):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]
    ema5, ema20, rsi = last["ema_fast"], last["ema_slow"], last["rsi"]

    if pd.notna(ema5) and pd.notna(ema20):
        if ema5 > ema20 and rsi < 70:
            return "BUY", df, "восходящий тренд", ema5, ema20, rsi
        elif ema5 < ema20 and rsi > 30:
            return "SELL", df, "нисходящий тренд", ema5, ema20, rsi
    return "HOLD", df, "нет тренда", ema5, ema20, rsi

# ===== Построение графика =====
def plot_chart(df, signal, price):
    if len(df) < 20:
        return
    os.makedirs("charts_currency", exist_ok=True)
    plt.figure(figsize=(8, 4))
    plt.plot(df["close"], label="Цена", color="black")
    plt.plot(df["ema_fast"], label="EMA(5)", color="blue")
    plt.plot(df["ema_slow"], label="EMA(20)", color="red")
    if signal == "BUY":
        plt.scatter(len(df) - 1, price, color="green")
    elif signal == "SELL":
        plt.scatter(len(df) - 1, price, color="red")
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig("charts_currency/chart.png")
    plt.close()

# ===== Telegram уведомления =====
async def send_chart(signal, price, reason, ema5, ema20, rsi):
    bot = Bot(token=TELEGRAM_TOKEN)
    if os.path.exists("charts_currency/chart.png"):
        photo = FSInputFile("charts_currency/chart.png")
        await bot.send_photo(
            CHAT_ID, photo,
            caption=f"[RUB/CNY] {signal} @ {price:.5f}\nПричина: {reason}\nEMA5: {ema5:.5f} | EMA20: {ema20:.5f} | RSI: {rsi:.2f}"
        )
    else:
        await bot.send_message(CHAT_ID, f"[RUB/CNY] {signal} @ {price:.5f}")
    await bot.session.close()

async def notify_order_rejected(reason):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, f"[RUB/CNY] ⚠️ Ордер отклонён!\nПричина: {reason}")
    await bot.session.close()

# ===== Отправка ордера =====
def place_market_order(direction, current_price):
    rub_balance, cny_balance = get_balances()
    trade_amount_rub = current_price * TRADE_LOTS

    # BUY — покупаем только если хватает RUB
    if direction == "BUY":
        if cny_balance > MIN_POSITION_THRESHOLD:
            print(f"[INFO] Уже есть {cny_balance} CNY — новый BUY не нужен")
            return None
        if trade_amount_rub > TRADE_RUB_LIMIT:
            print(f"[INFO] Сделка на {trade_amount_rub:.2f} ₽ превышает лимит")
            return None
        if trade_amount_rub > rub_balance:
            print("[INFO] Недостаточно RUB для покупки")
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_BUY
        qty = TRADE_LOTS

    # SELL — продаём только если хватает CNY
    elif direction == "SELL":
        if cny_balance < TRADE_LOTS:
            print(f"[INFO] Недостаточно CNY для продажи ({cny_balance})")
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_SELL
        qty = int(cny_balance)

    else:
        return None

    # Отправка ордера
    with Client(TINKOFF_TOKEN) as client:
        try:
            resp = client.orders.post_order(
                figi=TINKOFF_FIGI,
                quantity=qty,
                direction=order_dir,
                account_id=ACCOUNT_ID,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=str(uuid.uuid4())
            )
            if resp.execution_report_status.name != "EXECUTION_REPORT_STATUS_FILL":
                asyncio.run(notify_order_rejected(str(resp)))
                return None
            return resp
        except Exception as e:
            asyncio.run(notify_order_rejected(str(e)))
            return None

# ===== Основной цикл =====
def main():
    global current_position, entry_price
    prices = load_initial_prices()
    first_run = True

    while True:
        price = get_price()
        if price is None:
            time.sleep(60)
            continue

        prices.append(price)
        if len(prices) > 60:
            prices = prices[-60:]

        signal, df, reason, ema5, ema20, rsi = generate_signal(prices)
        plot_chart(df, signal, price)

        if first_run:
            asyncio.run(send_chart(f"🚀 Стартовый сигнал {signal}", price, reason, ema5, ema20, rsi))
            first_run = False

        if signal in ["BUY", "SELL"] and signal != current_position:
            resp = place_market_order(signal, price)
            if resp:
                current_position = signal if signal == "BUY" else None
                entry_price = price
                asyncio.run(send_chart(f"🟢 Открыта {signal}", price, reason, ema5, ema20, rsi))

        time.sleep(60)

if __name__ == "__main__":
    main()

import os
import pandas as pd
import ta
import time
import asyncio
import csv
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from aiogram.types import FSInputFile
from tinkoff.invest import Client, OrderDirection, OrderType, CandleInterval, StopOrderDirection, StopOrderExpirationType, StopOrderType
from config import TELEGRAM_TOKEN, CHAT_ID, STOP_LOSS_PCT, TAKE_PROFIT_PCT, TINKOFF_TOKEN, TINKOFF_FIGI, ACCOUNT_ID

# Московское время
moscow_tz = pytz.timezone("Europe/Moscow")

# Торговый объём
LOT_SIZE = 1  # минимальный размер лота RUB/CNY

# Текущая позиция
current_position = None
entry_price = None

# ===== Получение цены =====
def get_rub_cny_price():
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
            last_candle = candles.candles[-1]
            return last_candle.close.units + last_candle.close.nano / 1e9
    except Exception as e:
        print(f"[Ошибка получения цены] {e}")
        return None

# ===== Генерация сигнала =====
def generate_signal(prices: list):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]
    if pd.notna(last["ema_fast"]) and pd.notna(last["ema_slow"]):
        if last["ema_fast"] > last["ema_slow"] and last["rsi"] < 70:
            return "BUY", df
        elif last["ema_fast"] < last["ema_slow"] and last["rsi"] > 30:
            return "SELL", df
    return "HOLD", df

# ===== Выставление рыночного ордера =====
def place_market_order(direction: str, quantity: int):
    with Client(TINKOFF_TOKEN) as client:
        dir_enum = OrderDirection.ORDER_DIRECTION_BUY if direction == "BUY" else OrderDirection.ORDER_DIRECTION_SELL
        order = client.orders.post_order(
            figi=TINKOFF_FIGI,
            quantity=quantity,
            direction=dir_enum,
            account_id=ACCOUNT_ID,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=f"bot-order-{datetime.now().timestamp()}"
        )
        return order

# ===== Выставление стоп-ордеров (SL/TP) =====
def place_stop_orders(entry_price, direction):
    with Client(TINKOFF_TOKEN) as client:
        if direction == "BUY":
            stop_loss_price = entry_price * (1 - STOP_LOSS_PCT / 100)
            take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_SELL
        else:  # SELL
            stop_loss_price = entry_price * (1 + STOP_LOSS_PCT / 100)
            take_profit_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)
            stop_dir = StopOrderDirection.STOP_ORDER_DIRECTION_BUY

        # Stop Loss
        client.stop_orders.post_stop_order(
            figi=TINKOFF_FIGI,
            quantity=LOT_SIZE,
            price=stop_loss_price,
            stop_price=stop_loss_price,
            direction=stop_dir,
            account_id=ACCOUNT_ID,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LIMIT
        )

        # Take Profit
        client.stop_orders.post_stop_order(
            figi=TINKOFF_FIGI,
            quantity=LOT_SIZE,
            price=take_profit_price,
            stop_price=take_profit_price,
            direction=stop_dir,
            account_id=ACCOUNT_ID,
            expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT
        )

# ===== Журнал сделок =====
def log_trade(action, price, profit=None):
    with open("trades.csv", "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(moscow_tz).strftime("%Y-%m-%d %H:%M:%S"),
            action, price, profit
        ])

# ===== График =====
def plot_chart(df, signal, price):
    os.makedirs("charts", exist_ok=True)
    plt.figure(figsize=(8, 4))
    plt.plot(df["close"], label="Цена", color="black")
    plt.plot(df["ema_fast"], label="EMA(5)", color="blue")
    plt.plot(df["ema_slow"], label="EMA(20)", color="red")
    if signal == "BUY":
        plt.scatter(len(df) - 1, price, color="green", label="BUY")
    elif signal == "SELL":
        plt.scatter(len(df) - 1, price, color="red", label="SELL")
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig("charts/chart.png")
    plt.close()

# ===== Telegram =====
async def send_signal_with_chart(signal, price):
    bot = Bot(token=TELEGRAM_TOKEN)
    photo = FSInputFile("charts/chart.png")
    await bot.send_photo(CHAT_ID, photo, caption=f"{signal} @ {price:.5f}")
    await bot.session.close()

async def send_telegram_message(text):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, text)
    await bot.session.close()

# ===== Основной цикл =====
def main():
    global current_position, entry_price
    prices = []
    first_run = True

    while True:
        price = get_rub_cny_price()
        if price is None:
            time.sleep(60)
            continue

        prices.append(price)
        if len(prices) > 60:
            prices = prices[-60:]

        signal, df = generate_signal(prices)
        plot_chart(df, signal, price)

        if first_run:
            asyncio.run(send_signal_with_chart(f"🚀 Стартовый сигнал {signal}", price))
            first_run = False

        # Автоторговля
        if signal in ["BUY", "SELL"] and signal != current_position:
            # Закрываем старую позицию
            if current_position is not None:
                current_position = None

            # Открываем новую
            place_market_order(signal, LOT_SIZE)
            entry_price = price
            current_position = signal
            log_trade(f"OPEN {signal}", price)
            asyncio.run(send_telegram_message(f"🟢 Открыта {signal} @ {price:.5f}"))

            # Ставим реальные SL и TP
            place_stop_orders(entry_price, signal)
            asyncio.run(send_telegram_message(
                f"📌 Stop Loss: {entry_price * (1 - STOP_LOSS_PCT / 100 if signal == 'BUY' else 1 + STOP_LOSS_PCT / 100):.5f}\n"
                f"📌 Take Profit: {entry_price * (1 + TAKE_PROFIT_PCT / 100 if signal == 'BUY' else 1 - TAKE_PROFIT_PCT / 100):.5f}"
            ))

        time.sleep(60)

if __name__ == "__main__":
    main()

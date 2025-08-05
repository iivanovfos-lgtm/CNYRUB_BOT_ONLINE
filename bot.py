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

TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))
TRADE_RUB_LIMIT = float(os.getenv("TRADE_RUB_LIMIT", 10000))
MIN_POSITION_THRESHOLD = 0.5

moscow_tz = pytz.timezone("Europe/Moscow")
current_position = None
entry_price = None

def get_balances():
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

async def send_debug_message(text):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, f"🛠 DEBUG:\n{text}")
    await bot.session.close()

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

async def notify_order_rejected(reason):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, f"[RUB/CNY] ⚠️ Ордер отклонён!\nПричина: {reason}")
    await bot.session.close()

def place_market_order(direction, current_price):
    rub_balance, cny_balance = get_balances()
    trade_amount_rub = current_price * TRADE_LOTS

    debug_text = (
        f"Направление: {direction}\n"
        f"RUB баланс: {rub_balance:.2f}\n"
        f"CNY баланс: {cny_balance:.2f}\n"
        f"Лоты на сделку: {TRADE_LOTS}\n"
        f"Стоимость сделки: {trade_amount_rub:.2f} RUB\n"
        f"Лимит сделки: {TRADE_RUB_LIMIT:.2f} RUB"
    )
    print(debug_text)
    asyncio.run(send_debug_message(debug_text))

    if direction == "BUY":
        if cny_balance > MIN_POSITION_THRESHOLD:
            return None
        if trade_amount_rub > TRADE_RUB_LIMIT or trade_amount_rub > rub_balance:
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_BUY
        qty = TRADE_LOTS

    elif direction == "SELL":
        if cny_balance < MIN_POSITION_THRESHOLD:
            return None
        # 🔹 Продаём только то, что есть, не больше
        qty = min(int(cny_balance), TRADE_LOTS)
        if qty < 1:
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_SELL

    else:
        return None

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

        if first_run:
            asyncio.run(send_debug_message(f"🚀 Стартовый сигнал {signal} @ {price:.5f}"))
            first_run = False

        if signal in ["BUY", "SELL"] and signal != current_position:
            resp = place_market_order(signal, price)
            if resp:
                current_position = signal if signal == "BUY" else None
                entry_price = price
                asyncio.run(send_debug_message(f"🟢 Открыта {signal} @ {price:.5f}"))

        time.sleep(60)

if __name__ == "__main__":
    main()

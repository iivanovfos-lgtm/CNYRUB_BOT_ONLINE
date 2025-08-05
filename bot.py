import sys, os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import *
import pandas as pd
import ta
import time
import asyncio
import uuid
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from tinkoff.invest import Client, OrderDirection, OrderType, CandleInterval

# === Настройки ===
TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))
TRADE_RUB_LIMIT = float(os.getenv("TRADE_RUB_LIMIT", 10000))
LOT_SIZE_CNY = 1000  # 1 лот = 1000 CNY
TP_PERCENT = 0.3     # Take Profit %
SL_PERCENT = 0.2     # Stop Loss %
BROKER_FEE = 0.003   # 0.3% комиссия брокера
MIN_POSITION_THRESHOLD = 0.5

moscow_tz = pytz.timezone("Europe/Moscow")

current_position = None
entry_price = None
take_profit_price = None
stop_loss_price = None

# ===== Получаем остатки =====
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

# ===== Telegram =====
async def send_message(text):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(CHAT_ID, text)
    await bot.session.close()

async def notify_order_rejected(reason):
    await send_message(f"[RUB/CNY] ⚠️ Ордер отклонён!\nПричина: {reason}")

# ===== Цены =====
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

# ===== Сигналы =====
def generate_signal(prices):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]
    ema5, ema20, rsi = last["ema_fast"], last["ema_slow"], last["rsi"]

    if pd.notna(ema5) and pd.notna(ema20):
        if ema5 > ema20 and rsi < 70:
            return "BUY", "восходящий тренд", ema5, ema20, rsi
        elif ema5 < ema20 and rsi > 30:
            return "SELL", "нисходящий тренд", ema5, ema20, rsi
    return "HOLD", "нет тренда", ema5, ema20, rsi

# ===== Ордера =====
def place_market_order(direction, current_price):
    rub_balance, cny_balance = get_balances()

    cny_lots = int(cny_balance // LOT_SIZE_CNY)
    buy_cny_qty = TRADE_LOTS * LOT_SIZE_CNY
    trade_amount_rub = current_price * buy_cny_qty

    if direction == "BUY":
        if cny_balance >= LOT_SIZE_CNY:
            return None
        if trade_amount_rub > TRADE_RUB_LIMIT or trade_amount_rub > rub_balance:
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_BUY
        qty = TRADE_LOTS

    elif direction == "SELL":
        if cny_balance < LOT_SIZE_CNY:
            return None
        qty = min(cny_lots, TRADE_LOTS)
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

# ===== Основной цикл =====
def main():
    global current_position, entry_price, take_profit_price, stop_loss_price
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

        signal, reason, ema5, ema20, rsi = generate_signal(prices)

        # === Проверка TP/SL ===
        if current_position == "BUY":
            if price >= take_profit_price:
                asyncio.run(send_message(f"[RUB/CNY] 🎯 Take Profit достигнут @ {price:.5f}"))
                place_market_order("SELL", price)
                current_position = None
                continue
            elif price <= stop_loss_price:
                asyncio.run(send_message(f"[RUB/CNY] 🛑 Stop Loss достигнут @ {price:.5f}"))
                place_market_order("SELL", price)
                current_position = None
                continue

        # === Новый вход ===
        if first_run:
            asyncio.run(send_message(f"🚀 Стартовый сигнал {signal} @ {price:.5f}"))
            first_run = False

        if signal == "BUY" and current_position != "BUY":
            resp = place_market_order("BUY", price)
            if resp:
                current_position = "BUY"
                entry_price = price

                # === Цена входа с учётом комиссии брокера (на покупку и продажу) ===
                total_fee = BROKER_FEE * 2  # покупка + продажа
                entry_price_with_fee = entry_price * (1 + total_fee)

                take_profit_price = entry_price_with_fee * (1 + TP_PERCENT / 100)
                stop_loss_price = entry_price_with_fee * (1 - SL_PERCENT / 100)

                asyncio.run(send_message(
                    f"[RUB/CNY] 🟢 Открыта BUY @ {price:.5f}\n"
                    f"TP: {take_profit_price:.5f} | SL: {stop_loss_price:.5f} (учтена комиссия {BROKER_FEE*100:.2f}% с каждой сделки)"
                ))

        elif signal == "SELL" and current_position == "BUY":
            asyncio.run(send_message(f"[RUB/CNY] 📉 Тренд развернулся — SELL @ {price:.5f}"))
            place_market_order("SELL", price)
            current_position = None

        time.sleep(60)

if __name__ == "__main__":
    main()

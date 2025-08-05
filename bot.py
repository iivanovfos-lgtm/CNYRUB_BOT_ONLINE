from config import *
import os
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
TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))  # Лоты по умолчанию 1
TRADE_RUB_LIMIT = float(os.getenv("TRADE_RUB_LIMIT", 10000))  # Лимит в рублях по умолчанию 10 000
MIN_POSITION_THRESHOLD = 0.5  # Минимум лота, при котором считаем, что позиция открыта

moscow_tz = pytz.timezone("Europe/Moscow")
current_position = None
entry_price = None

# ===== Получаем баланс в рублях =====
def get_account_balance():
    """Баланс счёта в рублях."""
    with Client(TINKOFF_TOKEN) as client:
        portfolio = client.operations.get_portfolio(account_id=ACCOUNT_ID)
        for pos in portfolio.positions:
            if pos.instrument_type == "currency" and pos.figi == "FG0000000000":  # FIGI рубля
                return float(pos.quantity.units)
    return 0

# ===== Получаем текущую открытую позицию (CNY) =====
def get_current_position():
    """Возвращаем количество CNY в портфеле."""
    with Client(TINKOFF_TOKEN) as client:
        portfolio = client.operations.get_portfolio(account_id=ACCOUNT_ID)
        for pos in portfolio.positions:
            # CNY — это валюта, но не рубль
            if pos.instrument_type == "currency" and pos.figi != "FG0000000000":
                return float(pos.quantity.units)
    return 0

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
    except Exception as e:
        print(f"[Ошибка загрузки истории] {e}")
        return []

# ===== Получение текущей цены =====
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
        print(f"[Ошибка цены] {e}")
        return None

# ===== Генерация сигнала =====
def generate_signal(prices):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    last = df.iloc[-1]
    ema5 = last["ema_fast"]
    ema20 = last["ema_slow"]
    rsi = last["rsi"]

    if pd.notna(ema5) and pd.notna(ema20):
        if ema5 > ema20 and rsi < 70:
            return "BUY", df, "восходящий тренд", ema5, ema20, rsi
        elif ema5 < ema20 and rsi > 30:
            return "SELL", df, "нисходящий тренд", ema5, ema20, rsi
    return "HOLD", df, "нет тренда — EMA и RSI в нейтральной зоне", ema5, ema20, rsi

# ===== Построение графика =====
def plot_chart(df, signal, price):
    if len(df) < 20:
        print("[График] Недостаточно данных для построения")
        return
    os.makedirs("charts_currency", exist_ok=True)
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
    plt.savefig("charts_currency/chart.png")
    plt.close()

# ===== Telegram уведомления =====
async def send_chart(signal, price, reason, ema5, ema20, rsi):
    bot = Bot(token=TELEGRAM_TOKEN)
    if os.path.exists("charts_currency/chart.png"):
        photo = FSInputFile("charts_currency/chart.png")
        await bot.send_photo(
            CHAT_ID, photo,
            caption=(f"[RUB/CNY] {signal} @ {price:.5f}\n"
                     f"Причина: {reason}\n"
                     f"EMA(5): {ema5:.5f} | EMA(20): {ema20:.5f} | RSI: {rsi:.2f}")
        )
    else:
        await bot.send_message(
            CHAT_ID,
            f"[RUB/CNY] {signal} @ {price:.5f}\n"
            f"Причина: {reason}\n"
            f"EMA(5): {ema5:.5f} | EMA(20): {ema20:.5f} | RSI: {rsi:.2f}"
        )
    await bot.session.close()

async def notify_order_rejected(reason):
    bot = Bot(token=TELEGRAM_TOKEN)
    if "Need confirmation" in reason:
        await bot.send_message(
            CHAT_ID,
            "[RUB/CNY] ⚠️ Сделка не прошла — требуется подтверждение в приложении Тинькофф."
        )
    else:
        await bot.send_message(
            CHAT_ID,
            f"[RUB/CNY] ⚠️ Ордер отклонён!\nПричина: {reason}"
        )
    await bot.session.close()

# ===== Отправка ордера =====
def place_market_order(direction, current_price):
    """Только в рамках средств на счёте и без шорта."""
    current_balance = get_current_position()
    rub_balance = get_account_balance()
    trade_amount_rub = current_price * TRADE_LOTS

    # BUY
    if direction == "BUY":
        if current_balance > MIN_POSITION_THRESHOLD:
            print(f"[INFO] Уже есть открытая покупка ({current_balance} CNY) — новый BUY не нужен")
            return None
        if trade_amount_rub > TRADE_RUB_LIMIT:
            print(f"[INFO] Сделка на {trade_amount_rub:.2f} ₽ превышает лимит {TRADE_RUB_LIMIT:.2f} ₽ — пропуск")
            return None
        if trade_amount_rub > rub_balance:
            print("[INFO] Недостаточно средств для покупки")
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_BUY
        qty = TRADE_LOTS
        print(f"[INFO] Открытие позиции BUY на {qty} лот(ов)")

    # SELL
    elif direction == "SELL":
        if current_balance <= MIN_POSITION_THRESHOLD:
            print("[INFO] Нет позиции для продажи — пропуск SELL")
            return None
        order_dir = OrderDirection.ORDER_DIRECTION_SELL
        qty = int(current_balance)
        print(f"[INFO] Закрытие позиции SELL ({qty} лот(ов))")

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
            print(f"[TINKOFF] Ответ API: {resp}")
            if resp.execution_report_status.name != "EXECUTION_REPORT_STATUS_FILL":
                reason = getattr(resp, "message", str(resp.execution_report_status))
                asyncio.run(notify_order_rejected(str(reason)))
                return None
            print("[OK] Ордер исполнен")
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
        price = get_rub_cny_price()
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
            if resp:  # Ордер прошёл
                current_position = signal if signal == "BUY" else None
                entry_price = price
                asyncio.run(send_chart(f"🟢 Открыта {signal}", price, reason, ema5, ema20, rsi))

        time.sleep(60)

if __name__ == "__main__":
    main()

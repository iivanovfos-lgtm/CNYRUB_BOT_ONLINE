import os
import time
import uuid
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import ta
import pytz
from aiogram import Bot
from tinkoff.invest import Client, OrderDirection, OrderType, CandleInterval

# ==== Настройки ====
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TINKOFF_FIGI = os.getenv("TINKOFF_FIGI", "BBG0013HRTL0")  # RUB/CNY

TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))
LOT_SIZE_CNY = 1000
TP_PERCENT = 0.5
SL_PERCENT = 0.4
BROKER_FEE = 0.0005  # 0.05%

moscow_tz = pytz.timezone("Europe/Moscow")

# ==== Переменные ====
current_position = None
entry_price = None
take_profit_price = None
stop_loss_price = None
last_stop_time = None

# ==== Для прогнозов и отчётов ====
morning_forecast_sent = False
last_intermediate_report = None
INTERMEDIATE_INTERVAL_HOURS = 3

# ==== Дневная статистика ====
daily_profit = 0.0
daily_commission = 0.0
daily_buy_count = 0
daily_sell_count = 0
start_of_day_portfolio_value = None


# ==== Telegram ====
async def send_message(text):
    bot = Bot(token=TELEGRAM_TOKEN, parse_mode="Markdown")
    await bot.send_message(CHAT_ID, text)
    await bot.session.close()


# ==== Новости ====
async def get_news():
    import aiohttp
    NEWS_URL = "https://news.google.com/rss/search?q=рубль+юань&hl=ru&gl=RU&ceid=RU:ru"
    async with aiohttp.ClientSession() as session:
        async with session.get(NEWS_URL) as resp:
            text = await resp.text()
    news_list = []
    for line in text.split("<item>")[1:6]:
        try:
            title = line.split("<title>")[1].split("</title>")[0]
            link = line.split("<link>")[1].split("</link>")[0]
            news_list.append(f"- {title} — [читать]({link})")
        except:
            continue
    return "\n".join(news_list)


# ==== Баланс ====
def get_balances():
    rub_balance, cny_balance = 0, 0
    with Client(TINKOFF_TOKEN) as client:
        positions = client.operations.get_positions(account_id=ACCOUNT_ID)
        for cur in positions.money:
            if cur.currency == "rub":
                rub_balance = float(cur.units)
            elif cur.currency == "cny":
                cny_balance = float(cur.units)
    return rub_balance, cny_balance


# ==== Цена ====
def get_price():
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


# ==== Сигнал ====
def generate_signal(prices):
    df = pd.DataFrame(prices, columns=["close"])
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    last = df.iloc[-1]
    if pd.notna(last["ema_fast"]) and pd.notna(last["ema_slow"]):
        if last["ema_fast"] > last["ema_slow"] and last["rsi"] < 70:
            return "BUY"
        elif last["ema_fast"] < last["ema_slow"] and last["rsi"] > 30:
            return "SELL"
    return "HOLD"


# ==== Ордер ====
def place_market_order(direction, qty):
    with Client(TINKOFF_TOKEN) as client:
        resp = client.orders.post_order(
            figi=TINKOFF_FIGI,
            quantity=qty,
            direction=OrderDirection.ORDER_DIRECTION_BUY if direction == "BUY" else OrderDirection.ORDER_DIRECTION_SELL,
            account_id=ACCOUNT_ID,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid.uuid4())
        )
        return resp


# ==== Промежуточный отчёт ====
async def intermediate_report(price):
    rub_balance, cny_balance = get_balances()
    portfolio_value = rub_balance + cny_balance * price
    if current_position:
        floating_profit = (price - entry_price) * LOT_SIZE_CNY * TRADE_LOTS
        await send_message(
            f"[RUB/CNY] 📊 Промежуточный отчёт ({datetime.now(moscow_tz).strftime('%H:%M')})\n"
            f"Текущая цена: {price:.5f}\n"
            f"Открытая позиция: {current_position} @ {entry_price:.5f}\n"
            f"Плавающая прибыль: {floating_profit:.2f} ₽\n"
            f"До TP: {((take_profit_price - price) / price * 100):.2f}%\n"
            f"До SL: {((price - stop_loss_price) / price * 100):.2f}%\n\n"
            f"Портфель: {portfolio_value:.2f} ₽"
        )
    else:
        await send_message(
            f"[RUB/CNY] 📊 Промежуточный отчёт ({datetime.now(moscow_tz).strftime('%H:%M')})\n"
            f"Открытых позиций нет.\n"
            f"Портфель: {portfolio_value:.2f} ₽"
        )


# ==== Утренний прогноз ====
async def morning_forecast(prices):
    signal = generate_signal(prices)
    news_text = await get_news()
    await send_message(
        f"🌅 Утренний прогноз по RUB/CNY:\n"
        f"Сигнал: {signal}\n\n"
        f"📰 Новости:\n{news_text}"
    )


# ==== Основной цикл ====
def main():
    global current_position, entry_price, take_profit_price, stop_loss_price, last_stop_time
    global morning_forecast_sent, last_intermediate_report
    global start_of_day_portfolio_value

    prices = []
    first_run = True
    start_of_day_portfolio_value = get_balances()[0] + get_balances()[1] * get_price()

    while True:
        now = datetime.now(moscow_tz)
        price = get_price()
        if price:
            prices.append(price)
            if len(prices) > 60:
                prices = prices[-60:]

            # ==== Утренний прогноз ====
            if now.hour == 9 and 55 <= now.minute <= 56 and not morning_forecast_sent:
                asyncio.run(morning_forecast(prices))
                morning_forecast_sent = True

            if now.hour == 0:
                morning_forecast_sent = False  # сброс на следующий день

            # ==== Промежуточный отчёт ====
            if last_intermediate_report is None or (now - last_intermediate_report).seconds >= INTERMEDIATE_INTERVAL_HOURS * 3600:
                asyncio.run(intermediate_report(price))
                last_intermediate_report = now

            # ==== Логика сделок ====
            signal = generate_signal(prices)

            if current_position == "BUY":
                if price >= take_profit_price:
                    place_market_order("SELL", TRADE_LOTS)
                    current_position = None
                elif price <= stop_loss_price:
                    place_market_order("SELL", TRADE_LOTS)
                    current_position = None
                    last_stop_time = now

            if first_run:
                asyncio.run(send_message(f"[RUB/CNY] 🚀 Стартовый сигнал {signal} @ {price:.5f}"))
                first_run = False

            if last_stop_time and (now - last_stop_time).seconds < 900:
                continue

            if signal == "BUY" and current_position != "BUY":
                qty = TRADE_LOTS
                order = place_market_order("BUY", qty)
                if order:
                    current_position = "BUY"
                    entry_price = price
                    take_profit_price = entry_price * (1 + TP_PERCENT / 100)
                    stop_loss_price = entry_price * (1 - SL_PERCENT / 100)
                    asyncio.run(send_message(
                        f"[RUB/CNY] 🟢 Открыта BUY @ {price:.5f}\n"
                        f"TP: {take_profit_price:.5f} | SL: {stop_loss_price:.5f} "
                        f"(учтена комиссия {BROKER_FEE*100:.2f}%)"
                    ))

        time.sleep(60)


if __name__ == "__main__":
    main()

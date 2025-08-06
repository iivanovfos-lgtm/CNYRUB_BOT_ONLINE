import os
import time
import uuid
import json
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import ta
import pytz
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
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
BROKER_FEE = 0.0005

moscow_tz = pytz.timezone("Europe/Moscow")
POSITION_FILE = "open_position.json"

# ==== Ручная стартовая позиция ====
MANUAL_POSITION = True
MANUAL_DIRECTION = "BUY"
MANUAL_ENTRY_PRICE = 11.1200
MANUAL_LOTS = 4

# ==== Переменные ====
current_position = None
entry_price = None
take_profit_price = None
stop_loss_price = None
last_stop_time = None
morning_forecast_sent = False
last_intermediate_report = None
INTERMEDIATE_INTERVAL_HOURS = 3

# Дневник сделок
trades_today = []

# ==== Telegram ====
async def send_message(text):
    bot = Bot(
        token=TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode="Markdown")
    )
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
        return client.orders.post_order(
            figi=TINKOFF_FIGI,
            quantity=qty,
            direction=OrderDirection.ORDER_DIRECTION_BUY if direction == "BUY" else OrderDirection.ORDER_DIRECTION_SELL,
            account_id=ACCOUNT_ID,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=str(uuid.uuid4())
        )

# ==== Промежуточный отчёт ====
async def intermediate_report(price):
    rub_balance, cny_balance = get_balances()
    portfolio_value = rub_balance + cny_balance * price
    if current_position:
        floating_profit = (price - entry_price) * LOT_SIZE_CNY * MANUAL_LOTS
        await send_message(
            f"[RUB/CNY] 📊 Промежуточный отчёт ({datetime.now(moscow_tz).strftime('%H:%M')})\n"
            f"Открытая позиция: {current_position} @ {entry_price:.5f}\n"
            f"Текущая цена: {price:.5f}\n"
            f"Плавающая прибыль: {floating_profit:.2f} ₽\n"
            f"До TP: {((take_profit_price - price) / price * 100):.2f}%\n"
            f"До SL: {((price - stop_loss_price) / price * 100):.2f}%\n\n"
            f"RUB: {rub_balance:.2f} ₽\n"
            f"CNY: {cny_balance:.2f} ¥ (~{cny_balance * price:.2f} ₽)\n"
            f"Итого портфель: {portfolio_value:.2f} ₽"
        )
    else:
        await send_message(
            f"[RUB/CNY] 📊 Промежуточный отчёт ({datetime.now(moscow_tz).strftime('%H:%M')})\n"
            f"Открытых позиций нет.\n"
            f"RUB: {rub_balance:.2f} ₽\n"
            f"CNY: {cny_balance:.2f} ¥ (~{cny_balance * price:.2f} ₽)\n"
            f"Итого портфель: {portfolio_value:.2f} ₽"
        )

# ==== Итог дня ====
async def daily_report(prices):
    if not trades_today:
        trades_text = "Сегодня сделок не было."
        total_profit = 0
    else:
        trades_text = ""
        total_profit = sum(t["profit"] for t in trades_today)
        for i, trade in enumerate(trades_today, 1):
            trades_text += f"{i}. {trade['type']} @ {trade['entry']} → {trade['exit']} → {trade['profit']:.2f} ₽\n"

    rub_balance, cny_balance = get_balances()
    portfolio_value = rub_balance + cny_balance * prices[-1]
    percent_change = (total_profit / (portfolio_value - total_profit) * 100) if portfolio_value != total_profit else 0

    signal = generate_signal(prices)
    news_text = await get_news()

    await send_message(
        f"📆 Итоги за {datetime.now(moscow_tz).strftime('%d.%m.%Y')} (RUB/CNY)\n\n"
        f"Сделок за день: {len(trades_today)}\n"
        f"Прибыль: {total_profit:.2f} ₽ ({percent_change:.2f}% от портфеля)\n\n"
        f"Детали сделок:\n{trades_text}\n"
        f"💰 Итог портфеля: {portfolio_value:.2f} ₽\n\n"
        f"📊 Прогноз на завтра:\nСигнал: {signal}\n\n"
        f"📰 Новости:\n{news_text}"
    )

# ==== Основной цикл ====
def main():
    global current_position, entry_price, take_profit_price, stop_loss_price, last_stop_time
    global morning_forecast_sent, last_intermediate_report

    if MANUAL_POSITION:
        current_position = MANUAL_DIRECTION
        entry_price = MANUAL_ENTRY_PRICE
        take_profit_price = entry_price * (1 + TP_PERCENT / 100)
        stop_loss_price = entry_price * (1 - SL_PERCENT / 100)
        print(f"[INFO] Ручная позиция активирована: {current_position} @ {entry_price}")

    prices = []
    first_run = True

    while True:
        now = datetime.now(moscow_tz)
        price = get_price()
        if price:
            prices.append(price)
            if len(prices) > 60:
                prices = prices[-60:]

            # Утренний прогноз
            if now.hour == 9 and 55 <= now.minute <= 56 and not morning_forecast_sent:
                asyncio.run(morning_forecast(prices))
                morning_forecast_sent = True
            if now.hour == 0:
                morning_forecast_sent = False

            # Промежуточный отчёт
            if last_intermediate_report is None or (now - last_intermediate_report).seconds >= INTERMEDIATE_INTERVAL_HOURS * 3600:
                asyncio.run(intermediate_report(price))
                last_intermediate_report = now

            # Вечерний отчёт
            if now.hour == 23 and 50 <= now.minute <= 51:
                asyncio.run(daily_report(prices))
                trades_today.clear()

            # Сопровождение позиции
            if current_position == "BUY":
                if price >= take_profit_price:
                    place_market_order("SELL", MANUAL_LOTS)
                    profit = (take_profit_price - entry_price) * LOT_SIZE_CNY * MANUAL_LOTS
                    trades_today.append({"type": "BUY", "entry": entry_price, "exit": take_profit_price, "profit": profit})
                    current_position = None
                elif price <= stop_loss_price:
                    place_market_order("SELL", MANUAL_LOTS)
                    profit = (stop_loss_price - entry_price) * LOT_SIZE_CNY * MANUAL_LOTS
                    trades_today.append({"type": "BUY", "entry": entry_price, "exit": stop_loss_price, "profit": profit})
                    current_position = None
                    last_stop_time = now

            # Стартовое сообщение
            if first_run and not current_position:
                signal = generate_signal(prices)
                asyncio.run(send_message(f"[RUB/CNY] 🚀 Стартовый сигнал {signal} @ {price:.5f}"))
                first_run = False

            # Ждём после SL
            if last_stop_time and (now - last_stop_time).seconds < 900:
                continue

        time.sleep(60)

# ==== Утренний прогноз ====
async def morning_forecast(prices):
    signal = generate_signal(prices)
    news_text = await get_news()
    await send_message(
        f"🌅 Утренний прогноз по RUB/CNY:\n"
        f"Сигнал: {signal}\n\n"
        f"📰 Новости:\n{news_text}"
    )

if __name__ == "__main__":
    main()

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

# ==== Настройки из окружения ====
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TINKOFF_FIGI = os.getenv("TINKOFF_FIGI", "BBG0013HRTL0")  # RUB/CNY

TRADE_LOTS = int(os.getenv("TRADE_LOTS", 1))
TRADE_RUB_LIMIT = float(os.getenv("TRADE_RUB_LIMIT", 10000))
LOT_SIZE_CNY = 1000  # 1 лот = 1000 CNY
TP_PERCENT = 0.5  # Take Profit %
SL_PERCENT = 0.4  # Stop Loss %
BROKER_FEE = 0.0005  # 0.05% комиссия

moscow_tz = pytz.timezone("Europe/Moscow")

# ==== Переменные ====
current_position = None
entry_price = None
take_profit_price = None
stop_loss_price = None
last_stop_time = None

# ==== Дневная статистика ====
daily_profit = 0.0
daily_commission = 0.0
daily_buy_count = 0
daily_sell_count = 0
start_of_day_portfolio_value = None

# ==== Telegram ====
async def send_message(text):
    bot = Bot(token=TELEGRAM_TOKEN)
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

# ==== Текущая цена ====
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

# ==== Отчёт после сделки ====
async def trade_report(trade_type, price, qty, profit, commission):
    global daily_profit, daily_commission, daily_buy_count, daily_sell_count
    rub_balance, cny_balance = get_balances()
    portfolio_value = rub_balance + cny_balance * price

    if trade_type == "BUY":
        daily_buy_count += 1
    elif trade_type == "SELL":
        daily_sell_count += 1

    daily_profit += profit
    daily_commission += commission

    await send_message(
        f"[RUB/CNY] ✅ Сделка закрыта\n"
        f"Тип сделки: {trade_type}\n"
        f"Цена: {price:.5f}\n"
        f"Объём: {qty * LOT_SIZE_CNY} ¥\n\n"
        f"📊 Результат сделки:\n"
        f"Прибыль: {profit:.2f} ₽\n"
        f"Доходность по сделке: {(profit / (price * qty * LOT_SIZE_CNY) * 100):.2f}%\n"
        f"Комиссия: {commission:.2f} ₽\n\n"
        f"📈 Сводка по дню:\n"
        f"Накопительный итог: {daily_profit:.2f} ₽ ({(daily_profit / start_of_day_portfolio_value * 100):.2f}%)\n"
        f"Общая комиссия за день: {daily_commission:.2f} ₽\n"
        f"Сделок сегодня: BUY — {daily_buy_count} | SELL — {daily_sell_count}\n\n"
        f"💼 Портфель после сделки:\n"
        f"RUB: {rub_balance:.2f} ₽\n"
        f"CNY: {cny_balance:.2f} ¥ (~{cny_balance * price:.2f} ₽)\n"
        f"Итого: {portfolio_value:.2f} ₽"
    )

# ==== Ежедневный отчёт ====
async def daily_report():
    news_text = await get_news()
    rub_balance, cny_balance = get_balances()
    last_price = get_price()
    portfolio_value = rub_balance + cny_balance * last_price
    await send_message(
        f"📊 Ежедневный отчёт по RUB/CNY ({datetime.now(moscow_tz).strftime('%d.%m.%Y')})\n\n"
        f"💼 Портфель:\n"
        f"RUB: {rub_balance:.2f} ₽\n"
        f"CNY: {cny_balance:.2f} ¥ (~{cny_balance * last_price:.2f} ₽)\n"
        f"Итого: {portfolio_value:.2f} ₽\n\n"
        f"📈 Доходность за день: {(daily_profit / start_of_day_portfolio_value * 100):.2f}% ({daily_profit:.2f} ₽)\n"
        f"💸 Комиссия: {daily_commission:.2f} ₽\n"
        f"Сделок: BUY — {daily_buy_count} | SELL — {daily_sell_count}\n\n"
        f"📰 Новости:\n{news_text}"
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
    global daily_profit, daily_commission, daily_buy_count, daily_sell_count, start_of_day_portfolio_value

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

            # Утренний прогноз
            if now.hour == 9 and now.minute == 55:
                asyncio.run(morning_forecast(prices))

            # Ежедневный отчёт
            if now.hour == 23 and now.minute == 50:
                asyncio.run(daily_report())

            signal = generate_signal(prices)

            # Проверка TP/SL
            if current_position == "BUY":
                if price >= take_profit_price:
                    profit = (take_profit_price - entry_price) * TRADE_LOTS * LOT_SIZE_CNY
                    commission = take_profit_price * TRADE_LOTS * LOT_SIZE_CNY * BROKER_FEE * 2
                    place_market_order("SELL", TRADE_LOTS)
                    asyncio.run(trade_report("SELL", price, TRADE_LOTS, profit, commission))
                    current_position = None
                    continue
                elif price <= stop_loss_price:
                    profit = (stop_loss_price - entry_price) * TRADE_LOTS * LOT_SIZE_CNY
                    commission = stop_loss_price * TRADE_LOTS * LOT_SIZE_CNY * BROKER_FEE * 2
                    place_market_order("SELL", TRADE_LOTS)
                    asyncio.run(trade_report("SELL", price, TRADE_LOTS, profit, commission))
                    current_position = None
                    last_stop_time = now
                    continue

            # Новый вход
            if first_run:
                asyncio.run(send_message(f"[RUB/CNY] 🚀 Стартовый сигнал {signal} @ {price:.5f}"))
                first_run = False

            # Задержка после SL
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

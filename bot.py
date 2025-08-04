import pandas as pd
import ta
import time
import asyncio
import csv
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import pytz
from aiogram import Bot
from tinkoff.invest import Client, CandleInterval
from xgboost import XGBClassifier
from config import TELEGRAM_TOKEN, CHAT_ID, STOP_LOSS_PCT, TAKE_PROFIT_PCT, TINKOFF_TOKEN, TINKOFF_FIGI

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
                print("[WARNING] Свечи не найдены")
                return None

            last_candle = candles.candles[-1]
            price = last_candle.close.units + last_candle.close.nano / 1e9
            return float(price)
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

# ===== Обучение XGBoost =====
def train_xgb(prices: list):
    if len(prices) < 10:
        return None
    df = pd.DataFrame(prices, columns=["close"])
    df["return"] = df["close"].pct_change()
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["volatility"] = df["return"].rolling(5).std()
    df.dropna(inplace=True)
    X = df[["return", "ema_fast", "ema_slow", "rsi", "volatility"]]
    y = (df["close"].shift(-3) > df["close"]).astype(int).dropna()
    X = X.iloc[:-3]
    if len(y.unique()) < 2:
        return None
    model = XGBClassifier(use_label_encoder=False, eval_metric="logloss")
    model.fit(X, y)
    return model

# ===== Прогноз XGBoost =====
def predict_xgb(model, prices: list):
    if model is None:
        return None
    df = pd.DataFrame(prices, columns=["close"])
    df["return"] = df["close"].pct_change()
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=5)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=20)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["volatility"] = df["return"].rolling(5).std()
    last_row = df.dropna().iloc[-1:][["return", "ema_fast", "ema_slow", "rsi", "volatility"]]
    prob = model.predict_proba(last_row)[0]
    return round(prob[1] * 100, 1), round(prob[0] * 100, 1)

# ===== Сохранение сигнала =====
def save_signal_to_csv(signal, price, sl, tp, forecast, ema5=None, ema20=None, rsi=None):
    with open("signals.csv", mode="a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            price, signal, sl, tp, f"{forecast}",
            ema5, ema20, rsi
        ])

# ===== Построение графика =====
def plot_chart(df, signal, price, sl, tp, forecast):
    plt.figure(figsize=(8, 4))
    plt.plot(df["close"], label="Цена", color="black")
    plt.plot(df["ema_fast"], label="EMA(5)", color="blue")
    plt.plot(df["ema_slow"], label="EMA(20)", color="red")
    if signal == "BUY":
        plt.scatter(len(df) - 1, price, color="green", label="BUY", zorder=5)
    elif signal == "SELL":
        plt.scatter(len(df) - 1, price, color="red", label="SELL", zorder=5)
    plt.axhline(sl, color="orange", linestyle="--", label="Stop Loss")
    plt.axhline(tp, color="purple", linestyle="--", label="Take Profit")
    plt.title(f"RUB/CNY — {signal}", fontsize=14)
    if forecast:
        plt.text(0.02, 0.95, f"Прогноз: рост {forecast[0]}% / падение {forecast[1]}%",
                 transform=plt.gca().transAxes,
                 fontsize=10, bbox=dict(facecolor="white", alpha=0.7))
    plt.legend()
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig("charts/chart.png")
    plt.close()

# ===== Отправка сигнала =====
async def send_signal(signal, price, sl, tp, forecast, ema5=None, ema20=None, rsi=None, is_start=False):
    bot = Bot(token=TELEGRAM_TOKEN)
    forecast_text = f"📊 Прогноз: рост — {forecast[0]}%, падение — {forecast[1]}%" if forecast else ""
    ema_text = (
        f"📈 EMA5: {ema5:.5f} | EMA20: {ema20:.5f} | RSI: {rsi:.2f}"
        if ema5 is not None and ema20 is not None and rsi is not None
        else "📈 EMA/RSI: нет данных"
    )
    header = "🚀 Стартовый сигнал" if is_start else f"📌 Сигнал: {signal}"
    msg = (
        f"📊 RUB/CNY: {price:.5f}\n"
        f"{header}\n"
        f"🛑 Stop Loss: {sl:.5f}\n"
        f"🎯 Take Profit: {tp:.5f}\n"
        f"{forecast_text}\n"
        f"{ema_text}\n"
        f"⏱ Время: {datetime.now().strftime('%H:%M:%S')}"
    )
    await bot.send_message(CHAT_ID, msg)
    with open("charts/chart.png", "rb") as photo:
        await bot.send_photo(CHAT_ID, photo)
    await bot.session.close()

# ===== Основной цикл =====
def main():
    prices = []
    first_run = True
    last_signal = None

    while True:
        price = get_rub_cny_price()
        if price is None:
            time.sleep(60)
            continue

        prices.append(price)
        if len(prices) > 60:
            prices = prices[-60:]

        signal, df = generate_signal(prices)
        sl = price * (1 - STOP_LOSS_PCT / 100) if signal == "BUY" else price * (1 + STOP_LOSS_PCT / 100)
        tp = price * (1 + TAKE_PROFIT_PCT / 100) if signal == "BUY" else price * (1 - TAKE_PROFIT_PCT / 100)

        model = train_xgb(prices)
        forecast = predict_xgb(model, prices)

        last = df.iloc[-1]
        ema5_val = last["ema_fast"] if pd.notna(last["ema_fast"]) else None
        ema20_val = last["ema_slow"] if pd.notna(last["ema_slow"]) else None
        rsi_val = last["rsi"] if pd.notna(last["rsi"]) else None

        plot_chart(df, signal, price, sl, tp, forecast)

        if first_run or signal != last_signal:
            asyncio.run(send_signal(signal, price, sl, tp, forecast,
                                    ema5=ema5_val, ema20=ema20_val, rsi=rsi_val,
                                    is_start=first_run))
            save_signal_to_csv(signal, price, sl, tp, forecast,
                               ema5=ema5_val, ema20=ema20_val, rsi=rsi_val)
            last_signal = signal
            first_run = False

        time.sleep(60)

if __name__ == "__main__":
    main()

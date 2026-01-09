import logging
import os
import pandas as pd
import io
import json
import gc  # Додано для примусового очищення пам'яті
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf

from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler
from binance.client import Client
from flask import Flask
from threading import Thread

# --- Глобальні налаштування ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("Не знайдено TELEGRAM_TOKEN у змінних середовища!")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

binance_client = Client()
user_alerts = {}
all_binance_symbols = []

# --- Функції файлів (без змін) ---
def save_alerts_to_file():
    try:
        with open('alerts.json', 'w') as f:
            json.dump(user_alerts, f, indent=4)
    except Exception as e:
        logger.error(f"Помилка збереження: {e}")

def load_alerts_from_file():
    global user_alerts
    try:
        with open('alerts.json', 'r') as f:
            content = f.read()
            if content:
                user_alerts = {int(k): v for k, v in json.loads(content).items()}
            else:
                user_alerts = {}
    except (FileNotFoundError, json.JSONDecodeError):
        user_alerts = {}

# --- Допоміжні функції ---
def populate_symbols_cache():
    global all_binance_symbols
    try:
        logger.info("Завантаження пар з Binance...")
        exchange_info = binance_client.get_exchange_info()
        all_binance_symbols = [s["symbol"] for s in exchange_info["symbols"] if s["status"] == "TRADING"]
    except Exception as e:
        logger.error(f"Помилка завантаження символів: {e}")

def calculate_rsi(data: pd.Series, length: int = 14) -> pd.Series:
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# --- Обробники ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привіт! Я бот для моніторингу цін.\n\n"
        "📈 `/chart <СИМВОЛ> <ІНТЕРВАЛ> [ДНІ]`\n"
        "🔔 `/alert <СИМВОЛ> < > <ЦІНА>`\n"
        "📋 `/my_alerts`\n"
        "🗑️ `/delete_alert <НОМЕР>`"
    )

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query.upper()
    if len(query) < 2:
        return
    results = [s for s in all_binance_symbols if query in s]
    inline_results = [
        InlineQueryResultArticle(
            id=symbol, title=symbol,
            input_message_content=InputTextMessageContent(f"/chart {symbol} 1d"),
            description=f"Графік {symbol}"
        ) for symbol in results[:20]
    ]
    await update.inline_query.answer(inline_results, cache_time=10)

async def get_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_message = None
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Приклад: `/chart BTCUSDT 1d 90`")
            return

        symbol, interval = args[0].upper(), args[1].lower()
        days = int(args[2]) if len(args) > 2 else 30
        days = min(max(days, 1), 200) # Зменшив ліміт до 200 днів для економії пам'яті
        
        status_message = await update.message.reply_text(f"⏳ Завантажую дані для {symbol}...")

        days_to_fetch = days + 50
        start_str = f"{days_to_fetch} day ago UTC"
        
        # ВАЖЛИВО: Огортаємо блокуючі операції, щоб не вішати бота
        # (На майбутнє: краще використовувати asyncio.to_thread, але поки залишимо так для простоти)
        klines = binance_client.get_historical_klines(symbol, interval, start_str)

        if not klines:
            await status_message.edit_text(f"Немає даних для {symbol}.")
            return

        # Оптимізація DataFrame для зменшення пам'яті
        df = pd.DataFrame(klines, columns=["Open Time", "Open", "High", "Low", "Close", "Volume", "Close Time",
                                           "QAV", "NoT", "TBB", "TBQ", "Ignore"])
        
        # Видаляємо зайві колонки одразу
        df = df[["Open Time", "Open", "High", "Low", "Close", "Volume"]]
        
        df["Open Time"] = pd.to_datetime(df["Open Time"], unit="ms")
        df.set_index("Open Time", inplace=True)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col])

        df["SMA_20"] = df["Close"].rolling(window=20).mean()
        df["SMA_50"] = df["Close"].rolling(window=50).mean()
        df["RSI_14"] = calculate_rsi(df["Close"], 14)

        df_to_plot = df.tail(days)
        
        ap = [
            mpf.make_addplot(df_to_plot["SMA_20"], panel=0, color="orange", width=0.7),
            mpf.make_addplot(df_to_plot["SMA_50"], panel=0, color="cyan", width=0.7),
            mpf.make_addplot(df_to_plot["RSI_14"], panel=2, color="purple", width=0.7, ylabel="RSI"),
            mpf.make_addplot([70] * len(df_to_plot), panel=2, color="red", linestyle="--", width=0.5),
            mpf.make_addplot([30] * len(df_to_plot), panel=2, color="green", linestyle="--", width=0.5)
        ]

        buf = io.BytesIO()
        # Вимкнено tight_layout автоматично через config, зменшено dpi для швидкості
        mpf.plot(df_to_plot, type="candle", style="binance", title=f"{symbol} ({interval})", 
                 ylabel="Ціна", volume=True, ylabel_lower="Об'єм", addplot=ap, 
                 panel_ratios=(6, 2, 3), figratio=(16, 9),
                 savefig=dict(fname=buf, dpi=100)) # DPI 100 достатньо для Telegram і економить пам'ять
        
        buf.seek(0)
        
        # ВАЖЛИВО: Очищення ресурсів Matplotlib
        plt.clf()
        plt.close('all')
        
        last_price = df_to_plot['Close'].iloc[-1]
        caption_text = (f"**{symbol} | {interval}**\n"
                        f"Ціна: `{last_price:,.2f}`")
        
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_message.message_id)
        await update.message.reply_photo(photo=buf, caption=caption_text, parse_mode='Markdown')
        
        # Примусове очищення пам'яті
        del df
        del df_to_plot
        del buf
        gc.collect()

    except Exception as e:
        logger.error(f"Помилка графіка: {e}")
        if status_message:
            try:
                await status_message.edit_text("⚠️ Помилка завантаження даних.")
            except:
                pass
        else:
            await update.message.reply_text("⚠️ Помилка.")

async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        if len(context.args) != 3:
            await update.message.reply_text("Приклад: `/alert BTCUSDT > 65000`")
            return
        symbol, condition, price = context.args[0].upper(), context.args[1], float(context.args[2])
        
        # Швидка перевірка без запиту до API, якщо символ є в кеші
        if symbol not in all_binance_symbols:
             # Якщо немає в кеші, спробуємо перевірити через API (раптом новий лістинг)
            try:
                binance_client.get_symbol_ticker(symbol=symbol)
            except:
                await update.message.reply_text(f"Пара '{symbol}' не знайдена.")
                return

        if condition not in ['>', '<']:
            await update.message.reply_text("Тільки '>' або '<'.")
            return

        alert = {'symbol': symbol, 'condition': condition, 'price': price}
        if chat_id not in user_alerts:
            user_alerts[chat_id] = []
        user_alerts[chat_id].append(alert)
        save_alerts_to_file()
        await update.message.reply_text(f"✅ Сповіщення для **{symbol}** встановлено!", parse_mode='Markdown')

    except Exception:
        await update.message.reply_text("Помилка формату.")

async def my_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in user_alerts or not user_alerts[chat_id]:
        await update.message.reply_text("Немає сповіщень.")
        return
    message = "📋 **Ваші сповіщення:**\n"
    for i, alert in enumerate(user_alerts[chat_id]):
        message += f"{i + 1}. **{alert['symbol']}** {alert['condition']} {alert['price']}\n"
    await update.message.reply_text(message, parse_mode='Markdown')

async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        if len(context.args) != 1: return
        idx = int(context.args[0]) - 1
        if chat_id in user_alerts and 0 <= idx < len(user_alerts[chat_id]):
            removed = user_alerts[chat_id].pop(idx)
            save_alerts_to_file()
            await update.message.reply_text(f"🗑️ Видалено: {removed['symbol']}")
        else:
            await update.message.reply_text("Невірний номер.")
    except:
        await update.message.reply_text("Помилка.")

async def price_checker(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user_alerts:
        return
    
    alerts_to_remove = {}
    alerts_copy = user_alerts.copy()

    # Оптимізація: збираємо унікальні символи, щоб не робити зайві запити
    unique_symbols = set()
    for alerts in alerts_copy.values():
        for alert in alerts:
            unique_symbols.add(alert['symbol'])
    
    # Отримуємо ціни пакетно (якщо це можливо) або по черзі, але обробляємо помилки
    current_prices = {}
    for sym in unique_symbols:
        try:
            # Це все ще блокуючий запит, але ми робимо його 1 раз на символ, а не на кожен алерт
            ticker = binance_client.get_symbol_ticker(symbol=sym)
            current_prices[sym] = float(ticker['price'])
        except Exception as e:
            logger.error(f"Помилка отримання ціни {sym}: {e}")
            continue

    for chat_id, alerts in alerts_copy.items():
        for i, alert in enumerate(alerts):
            sym = alert['symbol']
            if sym not in current_prices: continue
            
            curr_price = current_prices[sym]
            target = alert['price']
            cond = alert['condition']

            if (cond == '>' and curr_price > target) or (cond == '<' and curr_price < target):
                try:
                    msg = f"🔔 **{sym}** досяг {curr_price}\n(умова: {cond} {target})"
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                    
                    if chat_id not in alerts_to_remove: alerts_to_remove[chat_id] = []
                    alerts_to_remove[chat_id].append(i)
                except Exception as e:
                    logger.error(f"Не вдалося надіслати повідомлення: {e}")

    if alerts_to_remove:
        for chat_id, indices in alerts_to_remove.items():
            # Видаляємо з кінця, щоб не збити індекси
            for index in sorted(indices, reverse=True):
                if chat_id in user_alerts and index < len(user_alerts[chat_id]):
                    user_alerts[chat_id].pop(index)
        save_alerts_to_file()

# --- Flask ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    # Важливо: вимикаємо debug режим і reloader для потоку
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True # Потік закриється разом з основним процесом
    t.start()

def main() -> None:
    load_alerts_from_file()
    keep_alive()
    populate_symbols_cache()
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("chart", get_chart))
    application.add_handler(CommandHandler("alert", set_alert))
    application.add_handler(CommandHandler("my_alerts", my_alerts))
    application.add_handler(CommandHandler("delete_alert", delete_alert))
    application.add_handler(InlineQueryHandler(inline_query))

    application.job_queue.run_repeating(price_checker, interval=60, first=10)

    logger.info("Бот запущено")
    application.run_polling()

if __name__ == "__main__":
    main()

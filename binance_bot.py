import logging
import os
import pandas as pd
import io
import mplfinance as mpf
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler
from binance.client import Client
from flask import Flask
from threading import Thread

# -------------------
#  Глобальні налаштування
# -------------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # ✅ Токен беремо з ENV
if not TELEGRAM_TOKEN:
    raise ValueError("Не знайдено TELEGRAM_TOKEN у змінних середовища!")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

binance_client = Client()
user_alerts = {}
all_binance_symbols = []

def populate_symbols_cache():
    """Завантажує та кешує всі торгові пари з Binance при старті."""
    global all_binance_symbols
    try:
        logger.info("Завантаження списку торгових пар з Binance...")
        exchange_info = binance_client.get_exchange_info()
        all_binance_symbols = [s["symbol"] for s in exchange_info["symbols"] if s["status"] == "TRADING"]
        logger.info(f"Успішно завантажено {len(all_binance_symbols)} пар.")
    except Exception as e:
        logger.error(f"Не вдалося завантажити список символів: {e}")

def calculate_rsi(data: pd.Series, length: int = 14) -> pd.Series:
    """Розраховує RSI вручну."""
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# -------------------
#  Команди
# -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привіт! Я бот для моніторингу цін на Binance.\n\n"
        "📈 `/chart <СИМВОЛ> <ІНТЕРВАЛ> [ДНІ]`\n"
        "🔔 `/alert <СИМВОЛ> < > <ЦІНА>`\n"
        "📋 `/my_alerts`\n"
        "🗑️ `/delete_alert <НОМЕР>`"
    )

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query.upper()
    if not query:
        return
    results = [s for s in all_binance_symbols if query in s]
    inline_results = [
        InlineQueryResultArticle(
            id=symbol,
            title=symbol,
            input_message_content=InputTextMessageContent(f"/chart {symbol} 1d"),
            description=f"Отримати денний графік для {symbol}"
        )
        for symbol in results[:20]
    ]
    await update.inline_query.answer(inline_results, cache_time=10)

async def get_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Приклад: `/chart BTCUSDT 1d 90`")
            return

        symbol, interval = args[0].upper(), args[1].lower()
        days = int(args[2]) if len(args) > 2 else 30
        days = min(max(days, 1), 500)

        status_message = await update.message.reply_text(f"⏳ Завантажую дані для {symbol}...")

        klines = binance_client.get_historical_klines(symbol, interval, f"{days+50} day ago UTC")
        if not klines:
            await status_message.edit_text(f"Немає даних для {symbol}.")
            return

        df = pd.DataFrame(klines, columns=["Open Time","Open","High","Low","Close","Volume","Close Time",
                                           "Quote Asset Volume","Number of Trades","Taker Buy Base Asset Volume",
                                           "Taker Buy Quote Asset Volume","Ignore"])
        df["Open Time"] = pd.to_datetime(df["Open Time"], unit="ms")
        df.set_index("Open Time", inplace=True)
        for col in ["Open","High","Low","Close","Volume"]:
            df[col] = pd.to_numeric(df[col])

        df["SMA_20"] = df["Close"].rolling(window=20).mean()
        df["SMA_50"] = df["Close"].rolling(window=50).mean()
        df["RSI_14"] = calculate_rsi(df["Close"], 14)

        df_to_plot = df.tail(days)
        ap = [
            mpf.make_addplot(df_to_plot["SMA_20"], panel=0, color="orange", width=0.7),
            mpf.make_addplot(df_to_plot["SMA_50"], panel=0, color="cyan", width=0.7),
            mpf.make_addplot(df_to_plot["RSI_14"], panel=2, color="purple", width=0.7, ylabel="RSI"),
            mpf.make_addplot([70]*len(df_to_plot), panel=2, color="red", linestyle="--", width=0.5),
            mpf.make_addplot([30]*len(df_to_plot), panel=2, color="green", linestyle="--", width=0.5)
        ]

        buf = io.BytesIO()
        mpf.plot(df_to_plot, type="candle", style="binance", title=f"{symbol} ({interval})",
                 ylabel="Ціна", volume=True, ylabel_lower="Об'єм", addplot=ap,
                 panel_ratios=(6,2,3), figratio=(16,9), savefig=dict(fname=buf, dpi=150))
        buf.seek(0)

        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_message.message_id)
        # --- Розрахунок даних для підпису ---
        last_price = df_to_plot['Close'].iloc[-1]
        high_price = df_to_plot['High'].max()
        low_price = df_to_plot['Low'].min()

        caption_text = (f"**{symbol} | {interval} | {days} днів**\n\n"
                        f"**Остання ціна:** `{last_price:,.2f}`\n"
                        f"**Максимум:** `{high_price:,.2f}`\n"
                        f"**Мінімум:** `{low_price:,.2f}`")

        # --- Надсилання фото з детальним підписом ---
        await update.message.reply_photo(photo=buf, caption=caption_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Помилка графіка: {e}")
        await update.message.reply_text("⚠️ Помилка при побудові графіка.")


async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Встановлює нове сповіщення про ціну з перевіркою символу."""
    chat_id = update.effective_chat.id
    try:
        if len(context.args) != 3:
            await update.message.reply_text("Неправильний формат. Приклад: `/alert BTCUSDT > 65000`")
            return

        symbol = context.args[0].upper()
        condition = context.args[1]
        price = float(context.args[2])

        try:
            binance_client.get_symbol_ticker(symbol=symbol)
            logger.info(f"Символ {symbol} успішно перевірено.")
        except Exception as e:
            logger.error(f"Недійсний символ {symbol} від {chat_id}: {e}")
            await update.message.reply_text(f"Помилка: торгова пара '{symbol}' не знайдена на Binance.")
            return

        if condition not in ['>', '<']:
            await update.message.reply_text("Умова може бути тільки '>' (більше) або '<' (менше).")
            return

        alert = {'symbol': symbol, 'condition': condition, 'price': price}

        if chat_id not in user_alerts:
            user_alerts[chat_id] = []

        user_alerts[chat_id].append(alert)
        logger.info(f"Встановлено нове сповіщення для {chat_id}: {alert}")
        await update.message.reply_text(f"✅ Сповіщення для **{symbol}** встановлено!", parse_mode='Markdown')

    except (ValueError, IndexError):
        await update.message.reply_text("Помилка. Перевірте, чи правильно введена ціна.")


async def my_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує активні сповіщення користувача."""
    chat_id = update.effective_chat.id
    if chat_id not in user_alerts or not user_alerts[chat_id]:
        await update.message.reply_text("У вас немає активних сповіщень.")
        return

    message = "📋 **Ваші активні сповіщення:**\n"
    for i, alert in enumerate(user_alerts[chat_id]):
        message += f"{i + 1}. **{alert['symbol']}** {alert['condition']} {alert['price']}\n"

    await update.message.reply_text(message, parse_mode='Markdown')


async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видаляє сповіщення за його номером."""
    chat_id = update.effective_chat.id
    try:
        if len(context.args) != 1:
            await update.message.reply_text("Вкажіть номер сповіщення для видалення. Наприклад: `/delete_alert 1`")
            return

        alert_index = int(context.args[0]) - 1

        if chat_id in user_alerts and 0 <= alert_index < len(user_alerts[chat_id]):
            removed_alert = user_alerts[chat_id].pop(alert_index)
            logger.info(f"Видалено сповіщення для {chat_id}: {removed_alert}")
            await update.message.reply_text(f"🗑️ Сповіщення для **{removed_alert['symbol']}** видалено.",
                                            parse_mode='Markdown')
        else:
            await update.message.reply_text("Неправильний номер сповіщення.")

    except (ValueError, IndexError):
        await update.message.reply_text("Будь ласка, введіть правильний номер.")


async def price_checker(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Періодично перевіряє ціни для всіх встановлених сповіщень."""
    if not user_alerts:
        return

    alerts_to_remove = {}

    for chat_id, alerts in user_alerts.items():
        for i, alert in enumerate(alerts):
            try:
                symbol, condition, target_price = alert['symbol'], alert['condition'], alert['price']

                ticker = binance_client.get_symbol_ticker(symbol=symbol)
                current_price = float(ticker['price'])

                condition_met = (condition == '>' and current_price > target_price) or \
                                (condition == '<' and current_price < target_price)

                if condition_met:
                    message = (f"🔔 **Спрацювало сповіщення!** 🔔\n\n"
                               f"**{symbol}** досяг ціни **{current_price:,.2f}**\n"
                               f"(умова: {condition} {target_price})")
                    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

                    if chat_id not in alerts_to_remove:
                        alerts_to_remove[chat_id] = []
                    alerts_to_remove[chat_id].append(i)
            except Exception as e:
                logger.error(f"Помилка перевірки ціни для {alert}: {e}")

    for chat_id, indices in alerts_to_remove.items():
        for index in sorted(indices, reverse=True):
            user_alerts[chat_id].pop(index)

# -------------------
#  Головна функція
# -------------------

app = Flask('')

@app.route('/')
def home():
    return "I'm alive"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
def main() -> None:
    """Основна функція для запуску бота та фонових завдань."""
    # Запускаємо веб-сервер у фоні, щоб Render не "засинав"
    keep_alive()

    # Заповнюємо кеш символів при старті
    populate_symbols_cache()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Реєстрація обробників команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("chart", get_chart))
    application.add_handler(CommandHandler("alert", set_alert))
    application.add_handler(CommandHandler("my_alerts", my_alerts))
    application.add_handler(CommandHandler("delete_alert", delete_alert))

    # Реєстрація обробника для inline-пошуку
    application.add_handler(InlineQueryHandler(inline_query))

    # Налаштування та запуск фонового завдання для перевірки цін
    job_queue = application.job_queue
    job_queue.run_repeating(price_checker, interval=60, first=10)
    # -----------------------------------------------

    logger.info("Бот запускається...")
    application.run_polling()

if __name__ == "__main__":
    main()

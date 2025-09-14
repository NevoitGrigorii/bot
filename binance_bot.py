import logging
import os
import pandas as pd
import io
import mplfinance as mpf
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler
from binance.client import Client

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
        await update.message.reply_photo(photo=buf, caption=f"📊 {symbol} ({interval})", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Помилка графіка: {e}")
        await update.message.reply_text("⚠️ Помилка при побудові графіка.")

# -------------------
#  Головна функція
# -------------------
def main():
    populate_symbols_cache()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chart", get_chart))
    app.add_handler(InlineQueryHandler(inline_query))
    logger.info("Бот запускається...")
    app.run_polling()

if __name__ == "__main__":
    main()

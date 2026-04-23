import asyncio
import logging
import io
import aiosqlite
import pandas as pd
import mplfinance as mpf
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, \
    InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
BOT_TOKEN = "api"
DB_NAME = "trading_bot.db"

# Список доступных пар для настройки утренней рассылки
AVAILABLE_PAIRS_FOR_SUMMARY = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XAUUSDT"]

MAIN_MENU_TEXT = (
    "📈 **Bybit Trading Assistant**\n\n"
    "Добро пожаловать в Get&Give! Я помогаю отслеживать графики и настроение рынка, а также уведомляю Вас об инетересующей паре в 8:00 UTC (европейская сессия) и об открытии рынка форекс в 22:00 UTC воскресенье.\n\n"
    "💡 *Как получить график?*\n"
    "Просто напиши название пары, например: `BTCUSDT`, `ETHUSDT` или `XAUUSDT`."
)

dp = Dispatcher()


# --- КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ---
class Database:
    @staticmethod
    async def init():
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    enabled INTEGER DEFAULT 1,
                    tickers TEXT DEFAULT 'BTCUSDT,ETHUSDT,XAUUSDT'
                )
            """)
            await db.commit()

    @staticmethod
    async def update_user(user_id, enabled=None, tickers=None):
        async with aiosqlite.connect(DB_NAME) as db:
            if enabled is not None:
                await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
                await db.execute("UPDATE users SET enabled = ? WHERE user_id = ?", (int(enabled), user_id))
            if tickers is not None:
                await db.execute("UPDATE users SET tickers = ? WHERE user_id = ?", (tickers, user_id))
            await db.commit()

    @staticmethod
    async def get_user(user_id):
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT enabled, tickers FROM users WHERE user_id = ?", (user_id,)) as cursor:
                return await cursor.fetchone()

    @staticmethod
    async def get_all_active_users():
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT user_id, tickers FROM users WHERE enabled = 1") as cursor:
                return await cursor.fetchall()


# --- РАБОТА С API И ГРАФИКОМ ---

async def fetch_ticker_info(ticker: str):
    """Новая функция: получает текущую цену, 24h изменение и тип рынка (Спот в приоритете)."""
    for cat in ['spot', 'linear']:
        url = f"https://api.bybit.com/v5/market/tickers?category={cat}&symbol={ticker}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=10) as resp:
                    res = await resp.json()
                    if res.get("result") and res["result"].get("list"):
                        data = res["result"]["list"][0]
                        return {
                            "price": float(data.get("lastPrice", 0)),
                            "change": float(data.get("price24hPcnt", 0)) * 100,
                            "category": "Спот" if cat == "spot" else "Фьючерсы"
                        }
            except Exception as e:
                logging.error(f"Ошибка API (Ticker Info): {e}")
    return None


async def fetch_klines(ticker: str, interval: str, limit: int = 60):
    tf_map = {"1м": "1", "5м": "5", "1ч": "60", "4ч": "240", "1д": "D"}
    bybit_tf = tf_map.get(interval, "60")

    # Приоритет спота: если есть на споте, берем спот. Иначе линейные (фьючи)
    for cat in ['spot', 'linear']:
        url = f"https://api.bybit.com/v5/market/kline?category={cat}&symbol={ticker}&interval={bybit_tf}&limit={limit}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=10) as resp:
                    res = await resp.json()
                    if res.get("result") and res["result"].get("list"):
                        return res["result"]["list"], cat
            except Exception as e:
                logging.error(f"Ошибка API (Klines): {e}")
    return None, None


def generate_candle_chart(kline_data, ticker, interval):
    df = pd.DataFrame(kline_data, columns=['time', 'open', 'high', 'low', 'close', 'vol', 'turnover'])
    df['time'] = pd.to_datetime(df['time'].astype(float), unit='ms')
    df = df.set_index('time').astype(float).sort_index()

    mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', wick='inherit', edge='inherit', volume='in')

    s = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=':',
        facecolor='#131722',
        edgecolor='#2f333d',
        rc={'text.color': 'white', 'axes.labelcolor': 'white', 'xtick.color': 'white', 'ytick.color': 'white'}
    )

    buf = io.BytesIO()
    mpf.plot(df, type='candle', style=s, title=f"\n{ticker} ({interval})",
             ylabel='Price', volume=False, savefig=dict(fname=buf, dpi=120, facecolor='#131722', bbox_inches='tight'))
    buf.seek(0)
    return buf


async def get_fear_and_greed():
    url = "https://api.alternative.me/fng/"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return data["data"][0]


# --- КЛАВИАТУРЫ ---

def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Список популярных пар", callback_data="coin_list")],
        [InlineKeyboardButton(text="🧭 Настроение рынка", callback_data="sentiment")],
        [InlineKeyboardButton(text="⚙️ Настройки рассылки", callback_data="settings_menu")]
    ])


def get_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_main")]
    ])


def get_timeframe_keyboard(ticker, current_tf):
    tfs = ["1м", "5м", "1ч", "4ч", "1д"]
    buttons = []
    for tf in tfs:
        text = f"✅ {tf}" if tf == current_tf else tf
        buttons.append(InlineKeyboardButton(text=text, callback_data=f"tf_{ticker}_{tf}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


# --- ОБРАБОТЧИКИ НАВИГАЦИИ (МЕНЮ) ---

@dp.message(CommandStart())
async def start_cmd(message: Message):
    await Database.update_user(message.chat.id, enabled=True)
    await message.answer(MAIN_MENU_TEXT, reply_markup=get_main_menu(), parse_mode="Markdown")


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    """Возвращает пользователя в главное меню без отправки нового сообщения."""
    await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=get_main_menu(), parse_mode="Markdown")


@dp.callback_query(F.data == "coin_list")
async def show_coin_list(callback: CallbackQuery):
    text = (
        "📚 **Популярные инструменты на Bybit**\n\n"
        "👑 **Топ Крипта:**\n"
        "`BTCUSDT` (Bitcoin)\n`ETHUSDT` (Ethereum)\n`SOLUSDT` (Solana)\n`BNBUSDT` (Binance Coin)\n\n"
        "🐕 **Мемы:**\n"
        "`DOGEUSDT` (Dogecoin)\n`PEPEUSDT` (Pepe)\n`SHIBUSDT` (Shiba Inu)\n\n"
        "🏦 **TradFi:**\n"
        "`XAUUSDT` (Золото)\n`XAGUSDT` (Серебро)\n\n"
        "👉 *Чтобы получить график, отправь мне любой из этих тикеров текстом.*"
    )
    # Используем edit_text вместо answer
    await callback.message.edit_text(text, reply_markup=get_back_button(), parse_mode="Markdown")


@dp.callback_query(F.data == "sentiment")
async def show_sentiment(callback: CallbackQuery):
    fng_data = await get_fear_and_greed()
    value = int(fng_data['value'])
    classification = fng_data['value_classification']

    if value < 25:
        emoji = "😱"
    elif value < 45:
        emoji = "😨"
    elif value < 55:
        emoji = "😐"
    elif value < 75:
        emoji = "😏"
    else:
        emoji = "🤑"

    text = (
        f"🧭 **Индекс Страха и Жадности (Fear & Greed)**\n\n"
        f"Текущее значение: **{value}/100** {emoji}\n"
        f"Состояние: **{classification}**\n\n"
        f"💡 *Рынок управляется эмоциями. Экстремальный страх — возможность для покупки. Экстремальная жадность — время фиксировать прибыль.*"
    )
    # Используем edit_text
    await callback.message.edit_text(text, reply_markup=get_back_button(), parse_mode="Markdown")


# --- ОБРАБОТЧИКИ НАСТРОЕК ---

@dp.callback_query(F.data == "settings_menu")
async def settings_menu(callback: CallbackQuery):
    user = await Database.get_user(callback.from_user.id)
    is_enabled = user[0] if user else 1

    status_text = "✅ Включена" if is_enabled else "❌ Выключена"
    btn_text = "Выключить 🔕" if is_enabled else "Включить 🔔"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text, callback_data="toggle_sub")],
        [InlineKeyboardButton(text="⚙️ Выбрать пары для рассылки", callback_data="manage_pairs")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_main")]
    ])

    await callback.message.edit_text(
        f"⚙️ **Настройки утренней сводки (08:00 UTC)**\n\nТекущий статус: {status_text}",
        reply_markup=kb, parse_mode="Markdown"
    )


@dp.callback_query(F.data == "toggle_sub")
async def toggle_sub(callback: CallbackQuery):
    user = await Database.get_user(callback.from_user.id)
    new_state = 0 if user[0] else 1
    await Database.update_user(callback.from_user.id, enabled=new_state)
    await settings_menu(callback)


@dp.callback_query(F.data == "manage_pairs")
async def manage_pairs(callback: CallbackQuery):
    """Отображает список пар с возможностью их включения/выключения."""
    user = await Database.get_user(callback.from_user.id)
    # user[1] содержит строку типа 'BTCUSDT,ETHUSDT'
    active_tickers = user[1].split(',') if user and user[1] else []

    buttons = []
    for pair in AVAILABLE_PAIRS_FOR_SUMMARY:
        status = "✅" if pair in active_tickers else "❌"
        # Передаем тикер в callback_data
        buttons.append([InlineKeyboardButton(text=f"{status} {pair}", callback_data=f"togglepair_{pair}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад в настройки", callback_data="settings_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        "🛠 **Управление парами для сводки**\n\n"
        "Нажимай на кнопки ниже, чтобы добавить или убрать пару из утренней рассылки:",
        reply_markup=kb, parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("togglepair_"))
async def toggle_pair(callback: CallbackQuery):
    """Добавляет или удаляет пару из БД пользователя."""
    pair_to_toggle = callback.data.split("_")[1]
    user = await Database.get_user(callback.from_user.id)

    active_tickers = user[1].split(',') if user and user[1] else []

    if pair_to_toggle in active_tickers:
        active_tickers.remove(pair_to_toggle)
    else:
        active_tickers.append(pair_to_toggle)

    # Если удалили все, добавим хотя бы BTCUSDT по умолчанию, чтобы не было пустой строки
    if not active_tickers:
        active_tickers = ["BTCUSDT"]
        await callback.answer("Нельзя удалить все пары. Оставлен BTCUSDT.", show_alert=True)

    new_tickers_str = ",".join(active_tickers)
    await Database.update_user(callback.from_user.id, tickers=new_tickers_str)

    # Обновляем меню с галочками
    await manage_pairs(callback)


# --- ЛОГИКА ГРАФИКОВ ---

@dp.message(Command("price"))
async def price_cmd_wrapper(message: Message):
    parts = message.text.split()
    ticker = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    await send_chart(message, ticker, "1ч")


@dp.message(F.text & ~F.text.startswith('/'))
async def quick_search(message: Message):
    ticker = message.text.strip().upper()
    await send_chart(message, ticker, "1ч")


async def send_chart(message: Message, ticker: str, tf: str):
    msg = await message.answer(f"⏳ Анализирую {ticker} ({tf})...")

    # Получаем инфо (цена, 24h, тип рынка) и свечи параллельно для скорости
    ticker_info, (klines, cat) = await asyncio.gather(
        fetch_ticker_info(ticker),
        fetch_klines(ticker, tf)
    )

    if not klines or not ticker_info:
        await msg.edit_text(f"❌ Инструмент `{ticker}` не найден на Bybit.")
        return

    chart = generate_candle_chart(klines, ticker, tf)
    photo = BufferedInputFile(chart.read(), filename=f"{ticker}.png")

    # Формируем новую красивую подпись под графиком
    emoji_change = "🟢" if ticker_info['change'] >= 0 else "🔴"
    caption = (
        f"📊 **{ticker}** | Таймфрейм: {tf}\n\n"
        f"💰 **Цена:** `{ticker_info['price']:,.2f}`\n"
        f"{emoji_change} **Изменение (24h):** `{ticker_info['change']:+.2f}%`\n"
        f"📌 **Рынок:** {ticker_info['category']}"
    )

    await message.answer_photo(
        photo=photo,
        caption=caption,
        reply_markup=get_timeframe_keyboard(ticker, tf),
        parse_mode="Markdown"
    )
    await msg.delete()


# --- ОБРАБОТЧИК КНОПОК ТАЙМФРЕЙМОВ ---

@dp.callback_query(F.data.startswith("tf_"))
async def change_timeframe(callback: CallbackQuery):
    parts = callback.data.split("_")
    ticker = parts[1]
    new_tf = parts[2]

    await callback.answer(f"Загружаю {new_tf}...")

    ticker_info, (klines, cat) = await asyncio.gather(
        fetch_ticker_info(ticker),
        fetch_klines(ticker, new_tf)
    )

    if not klines or not ticker_info:
        await callback.answer("Ошибка API", show_alert=True)
        return

    chart = generate_candle_chart(klines, ticker, new_tf)
    photo = BufferedInputFile(chart.read(), filename=f"{ticker}.png")

    emoji_change = "🟢" if ticker_info['change'] >= 0 else "🔴"
    caption = (
        f"📊 **{ticker}** | Таймфрейм: {new_tf}\n\n"
        f"💰 **Цена:** `{ticker_info['price']:,.2f}`\n"
        f"{emoji_change} **Изменение (24h):** `{ticker_info['change']:+.2f}%`\n"
        f"📌 **Рынок:** {ticker_info['category']}"
    )

    media = InputMediaPhoto(media=photo, caption=caption, parse_mode="Markdown")

    await callback.message.edit_media(media=media, reply_markup=get_timeframe_keyboard(ticker, new_tf))


# --- ПЛАНИРОВЩИК ---
async def daily_job(bot: Bot):
    users = await Database.get_all_active_users()
    for uid, tickers in users:
        text = "🔔 **Утренняя сводка (08:00 UTC)**\n\n"
        for t in tickers.split(','):
            info = await fetch_ticker_info(t)
            if info:
                emoji = "🟢" if info['change'] >= 0 else "🔴"
                text += f"• **{t}:** `{info['price']:,.2f}` ({emoji} {info['change']:+.2f}%)\n"
            else:
                text += f"• **{t}:** `Нет данных`\n"
        try:
            await bot.send_message(uid, text, parse_mode="Markdown")
        except:
            pass


async def forex_open_job(bot: Bot):
    """Новая функция: Уведомление об открытии Форекс в понедельник 22:00 UTC"""
    users = await Database.get_all_active_users()
    text = (
        "🌍 **Рынок Форекс открыт!**\n\n"
        "Традиционные рынки (TradFi) снова в игре. Удачной торговой недели! 🚀\n"
        "Не забудь проверить графики `XAUUSDT`."
    )
    for uid, _ in users:
        try:
            await bot.send_message(uid, text, parse_mode="Markdown")
        except:
            pass


async def main():
    logging.basicConfig(level=logging.INFO)
    await Database.init()
    bot = Bot(token=BOT_TOKEN)

    scheduler = AsyncIOScheduler(timezone="UTC")
    # Утренняя рассылка (каждый день в 8:00)
    scheduler.add_job(daily_job, trigger='cron', hour=8, minute=0, args=[bot])
    # Новое уведомление: Понедельник (mon), 22:00
    scheduler.add_job(forex_open_job, trigger='cron', day_of_week='sun', hour=22, minute=0, args=[bot])
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
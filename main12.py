import asyncio
import logging
import aiosqlite
import pandas as pd
import mplfinance as mpf
import io
from datetime import datetime

import aiohttp
from aiohttp import web
import aiohttp_cors
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BufferedInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
BOT_TOKEN = "ТВОЙ_ТОКЕН_СЮДА"
DB_NAME = "trading_bot.db"
# СЮДА ВСТАВЬ ССЫЛКУ ОТ CLOUDFLARE (например: https://my-tunnel.trycloudflare.com)
WEBAPP_URL = "ТВОЯ_ССЫЛКА_CLOUDFLARE_СЮДА"

AVAILABLE_PAIRS_FOR_SUMMARY = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XAUUSDT"]

dp = Dispatcher()

# Заголовки для обхода блокировок API Bybit
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


# --- ФУНКЦИИ ДЛЯ СТАРОГО ГРАФИКА (РАССЫЛКА) ---
async def fetch_klines_simple(symbol, interval='60', category='spot'):
    url = f"https://api.bybit.com/v5/market/kline?category={category}&symbol={symbol}&interval={interval}&limit=50"
    async with aiohttp.ClientSession(headers=API_HEADERS) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get('retCode') == 0:
                return data['result']['list']
            return None


def generate_old_style_chart(klines, symbol):
    df = pd.DataFrame(klines, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'turnover'])
    df['ts'] = pd.to_datetime(pd.to_numeric(df['ts']), unit='ms')
    df.set_index('ts', inplace=True)
    df = df.astype(float).iloc[::-1]

    buf = io.BytesIO()
    mc = mpf.make_marketcolors(up='#2ea043', down='#f85149', inherit=True)
    s = mpf.make_mpf_style(base_mpf_style='charles', marketcolors=mc, gridcolor='#30363d', facecolor='#0d1117',
                           edgecolor='#30363d')

    mpf.plot(df, type='candle', style=s, title=f"\n{symbol} (1h)", savefig=buf, volume=False, figsize=(8, 4))
    buf.seek(0)
    return buf


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
            await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            if enabled is not None:
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


# --- HTML ФРОНТЕНД (Хранится в памяти) ---
HTML_CONTENT = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Trading App</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        :root {
            --bg-color: #0d1117; --text-color: #c9d1d9; --primary-blue: #58a6ff;
            --card-bg: #161b22; --border-color: #30363d; --green: #2ea043; --red: #f85149;
        }
        body {
            background-color: var(--bg-color); color: var(--text-color);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0; padding: 15px; box-sizing: border-box;
            display: flex; flex-direction: column; height: 100vh;
        }
        h2 { color: var(--primary-blue); margin-top: 0; margin-bottom: 10px; font-size: 18px;}
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; flex-shrink: 0; }
        .tab-btn {
            flex: 1; padding: 10px; background: transparent; border: 1px solid var(--primary-blue);
            color: var(--primary-blue); border-radius: 8px; font-weight: bold; cursor: pointer;
        }
        .tab-btn.active { background: var(--primary-blue); color: #fff; }

        #section-chart, #section-sentiment { display: flex; flex-direction: column; flex-grow: 1; }
        .card { background: var(--card-bg); padding: 15px; border-radius: 12px; border: 1px solid var(--border-color); margin-bottom: 15px; display: flex; flex-direction: column; flex-grow: 1;}

        .controls-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; flex-shrink: 0; }
        .controls-grid select, .controls-grid input { 
            width: 100%; padding: 12px; background: var(--bg-color); color: #fff; 
            border: 1px solid var(--border-color); border-radius: 8px; font-size: 14px; box-sizing: border-box;
        }
        .full-width { grid-column: span 2; }
        button.primary { width: 100%; padding: 12px; margin-top: 20px; background: var(--primary-blue); color: #fff; border: none; border-radius: 8px; font-weight: bold; cursor: pointer;}

        .search-container { position: relative; }
        .autocomplete-items {
            position: absolute; border: 1px solid var(--border-color); border-top: none; z-index: 99;
            top: 100%; left: 0; right: 0; background-color: var(--card-bg); max-height: 200px; 
            overflow-y: auto; border-radius: 0 0 8px 8px; display: none; box-shadow: 0px 8px 16px rgba(0,0,0,0.5);
        }
        .autocomplete-items div { padding: 12px; cursor: pointer; border-bottom: 1px solid var(--border-color); }
        .autocomplete-items div:hover { background-color: #21262d; color: var(--primary-blue); }

        #tvchart { width: 100%; height: 55vh; min-height: 350px; flex-grow: 1; }

        .ticker-stats { 
            display: flex; justify-content: space-between; align-items: center; 
            background: #0d1117; padding: 12px; border-radius: 8px; margin-top: 15px; border: 1px solid var(--border-color); flex-shrink: 0;
        }
        .stat-item { display: flex; flex-direction: column; }
        .stat-label { font-size: 12px; color: #8b949e; margin-bottom: 4px;}
        .stat-value { font-size: 16px; font-weight: bold; }
        .stat-value.up { color: var(--green); }
        .stat-value.down { color: var(--red); }

        .setting-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border-color); }
        .switch { position: relative; display: inline-block; width: 40px; height: 20px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--border-color); transition: .4s; border-radius: 20px; }
        .slider:before { position: absolute; content: ""; height: 16px; width: 16px; left: 2px; bottom: 2px; background-color: white; transition: .4s; border-radius: 50%; }
        input:checked + .slider { background-color: var(--primary-blue); }
        input:checked + .slider:before { transform: translateX(20px); }
        #section-settings, #section-sentiment { display: none; }

        /* Стили для сентимента */
        .sentiment-bar { height: 24px; background: var(--red); border-radius: 12px; overflow: hidden; display: flex; margin: 15px 0; }
        .sentiment-long { background: var(--green); height: 100%; transition: width 0.5s; }
        .sentiment-info { display: flex; justify-content: space-between; font-weight: bold; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('chart')">Графики</button>
        <button class="tab-btn" onclick="switchTab('sentiment')">Настроение</button>
        <button class="tab-btn" onclick="switchTab('settings')">Настройки</button>
    </div>

    <div id="section-chart">
        <div class="card">
            <h2>Терминал Bybit</h2>

            <div class="controls-grid">
                <select id="market-select" class="full-width" onchange="onMarketChange()">
                    <option value="linear" selected>Рынок: Фьючерсы</option>
                    <option value="spot">Рынок: Спот</option>
                </select>

                <div class="search-container">
                    <input type="text" id="coin-select" value="BTCUSDT" placeholder="Поиск..." onkeyup="filterSymbols()" onclick="filterSymbols()" autocomplete="off">
                    <div id="autocomplete-list" class="autocomplete-items"></div>
                </div>

                <select id="tf-select" onchange="loadChartData()">
                    <option value="1">1 Мин</option>
                    <option value="5">5 Мин</option>
                    <option value="15">15 Мин</option>
                    <option value="60" selected>1 Час</option>
                    <option value="240">4 Часа</option>
                    <option value="D">1 День</option>
                </select>
            </div>

            <div id="tvchart"></div>

            <div class="ticker-stats">
                <div class="stat-item">
                    <span class="stat-label">Цена</span>
                    <span class="stat-value" id="stat-price">...</span>
                </div>
                <div class="stat-item" style="text-align: center;">
                    <span class="stat-label">24h Изм.</span>
                    <span class="stat-value" id="stat-change">...</span>
                </div>
                <div class="stat-item" style="text-align: right;">
                    <span class="stat-label">Тип</span>
                    <span class="stat-value" id="stat-type" style="color: #c9d1d9;">...</span>
                </div>
            </div>
        </div>
    </div>

    <div id="section-sentiment">
        <div class="card">
            <h2>Настроение рынка (Long/Short)</h2>
            <div class="controls-grid" style="margin-bottom: 20px;">
                <input type="text" id="sentiment-coin" class="full-width" value="BTCUSDT" placeholder="Введите монету (например, BTCUSDT)">
                <button class="primary full-width" onclick="loadSentiment()" style="margin-top: 0;">Проверить</button>
            </div>
            <div id="sentiment-result" style="display: none; padding-top: 10px;">
                <h3 id="sentiment-title" style="text-align: center; margin-bottom: 5px; color: var(--primary-blue);">BTCUSDT</h3>
                <p style="text-align: center; color: #8b949e; font-size: 12px; margin-top: 0;">Фьючерсы, за 1 час</p>
                <div class="sentiment-bar">
                    <div id="long-bar" class="sentiment-long" style="width: 50%;"></div>
                </div>
                <div class="sentiment-info">
                    <span style="color: var(--green);" id="long-text">Лонг: 50%</span>
                    <span style="color: var(--red);" id="short-text">Шорт: 50%</span>
                </div>
                <p id="sentiment-ratio" style="text-align: center; margin-top: 25px; font-size: 18px; font-weight: bold;">Соотношение: 1.0</p>
            </div>
        </div>
    </div>

    <div id="section-settings">
        <div class="card">
            <h2>Утренняя рассылка</h2>
            <div class="setting-item">
                <span>Включить рассылку (08:00 UTC)</span>
                <label class="switch"><input type="checkbox" id="main-toggle"><span class="slider"></span></label>
            </div>
            <h3 style="margin-top: 20px; font-size: 16px; color: var(--primary-blue);">Монеты для сводки</h3>
            <div id="coins-list"></div>
            <button class="primary" onclick="saveSettings()">Сохранить настройки</button>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();

        const userId = tg.initDataUnsafe?.user?.id || 123; 
        const defaultSummaryCoins = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XAUUSDT"];
        let chart = null;
        let candleSeries = null;
        let allSymbols = [];

        function switchTab(tab) {
            document.getElementById('section-chart').style.display = tab === 'chart' ? 'block' : 'none';
            document.getElementById('section-sentiment').style.display = tab === 'sentiment' ? 'block' : 'none';
            document.getElementById('section-settings').style.display = tab === 'settings' ? 'block' : 'none';
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');
            if (tab === 'settings') loadSettings();
            if (tab === 'sentiment' && document.getElementById('sentiment-result').style.display === 'none') loadSentiment();
        }

        // --- ИНИЦИАЛИЗАЦИЯ ГРАФИКА ---
        function initChart() {
            const container = document.getElementById('tvchart');
            container.innerHTML = ''; 

            chart = LightweightCharts.createChart(container, {
                autoSize: true,
                layout: { background: { color: 'transparent' }, textColor: '#c9d1d9' },
                grid: { vertLines: { color: '#30363d' }, horzLines: { color: '#30363d' } },
                timeScale: { timeVisible: true },
                rightPriceScale: {
                    autoScale: true,
                },
                handleScale: {
                    axisPressedMouseMove: true,
                    mouseWheel: true,
                    pinch: true,
                },
                handleScroll: {
                    mouseWheel: true,
                    pressedMouseMove: true,
                }
            });

            candleSeries = chart.addCandlestickSeries({ 
                upColor: '#2ea043', downColor: '#f85149', 
                borderVisible: false, wickUpColor: '#2ea043', wickDownColor: '#f85149' 
            });

            onMarketChange(); 
        }

        async function loadSymbols() {
            const market = document.getElementById('market-select').value;
            try {
                const res = await fetch(`/api/symbols?category=${market}`);
                const data = await res.json();
                allSymbols = data.symbols || [];
            } catch (e) { 
                console.error("Ошибка при загрузке списка монет");
            }
        }

        function filterSymbols() {
            const val = document.getElementById('coin-select').value.toUpperCase();
            const list = document.getElementById('autocomplete-list');
            list.innerHTML = '';

            if (!val) { list.style.display = 'none'; return; }

            const filtered = allSymbols.filter(s => s.includes(val)).slice(0, 30);
            if(filtered.length === 0) { list.style.display = 'none'; return; }

            filtered.forEach(sym => {
                let div = document.createElement('div');
                div.innerHTML = sym.replace(val, `<strong>${val}</strong>`); 
                div.onclick = function() {
                    document.getElementById('coin-select').value = sym;
                    list.style.display = 'none';
                    loadChartData(); 
                };
                list.appendChild(div);
            });
            list.style.display = 'block';
        }

        document.addEventListener('click', function(e) {
            if(e.target.id !== 'coin-select') {
                document.getElementById('autocomplete-list').style.display = 'none';
            }
        });

        async function onMarketChange() {
            await loadSymbols();
            loadChartData();
        }

        async function loadChartData() {
            if (!candleSeries) {
                initChart();
                return;
            }

            const coinInput = document.getElementById('coin-select').value.toUpperCase();
            const coin = coinInput ? coinInput : 'BTCUSDT';
            const tf = document.getElementById('tf-select').value;
            const market = document.getElementById('market-select').value;

            try {
                const response = await fetch(`/api/klines?category=${market}&symbol=${coin}&interval=${tf}`);
                if (!response.ok) throw new Error("HTTP " + response.status);
                const klineRes = await response.json();

                if (klineRes.retCode === 0 && klineRes.result && klineRes.result.list && klineRes.result.list.length > 0) {
                    const data = klineRes.result.list.map(k => ({
                        time: parseInt(k[0]) / 1000, open: parseFloat(k[1]), high: parseFloat(k[2]), low: parseFloat(k[3]), close: parseFloat(k[4])
                    })).reverse();

                    candleSeries.setData(data);
                    chart.timeScale().fitContent();
                    // СБРОС МАСШТАБА ЦЕНЫ: График больше не будет "застревать" на старой цене при смене монеты
                    chart.priceScale('right').applyOptions({ autoScale: true }); 
                } else {
                    candleSeries.setData([]); 
                    tg.showAlert(`Нет данных для ${coin} на рынке ${market === 'spot' ? 'Спот' : 'Фьючерсы'}`);
                }
            } catch (e) { 
                console.error(e);
            }

            try {
                const tResponse = await fetch(`/api/ticker?category=${market}&symbol=${coin}`);
                const tickerRes = await tResponse.json();

                if (tickerRes.retCode === 0 && tickerRes.result && tickerRes.result.list && tickerRes.result.list.length > 0) {
                    const tData = tickerRes.result.list[0];
                    const price = parseFloat(tData.lastPrice);
                    const change = parseFloat(tData.price24hPcnt) * 100;

                    document.getElementById('stat-price').innerText = price >= 1 ? price.toFixed(2) : price.toFixed(5);
                    const changeEl = document.getElementById('stat-change');
                    changeEl.innerText = (change > 0 ? '+' : '') + change.toFixed(2) + '%';
                    changeEl.className = 'stat-value ' + (change >= 0 ? 'up' : 'down');
                    document.getElementById('stat-type').innerText = tData.market_type || "Bybit";
                } else {
                    document.getElementById('stat-price').innerText = "-";
                    document.getElementById('stat-change').innerText = "-";
                }
            } catch (e) { console.error("Ошибка тикера", e); }
        }

        async function loadSentiment() {
            const coinInput = document.getElementById('sentiment-coin').value.toUpperCase() || 'BTCUSDT';
            document.getElementById('sentiment-coin').value = coinInput;
            try {
                const res = await fetch(`/api/sentiment?symbol=${coinInput}`);
                const data = await res.json();

                if (data.retCode === 0 && data.result && data.result.list && data.result.list.length > 0) {
                    const ratioData = data.result.list[0];
                    const buyVol = parseFloat(ratioData.buyRatio);
                    const sellVol = parseFloat(ratioData.sellRatio);

                    const longPct = (buyVol * 100).toFixed(1);
                    const shortPct = (sellVol * 100).toFixed(1);
                    const ratio = (buyVol / sellVol).toFixed(2);

                    document.getElementById('sentiment-title').innerText = coinInput;
                    document.getElementById('long-bar').style.width = `${longPct}%`;
                    document.getElementById('long-text').innerText = `Лонг: ${longPct}%`;
                    document.getElementById('short-text').innerText = `Шорт: ${shortPct}%`;
                    document.getElementById('sentiment-ratio').innerText = `Соотношение (Long/Short): ${ratio}`;

                    document.getElementById('sentiment-result').style.display = 'block';
                } else {
                    tg.showAlert(`Нет данных о настроении для ${coinInput}. Попробуйте другую монету (только фьючерсы).`);
                }
            } catch (e) {
                console.error(e);
                tg.showAlert("Ошибка при загрузке настроений");
            }
        }

        async function loadSettings() {
            try {
                const res = await fetch(`/api/settings?user_id=${userId}`).then(r => r.json());
                document.getElementById('main-toggle').checked = res.enabled;
                const container = document.getElementById('coins-list');
                container.innerHTML = '';
                defaultSummaryCoins.forEach(coin => {
                    const isChecked = res.tickers.includes(coin) ? 'checked' : '';
                    container.innerHTML += `
                        <div class="setting-item">
                            <span>${coin}</span>
                            <label class="switch"><input type="checkbox" class="coin-cb" value="${coin}" ${isChecked}><span class="slider"></span></label>
                        </div>`;
                });
            } catch (e) { console.error(e); }
        }

        async function saveSettings() {
            const enabled = document.getElementById('main-toggle').checked ? 1 : 0;
            const tickers = Array.from(document.querySelectorAll('.coin-cb:checked')).map(cb => cb.value);
            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId, enabled, tickers })
            });
            tg.showAlert("Настройки успешно сохранены!");
        }

        document.addEventListener("DOMContentLoaded", () => {
            initChart();
        });
    </script>
</body>
</html>"""

# --- ВЕБ-СЕРВЕР (API ДЛЯ MINI APP) ---

HTTP_HEADERS = {
    "X-Frame-Options": "ALLOWALL",
    "Access-Control-Allow-Origin": "*",
    "Content-Security-Policy": "frame-ancestors *"
}


async def handle_index(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html', headers=HTTP_HEADERS)


async def api_get_settings(request):
    user_id = request.query.get('user_id')
    if not user_id:
        return web.json_response({"error": "No user_id"}, status=400, headers=HTTP_HEADERS)

    user = await Database.get_user(int(user_id))
    if user:
        return web.json_response({"enabled": user[0], "tickers": user[1].split(',')}, headers=HTTP_HEADERS)
    return web.json_response({"enabled": 1, "tickers": ["BTCUSDT", "ETHUSDT", "XAUUSDT"]}, headers=HTTP_HEADERS)


async def api_update_settings(request):
    data = await request.json()
    user_id = data.get('user_id')
    enabled = data.get('enabled')
    tickers = ",".join(data.get('tickers', []))

    if user_id:
        await Database.update_user(int(user_id), enabled=enabled, tickers=tickers)
        return web.json_response({"status": "ok"}, headers=HTTP_HEADERS)
    return web.json_response({"error": "Bad request"}, status=400, headers=HTTP_HEADERS)


async def proxy_klines(request):
    symbol = request.query.get('symbol', 'BTCUSDT').upper()
    interval = request.query.get('interval', '60')
    category = request.query.get('category', 'linear')

    url = f"https://api.bybit.com/v5/market/kline?category={category}&symbol={symbol}&interval={interval}&limit=200"
    async with aiohttp.ClientSession(headers=API_HEADERS) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return web.json_response(data, headers=HTTP_HEADERS)


async def proxy_ticker(request):
    symbol = request.query.get('symbol', 'BTCUSDT').upper()
    category = request.query.get('category', 'linear')

    url = f"https://api.bybit.com/v5/market/tickers?category={category}&symbol={symbol}"
    async with aiohttp.ClientSession(headers=API_HEADERS) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            market_type = "Фьючерсы" if category == "linear" else "Спот"
            if data.get("result") and data["result"].get("list"):
                data["result"]["list"][0]["market_type"] = market_type
            return web.json_response(data, headers=HTTP_HEADERS)


async def proxy_symbols(request):
    category = request.query.get('category', 'linear')
    url = f"https://api.bybit.com/v5/market/tickers?category={category}"
    async with aiohttp.ClientSession(headers=API_HEADERS) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            symbols = [item['symbol'] for item in data.get('result', {}).get('list', [])]
            return web.json_response({"symbols": sorted(list(set(symbols)))}, headers=HTTP_HEADERS)


async def proxy_sentiment(request):
    symbol = request.query.get('symbol', 'BTCUSDT').upper()
    # Получаем соотношение Long/Short позиций (account-ratio)
    url = f"https://api.bybit.com/v5/market/account-ratio?category=linear&symbol={symbol}&period=1h&limit=1"
    async with aiohttp.ClientSession(headers=API_HEADERS) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return web.json_response(data, headers=HTTP_HEADERS)


async def start_web_server():
    app = web.Application()

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
    })

    cors.add(app.router.add_get('/', handle_index))
    cors.add(app.router.add_get('/api/settings', api_get_settings))
    cors.add(app.router.add_post('/api/settings', api_update_settings))
    cors.add(app.router.add_get('/api/klines', proxy_klines))
    cors.add(app.router.add_get('/api/ticker', proxy_ticker))
    cors.add(app.router.add_get('/api/symbols', proxy_symbols))
    cors.add(app.router.add_get('/api/sentiment', proxy_sentiment))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logging.info("Веб-сервер запущен на порту 8080")


# --- ОБРАБОТЧИКИ БОТА ---
@dp.message(CommandStart())
async def start_cmd(message: Message):
    await Database.update_user(message.chat.id, enabled=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Открыть терминал", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])

    text = (
        "📈 **Bybit Trading Assistant**\n\n"
        "Добро пожаловать в G&G! Теперь у нас есть удобное мини-приложение для графиков.\n\n"
        "Нажми на кнопку ниже, чтобы открыть терминал."
    )
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


# --- ПЛАНИРОВЩИКИ ---
async def daily_job(bot: Bot):
    users = await Database.get_all_active_users()
    for uid, tickers in users:
        for t in tickers.split(','):
            t = t.strip().upper()
            if not t: continue

            try:
                # Получаем график (Спот, 1 час)
                klines = await fetch_klines_simple(t, interval='60', category='spot')

                # Получаем цену (Спот)
                url_ticker = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={t}"
                async with aiohttp.ClientSession(headers=API_HEADERS) as session:
                    async with session.get(url_ticker) as resp:
                        t_data = await resp.json()

                # Если данные есть, формируем старый график и отправляем
                if klines and t_data.get('retCode') == 0 and t_data['result']['list']:
                    item = t_data['result']['list'][0]
                    price = float(item['lastPrice'])
                    change = float(item['price24hPcnt']) * 100

                    chart_buf = generate_old_style_chart(klines, t)
                    photo = BufferedInputFile(chart_buf.read(), filename=f"{t}.png")

                    emoji = "🟢" if change >= 0 else "🔴"
                    caption = (
                        f"🔔 **Утренняя сводка (08:00 UTC)**\n\n"
                        f"📊 **{t}** | Таймфрейм: 1 Час\n\n"
                        f"💰 **Цена:** `{price:,.5f}`\n"
                        f"{emoji} **Изменение (24h):** `{change:+.2f}%`\n"
                        f"📌 **Рынок:** Спот"
                    )
                    await bot.send_photo(uid, photo=photo, caption=caption, parse_mode="Markdown")
                else:
                    await bot.send_message(uid, f"🔔 **Утренняя сводка**\n\nНет данных для {t} (Спот).",
                                           parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Ошибка отправки сводки для {t} пользователю {uid}: {e}")


async def forex_open_job(bot: Bot):
    users = await Database.get_all_active_users()
    for uid, _ in users:
        try:
            await bot.send_message(uid, "🌍 **Рынок Форекс открыт!**", parse_mode="Markdown")
        except:
            pass


# --- ОСНОВНОЙ ЦИКЛ ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await Database.init()

    bot = Bot(token=BOT_TOKEN)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(daily_job, trigger='cron', hour=8, minute=0, args=[bot])
    scheduler.add_job(forex_open_job, trigger='cron', day_of_week='sun', hour=22, minute=0, args=[bot])
    scheduler.start()

    asyncio.create_task(start_web_server())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
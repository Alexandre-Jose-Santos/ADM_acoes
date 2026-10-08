import os
import re
import time
import threading
import requests
from flask import Flask, jsonify
from datetime import datetime
import pytz

app = Flask(__name__)

# ─── Config via variáveis de ambiente (Render) ───────────
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID        = os.environ.get('CHAT_ID', '')
TICKERS        = [t.strip() for t in os.environ.get('TICKERS', 'ARR').upper().split(',') if t.strip()]
INTRADAY_PCT   = float(os.environ.get('INTRADAY_PCT', '1'))
INTRADAY_UP_PCT = float(os.environ.get('INTRADAY_UP_PCT', '0'))  # 0 = desativado; % de alta desde último registro pra alertar
TARGET_PRICE   = float(os.environ.get('TARGET_PRICE', '0'))
TARGET_DIR     = os.environ.get('TARGET_DIR', 'below')
DAILY_SUMMARY  = os.environ.get('DAILY_SUMMARY', 'true').lower() == 'true'
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '300'))

# Melhorias novas
MARKET_HOURS_ONLY = os.environ.get('MARKET_HOURS_ONLY', 'false').lower() == 'true'  # horário de silêncio
RECOVERY_ALERT    = os.environ.get('RECOVERY_ALERT', 'false').lower() == 'true'      # alerta de recuperação
RECOVERY_PCT      = float(os.environ.get('RECOVERY_PCT', '1'))                       # % de alta pra alertar recuperação
VOLUME_ALERT      = os.environ.get('VOLUME_ALERT', 'false').lower() == 'true'        # alerta de volume anormal
VOLUME_MULTIPLIER = float(os.environ.get('VOLUME_MULTIPLIER', '2'))                  # quantas vezes acima da média

# ─── Estado em memória (tudo já separado por ticker) ─────
last_known_price = {}
lowest_since_drop = {}  # menor preço registrado desde a última queda — usado pra recuperação
target_alerted   = {}
daily_done       = {}
volume_alerted   = {}   # evita spam de alerta de volume no mesmo dia

# ─── Chaves das APIs ─────────────────────────────────────
TWELVE_KEY  = os.environ.get('TWELVE_KEY', '')
BRAPI_TOKEN = os.environ.get('BRAPI_TOKEN', '')  # opcional (MGLU3 funciona sem token em modo de teste)

# ─── Mercados ────────────────────────────────────────────
# Horários em hora local de cada bolsa. CONFIRA os horários da B3 e ajuste se precisar.
MARKETS = {
    'US': {'tz': 'America/New_York',  'cur': '$',  'open': (9, 30),  'close': (16, 0)},
    'B3': {'tz': 'America/Sao_Paulo', 'cur': 'R$ ', 'open': (10, 0), 'close': (17, 55)},
}

def is_b3(ticker):
    """Ticker da B3: 4 letras + 1 ou 2 dígitos (MGLU3, PETR4, ITUB4, TAEE11...)."""
    return re.fullmatch(r'[A-Z]{4}\d{1,2}', ticker) is not None

def market_of(ticker):
    return MARKETS['B3'] if is_b3(ticker) else MARKETS['US']

def intraday_pct(ticker):
    """Permite um limite próprio por ação, ex.: variável INTRADAY_PCT_MGLU3=1"""
    return float(os.environ.get(f'INTRADAY_PCT_{ticker}', INTRADAY_PCT))

def intraday_up_pct(ticker):
    return float(os.environ.get(f'INTRADAY_UP_PCT_{ticker}', INTRADAY_UP_PCT))

def minutes_now(mkt):
    now = datetime.now(pytz.timezone(mkt['tz']))
    return now, now.hour * 60 + now.minute

def session_state(mkt):
    now, mins = minutes_now(mkt)
    o = mkt['open'][0] * 60 + mkt['open'][1]
    c = mkt['close'][0] * 60 + mkt['close'][1]
    if now.weekday() >= 5:
        return 'FECHADO'
    if mins < o:
        return 'PRE'
    if mins <= c:
        return 'REGULAR'
    return 'POST'

# ─── Busca de preço ──────────────────────────────────────
def fetch_price_twelve(ticker):
    sess = requests.Session()
    sess.headers.update({'User-Agent': 'curl/8.0', 'Accept': '*/*'})

    url = f'https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVE_KEY}'
    # Não imprime a URL: ela contém a chave da API
    print(f'[TWELVE] GET quote {ticker}', flush=True)
    try:
        r = sess.get(url, timeout=(5, 10))
        print(f'[TWELVE] Status HTTP: {r.status_code}', flush=True)
    except Exception as e:
        print(f'[TWELVE] EXCEPTION na requisição: {type(e).__name__}: {e}', flush=True)
        raise
    data = r.json()
    print(f'[TWELVE] Resposta (quote): {data}', flush=True)

    if 'close' not in data:
        raise Exception(f'Sem dados para {ticker}: {data}')

    price      = float(data['close'])
    open_price = float(data.get('open', price))
    prev_close = float(data.get('previous_close', price))
    day_high   = float(data.get('high', price))
    day_low    = float(data.get('low', price))
    volume     = float(data.get('volume', 0))
    avg_volume = float(data.get('average_volume', 0))

    return {
        'ticker': ticker, 'name': ticker, 'price': price, 'open': open_price,
        'prev_close': prev_close, 'day_high': day_high, 'day_low': day_low,
        'volume': volume, 'avg_volume': avg_volume,
    }

def fetch_price_brapi(ticker):
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    if BRAPI_TOKEN:
        headers['Authorization'] = f'Bearer {BRAPI_TOKEN}'

    url = f'https://brapi.dev/api/quote/{ticker}'
    print(f'[BRAPI] GET quote {ticker}', flush=True)
    try:
        r = requests.get(url, headers=headers, timeout=(5, 10))
        print(f'[BRAPI] Status HTTP: {r.status_code}', flush=True)
    except Exception as e:
        print(f'[BRAPI] EXCEPTION na requisição: {type(e).__name__}: {e}', flush=True)
        raise
    data = r.json()
    results = data.get('results') or []
    if r.status_code != 200 or not results:
        raise Exception(f'Sem dados para {ticker} (HTTP {r.status_code}): {data}')

    q = results[0]
    print(f'[BRAPI] {ticker}: preço={q.get("regularMarketPrice")} '
          f'tempo={q.get("regularMarketTime")}', flush=True)

    price = float(q['regularMarketPrice'])
    return {
        'ticker':     ticker,
        'name':       q.get('shortName') or q.get('longName') or ticker,
        'price':      price,
        'open':       float(q.get('regularMarketOpen') or price),
        'prev_close': float(q.get('regularMarketPreviousClose') or price),
        'day_high':   float(q.get('regularMarketDayHigh') or price),
        'day_low':    float(q.get('regularMarketDayLow') or price),
        'volume':     float(q.get('regularMarketVolume') or 0),
        'avg_volume': float(q.get('averageDailyVolume3Month') or 0),
    }

def fetch_price(ticker):
    mkt = market_of(ticker)
    data = fetch_price_brapi(ticker) if is_b3(ticker) else fetch_price_twelve(ticker)
    data['state'] = session_state(mkt)
    data['cur']   = mkt['cur']
    data['mkt']   = mkt
    return data

# ─── Telegram ────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    r = requests.post(
        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
        json={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'HTML', 'disable_web_page_preview': True},
        timeout=10
    )
    ok = r.json().get('ok', False)
    print(f'[TELEGRAM] {"✅ Enviado" if ok else "❌ Erro: " + str(r.json())}')
    return ok

# ─── Checagem por ticker ─────────────────────────────────
def check_ticker(ticker):
    data       = fetch_price(ticker)
    price      = data['price']
    prev_close = data['prev_close']
    cur        = data['cur']
    mkt        = data['mkt']
    pct_day    = ((price - prev_close) / prev_close * 100) if prev_close else 0
    now_str    = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    today      = datetime.now().strftime('%Y-%m-%d')

    print(f'[{ticker}] {cur}{price:.2f} ({pct_day:+.2f}%) | {data["state"]}')

    # ── Opção 1: Queda/Alta acumulada no dia (vs. fechamento anterior) ──
    # Alerta a cada NÍVEL de X% atingido, sempre comparando com o fechamento anterior.
    down_step = intraday_pct(ticker)
    if down_step > 0:
        level_reached = int(abs(pct_day) // down_step) if pct_day < 0 else 0
        alert_key = f'{ticker}_down_{today}_{level_reached}'
        if level_reached >= 1 and not target_alerted.get(alert_key):
            target_alerted[alert_key] = True
            send_telegram(
                f'📉 <b>QUEDA NO DIA — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Preço atual: <b>{cur}{price:.2f}</b>\n'
                f'📌 Fechamento anterior: {cur}{prev_close:.2f}\n'
                f'📉 Variação acumulada: <b>{pct_day:.2f}%</b>\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

    up_step = intraday_up_pct(ticker)
    if up_step > 0:
        level_reached_up = int(pct_day // up_step) if pct_day > 0 else 0
        alert_key_up = f'{ticker}_up_{today}_{level_reached_up}'
        if level_reached_up >= 1 and not target_alerted.get(alert_key_up):
            target_alerted[alert_key_up] = True
            send_telegram(
                f'📈 <b>ALTA NO DIA — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Preço atual: <b>{cur}{price:.2f}</b>\n'
                f'📌 Fechamento anterior: {cur}{prev_close:.2f}\n'
                f'📈 Variação acumulada: <b>+{pct_day:.2f}%</b>\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

    last_known_price[ticker] = price

    # ── Melhoria: Alerta de recuperação ───────────────────
    if RECOVERY_ALERT:
        lowest = lowest_since_drop.get(ticker)
        if lowest is None or price < lowest:
            lowest_since_drop[ticker] = price
            lowest = price
        else:
            recovery = ((price - lowest) / lowest * 100)
            if recovery >= RECOVERY_PCT:
                send_telegram(
                    f'📈 <b>RECUPERAÇÃO — {ticker}</b>\n\n'
                    f'🏷 <b>{data["name"]}</b>\n'
                    f'💵 Preço atual: <b>{cur}{price:.2f}</b>\n'
                    f'📌 Mínima recente: {cur}{lowest:.2f}\n'
                    f'📈 Recuperação: <b>+{recovery:.2f}%</b>\n'
                    f'📊 Variação no dia: {pct_day:.2f}%\n'
                    f'⏰ {now_str}\n'
                    f'<i>⚠️ Não é recomendação de investimento.</i>'
                )
                lowest_since_drop[ticker] = price  # reseta a base para próxima recuperação

    # ── Melhoria: Alerta de volume anormal ────────────────
    if VOLUME_ALERT:
        volume     = data.get('volume', 0)
        avg_volume = data.get('avg_volume', 0)
        vol_key    = f'{ticker}_{today}'
        if avg_volume > 0 and volume >= avg_volume * VOLUME_MULTIPLIER and not volume_alerted.get(vol_key):
            volume_alerted[vol_key] = True
            vol_ratio = volume / avg_volume
            send_telegram(
                f'📊 <b>VOLUME ANORMAL — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Preço atual: <b>{cur}{price:.2f}</b>\n'
                f'📦 Volume hoje: <b>{volume:,.0f}</b>\n'
                f'📌 Média: {avg_volume:,.0f}\n'
                f'⚡ <b>{vol_ratio:.1f}x acima da média</b>\n'
                f'📊 Variação no dia: {pct_day:.2f}%\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Pode indicar movimento forte. Não é recomendação de investimento.</i>'
            )

    # ── Opção 2: Faixa de preço alvo ─────────────────────
    # Obs.: TARGET_PRICE é um valor único e vale para todos os tickers.
    # Se for pensado para a ARR (em US$), pode disparar errado na MGLU3 (em R$).
    # Para alvo próprio: variáveis TARGET_PRICE_MGLU3 e TARGET_DIR_MGLU3.
    t_price = float(os.environ.get(f'TARGET_PRICE_{ticker}', TARGET_PRICE))
    t_dir   = os.environ.get(f'TARGET_DIR_{ticker}', TARGET_DIR)
    if t_price > 0:
        crossed = price < t_price if t_dir == 'below' else price > t_price
        key = f'{ticker}_{t_dir}_{t_price}'
        if crossed and not target_alerted.get(key):
            target_alerted[key] = True
            dir_label = 'caiu abaixo de' if t_dir == 'below' else 'subiu acima de'
            send_telegram(
                f'{"📉" if t_dir == "below" else "📈"} <b>ALERTA DE PREÇO ALVO — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Preço atual: <b>{cur}{price:.2f}</b>\n'
                f'🎯 Cruzou o alvo: <b>{dir_label} {cur}{t_price:.2f}</b>\n'
                f'📊 Variação no dia: {pct_day:.2f}%\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )
        if not crossed and target_alerted.get(key):
            del target_alerted[key]

    # ── Opção 3: Resumo diário (no horário de cada bolsa) ─
    if DAILY_SUMMARY:
        local_now, mins = minutes_now(mkt)
        open_min  = mkt['open'][0] * 60 + mkt['open'][1]
        close_min = mkt['close'][0] * 60 + mkt['close'][1]
        open_lbl  = f'{mkt["open"][0]}h{mkt["open"][1]:02d}'
        close_lbl = f'{mkt["close"][0]}h{mkt["close"][1]:02d}'

        if 0 <= mins - open_min <= 5 and local_now.weekday() < 5 \
                and not daily_done.get(f'{ticker}_open_{today}'):
            daily_done[f'{ticker}_open_{today}'] = True
            open_pct = ((data['open'] - prev_close) / prev_close * 100) if prev_close else 0
            send_telegram(
                f'🔔 <b>ABERTURA DO PREGÃO — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Abertura: <b>{cur}{data["open"]:.2f}</b>\n'
                f'📌 Fech. anterior: {cur}{prev_close:.2f}\n'
                f'📊 Variação inicial: {open_pct:.2f}%\n'
                f'⏰ {now_str} ({open_lbl} local)\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

        if 0 <= mins - close_min <= 5 and local_now.weekday() < 5 \
                and not daily_done.get(f'{ticker}_close_{today}'):
            daily_done[f'{ticker}_close_{today}'] = True
            send_telegram(
                f'{"📉" if pct_day < 0 else "📈"} <b>FECHAMENTO DO PREGÃO — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Fechamento: <b>{cur}{price:.2f}</b>\n'
                f'📌 Abertura: {cur}{data["open"]:.2f}\n'
                f'📊 Variação no dia: <b>{pct_day:.2f}%</b>\n'
                f'📈 Máxima: {cur}{data["day_high"]:.2f} · 📉 Mínima: {cur}{data["day_low"]:.2f}\n'
                f'⏰ {now_str} ({close_lbl} local)\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

# ─── Loop principal ───────────────────────────────────────
def is_market_hours(ticker):
    """True se a bolsa do ticker está em pregão (dias úteis). EUA inclui after-hours até 20h ET."""
    mkt = market_of(ticker)
    now, mins = minutes_now(mkt)
    if now.weekday() >= 5:
        return False
    open_min = mkt['open'][0] * 60 + mkt['open'][1]
    if is_b3(ticker):
        close_min = mkt['close'][0] * 60 + mkt['close'][1]
    else:
        close_min = 20 * 60  # inclui after-hours até 20h ET
    return open_min <= mins <= close_min

def monitor_loop():
    print(f'[MONITOR] Tickers: {TICKERS} | Intervalo: {CHECK_INTERVAL}s')
    print(f'[MONITOR] TWELVE_KEY configurada: {"SIM" if TWELVE_KEY else "NAO"}')
    print(f'[MONITOR] BRAPI_TOKEN configurado: {"SIM" if BRAPI_TOKEN else "NAO (modo teste)"}')
    print(f'[MONITOR] Horário de silêncio: {"ATIVO (só pregão)" if MARKET_HOURS_ONLY else "INATIVO (24h)"}')
    print(f'[MONITOR] Alerta de recuperação: {"ATIVO" if RECOVERY_ALERT else "INATIVO"} ({RECOVERY_PCT}%)')
    print(f'[MONITOR] Alerta de volume: {"ATIVO" if VOLUME_ALERT else "INATIVO"} ({VOLUME_MULTIPLIER}x)')
    while True:
        print(f'[MONITOR] Iniciando checagem... {datetime.now()}')
        for ticker in TICKERS:
            if MARKET_HOURS_ONLY and not is_market_hours(ticker):
                print(f'[MONITOR] {ticker}: fora do horário de pregão — pulando')
                continue
            try:
                print(f'[MONITOR] Checando {ticker}...')
                check_ticker(ticker)
            except Exception as e:
                import traceback
                print(f'[ERRO] {ticker}: {e}')
                print(traceback.format_exc())
        print(f'[MONITOR] Aguardando {CHECK_INTERVAL}s...')
        time.sleep(CHECK_INTERVAL)

# ─── Flask (keepalive + status) ───────────────────────────
@app.route('/')
def index():
    return jsonify({'status': 'online', 'tickers': TICKERS, 'precos': last_known_price})

@app.route('/ping')
def ping():
    return 'ok', 200

@app.route('/status')
def status():
    return jsonify(last_known_price)

if __name__ == '__main__':
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

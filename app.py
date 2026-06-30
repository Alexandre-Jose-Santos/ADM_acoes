import os
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
TICKERS        = [t.strip() for t in os.environ.get('TICKERS', 'ARR').upper().split(',')]
INTRADAY_PCT   = float(os.environ.get('INTRADAY_PCT', '1'))
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

# ─── Estado em memória ───────────────────────────────────
last_known_price = {}
lowest_since_drop = {}  # menor preço registrado desde a última queda — usado pra recuperação
target_alerted   = {}
daily_done       = {}
volume_alerted   = {}   # evita spam de alerta de volume no mesmo dia

# ─── Twelve Data ─────────────────────────────────────────
TWELVE_KEY = os.environ.get('TWELVE_KEY', '')

def fetch_price(ticker):
    sess = requests.Session()
    sess.headers.update({'User-Agent': 'curl/8.0', 'Accept': '*/*'})

    url = f'https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVE_KEY}'
    print(f'[TWELVE] GET {url}', flush=True)
    try:
        r = sess.get(url, timeout=(5, 10))
        print(f'[TWELVE] Status HTTP: {r.status_code}', flush=True)
    except Exception as e:
        print(f'[TWELVE] EXCEPTION na requisição: {type(e).__name__}: {e}', flush=True)
        raise
    data = r.json()
    print(f'[TWELVE] Resposta completa (quote): {data}', flush=True)

    if 'close' not in data:
        raise Exception(f'Sem dados para {ticker}: {data}')

    price      = float(data['close'])
    open_price = float(data.get('open', price))
    prev_close = float(data.get('previous_close', price))
    day_high   = float(data.get('high', price))
    day_low    = float(data.get('low', price))
    volume     = float(data.get('volume', 0))
    avg_volume = float(data.get('average_volume', 0))

    et_now = datetime.now(pytz.timezone('America/New_York'))
    h, m = et_now.hour, et_now.minute
    if (h == 9 and m >= 30) or (10 <= h <= 15) or (h == 16 and m == 0):
        state = 'REGULAR'
    elif h < 9 or (h == 9 and m < 30):
        state = 'PRE'
    else:
        state = 'POST'
    return {
        'ticker':     ticker,
        'name':       ticker,
        'price':      price,
        'open':       open_price,
        'prev_close': prev_close,
        'day_high':   day_high,
        'day_low':    day_low,
        'volume':     volume,
        'avg_volume': avg_volume,
        'state':      state,
    }

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
    pct_day    = ((price - prev_close) / prev_close * 100) if prev_close else 0
    now_str    = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    today      = datetime.now().strftime('%Y-%m-%d')

    print(f'[{ticker}] ${price:.2f} ({pct_day:+.2f}%) | {data["state"]}')

    # ── Opção 1: Queda intraday ───────────────────────────
    last = last_known_price.get(ticker)
    if last is not None:
        drop = ((price - last) / last * 100)
        if drop <= -INTRADAY_PCT:
            send_telegram(
                f'📉 <b>QUEDA INTRADAY — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Preço atual: <b>${price:.2f}</b>\n'
                f'📌 Último registrado: ${last:.2f}\n'
                f'📉 Variação: <b>{drop:.2f}%</b>\n'
                f'📊 Variação no dia: {pct_day:.2f}%\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )
            last_known_price[ticker] = price
    else:
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
                    f'💵 Preço atual: <b>${price:.2f}</b>\n'
                    f'📌 Mínima recente: ${lowest:.2f}\n'
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
                f'💵 Preço atual: <b>${price:.2f}</b>\n'
                f'📦 Volume hoje: <b>{volume:,.0f}</b>\n'
                f'📌 Média: {avg_volume:,.0f}\n'
                f'⚡ <b>{vol_ratio:.1f}x acima da média</b>\n'
                f'📊 Variação no dia: {pct_day:.2f}%\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Pode indicar movimento forte. Não é recomendação de investimento.</i>'
            )

    # ── Opção 2: Faixa de preço alvo ─────────────────────
    if TARGET_PRICE > 0:
        crossed = price < TARGET_PRICE if TARGET_DIR == 'below' else price > TARGET_PRICE
        key = f'{ticker}_{TARGET_DIR}_{TARGET_PRICE}'
        if crossed and not target_alerted.get(key):
            target_alerted[key] = True
            dir_label = 'caiu abaixo de' if TARGET_DIR == 'below' else 'subiu acima de'
            send_telegram(
                f'{"📉" if TARGET_DIR == "below" else "📈"} <b>ALERTA DE PREÇO ALVO — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Preço atual: <b>${price:.2f}</b>\n'
                f'🎯 Cruzou o alvo: <b>{dir_label} ${TARGET_PRICE:.2f}</b>\n'
                f'📊 Variação no dia: {pct_day:.2f}%\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )
        if not crossed and target_alerted.get(key):
            del target_alerted[key]

    # ── Opção 3: Resumo diário ────────────────────────────
    if DAILY_SUMMARY:
        et_now = datetime.now(pytz.timezone('America/New_York'))
        h, m   = et_now.hour, et_now.minute

        if h == 9 and 30 <= m <= 35 and not daily_done.get(f'{ticker}_open_{today}'):
            daily_done[f'{ticker}_open_{today}'] = True
            open_pct = ((data['open'] - prev_close) / prev_close * 100) if prev_close else 0
            send_telegram(
                f'🔔 <b>ABERTURA DO PREGÃO — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Abertura: <b>${data["open"]:.2f}</b>\n'
                f'📌 Fech. anterior: ${prev_close:.2f}\n'
                f'📊 Variação inicial: {open_pct:.2f}%\n'
                f'⏰ {now_str} (9h30 ET)\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

        if h == 16 and 0 <= m <= 5 and not daily_done.get(f'{ticker}_close_{today}'):
            daily_done[f'{ticker}_close_{today}'] = True
            send_telegram(
                f'{"📉" if pct_day < 0 else "📈"} <b>FECHAMENTO DO PREGÃO — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Fechamento: <b>${price:.2f}</b>\n'
                f'📌 Abertura: ${data["open"]:.2f}\n'
                f'📊 Variação no dia: <b>{pct_day:.2f}%</b>\n'
                f'📈 Máxima: ${data["day_high"]:.2f} · 📉 Mínima: ${data["day_low"]:.2f}\n'
                f'⏰ {now_str} (16h ET)\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

# ─── Loop principal ───────────────────────────────────────
def is_market_hours():
    """Retorna True se está dentro do pregão (9h30-16h ET, dias úteis)."""
    et_now = datetime.now(pytz.timezone('America/New_York'))
    if et_now.weekday() >= 5:  # sábado=5, domingo=6
        return False
    h, m = et_now.hour, et_now.minute
    after_open  = (h > 9) or (h == 9 and m >= 30)
    before_close = (h < 16)
    return after_open and before_close

def monitor_loop():
    print(f'[MONITOR] Tickers: {TICKERS} | Intervalo: {CHECK_INTERVAL}s')
    print(f'[MONITOR] TWELVE_KEY configurada: {"SIM" if TWELVE_KEY else "NAO"}')
    print(f'[MONITOR] Horário de silêncio: {"ATIVO (só pregão)" if MARKET_HOURS_ONLY else "INATIVO (24h)"}')
    print(f'[MONITOR] Alerta de recuperação: {"ATIVO" if RECOVERY_ALERT else "INATIVO"} ({RECOVERY_PCT}%)')
    print(f'[MONITOR] Alerta de volume: {"ATIVO" if VOLUME_ALERT else "INATIVO"} ({VOLUME_MULTIPLIER}x)')
    while True:
        if MARKET_HOURS_ONLY and not is_market_hours():
            print(f'[MONITOR] Fora do horário de pregão — aguardando... {datetime.now()}')
            time.sleep(CHECK_INTERVAL)
            continue
        print(f'[MONITOR] Iniciando checagem... {datetime.now()}')
        for ticker in TICKERS:
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

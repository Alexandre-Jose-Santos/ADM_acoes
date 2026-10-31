import os
import re
import time
import threading
import requests
import telebot
from telebot import types
from contextlib import contextmanager
from flask import Flask, jsonify, request
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

# ─── Config via variáveis de ambiente (Render) ───────────
TELEGRAM_TOKEN  = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID         = os.environ.get('CHAT_ID', '')          # seu chat (dono do bot)
TICKERS         = [t.strip() for t in os.environ.get('TICKERS', 'ARR').upper().split(',') if t.strip()]
INTRADAY_PCT    = float(os.environ.get('INTRADAY_PCT', '1'))
INTRADAY_UP_PCT = float(os.environ.get('INTRADAY_UP_PCT', '0'))
TARGET_PRICE    = float(os.environ.get('TARGET_PRICE', '0'))   # só vale para o dono
TARGET_DIR      = os.environ.get('TARGET_DIR', 'below')
DAILY_SUMMARY   = os.environ.get('DAILY_SUMMARY', 'true').lower() == 'true'
CHECK_INTERVAL  = int(os.environ.get('CHECK_INTERVAL', '300'))

MARKET_HOURS_ONLY = os.environ.get('MARKET_HOURS_ONLY', 'false').lower() == 'true'
RECOVERY_ALERT    = os.environ.get('RECOVERY_ALERT', 'false').lower() == 'true'
RECOVERY_PCT      = float(os.environ.get('RECOVERY_PCT', '1'))
VOLUME_ALERT      = os.environ.get('VOLUME_ALERT', 'false').lower() == 'true'
VOLUME_MULTIPLIER = float(os.environ.get('VOLUME_MULTIPLIER', '2'))

# ─── Assinatura / banco / pagamento ──────────────────────
DATABASE_URL  = os.environ.get('DATABASE_URL', '')       # sem isso, roda no modo antigo (só você)
HAS_DB        = bool(DATABASE_URL)
ASAAS_KEY     = os.environ.get('ASAAS_KEY', '')
ASAAS_URL     = os.environ.get('ASAAS_URL', 'https://api-sandbox.asaas.com/v3')  # produção: https://api.asaas.com/v3
WEBHOOK_TOKEN = os.environ.get('WEBHOOK_TOKEN', '')      # token do webhook do Asaas
TG_SECRET     = os.environ.get('TG_SECRET', '')          # parte secreta da URL do webhook do Telegram
PUBLIC_URL    = os.environ.get('PUBLIC_URL', '') or os.environ.get('RENDER_EXTERNAL_URL', '')
PRECO         = float(os.environ.get('ASSINATURA_PRECO', '20.00'))
TRIAL_DIAS    = int(os.environ.get('TRIAL_DIAS', '7'))          # dias de teste grátis
LIMITE_ATIVOS = int(os.environ.get('LIMITE_ATIVOS', '30'))
KB_ASSINAR    = {'inline_keyboard': [[{'text': f'⭐ Assinar (R$ {PRECO:.2f}/mês)', 'callback_data': 'assinar'}]]}

# ─── Estado em memória (chaves por usuário+ticker) ───────
last_known_price  = {}
lowest_since_drop = {}
target_alerted    = {}
daily_done        = {}
volume_alerted    = {}

# ─── Chaves das APIs ─────────────────────────────────────
TWELVE_KEY  = os.environ.get('TWELVE_KEY', '')
BRAPI_TOKEN = os.environ.get('BRAPI_TOKEN', '')

# ─── Mercados ────────────────────────────────────────────
MARKETS = {
    'US': {'tz': 'America/New_York',  'cur': '$',  'open': (9, 30),  'close': (16, 0)},
    'B3': {'tz': 'America/Sao_Paulo', 'cur': 'R$ ', 'open': (10, 0), 'close': (17, 55)},
}

def is_b3(ticker):
    return re.fullmatch(r'[A-Z]{4}\d{1,2}', ticker) is not None

def market_of(ticker):
    return MARKETS['B3'] if is_b3(ticker) else MARKETS['US']

def intraday_pct(ticker):
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

# ─── Banco de dados ──────────────────────────────────────
if HAS_DB:
    import psycopg2

@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:                      # commit ao sair, rollback em erro
            with conn.cursor() as cur:
                yield cur
    finally:
        conn.close()

def init_db():
    with db() as cur:
        cur.execute("""
            create table if not exists users (
              telegram_id bigint primary key,
              plan text default 'free',
              plan_until timestamptz,
              asaas_customer text,
              created_at timestamptz default now());
            create table if not exists watchlist (
              id serial primary key,
              telegram_id bigint references users(telegram_id),
              ticker text not null,
              unique (telegram_id, ticker));
            create table if not exists payments (
              id text primary key,
              telegram_id bigint references users(telegram_id),
              amount numeric,
              status text default 'pending',
              created_at timestamptz default now());
        """)
        cur.execute("alter table users add column if not exists warned boolean default false")

def seed_owner():
    """Garante que VOCÊ é Pro (sem vencimento) e tem seus tickers do env na lista."""
    if not CHAT_ID:
        return
    uid = int(CHAT_ID)
    with db() as cur:
        cur.execute("""insert into users(telegram_id, plan, plan_until)
                       values (%s, 'pro', '2099-01-01')
                       on conflict (telegram_id)
                       do update set plan='pro', plan_until='2099-01-01'""", (uid,))
        cur.execute("select count(*) from watchlist where telegram_id=%s", (uid,))
        if cur.fetchone()[0] == 0:          # só semeia se a lista estiver vazia
            for t in TICKERS:
                cur.execute("""insert into watchlist(telegram_id, ticker) values (%s,%s)
                               on conflict do nothing""", (uid, t))

def ensure_user(uid):
    """Cria o usuário (já com o teste grátis) se for o primeiro contato."""
    with db() as cur:
        cur.execute("""insert into users(telegram_id, plan, plan_until)
                       values (%s, 'pro', now() + %s * interval '1 day')
                       on conflict do nothing""", (uid, TRIAL_DIAS))
        cur.execute("select plan, plan_until, asaas_customer from users where telegram_id=%s", (uid,))
        return cur.fetchone()

def acesso_ativo(plan, until):
    return plan == 'pro' and until is not None and until > datetime.now(pytz.utc)

def alvos():
    """{ticker: [(chat_id, plano), ...]}"""
    if not HAS_DB:
        return {t: [(CHAT_ID, 'pro')] for t in TICKERS} if CHAT_ID else {}
    out = {}
    with db() as cur:
        cur.execute("""select w.ticker, w.telegram_id, u.plan
                       from watchlist w join users u using (telegram_id)
                       where u.plan='pro' and u.plan_until > now()""")
        for t, uid, plan in cur.fetchall():
            out.setdefault(t, []).append((uid, plan))
    return out

def checar_vencidos():
    with db() as cur:
        cur.execute("""update users set plan='free'
                       where plan='pro' and plan_until < now()
                       returning telegram_id""")
        vencidos = [r[0] for r in cur.fetchall()]
        cur.execute("""update users set warned=true
                       where plan='pro' and warned=false
                         and plan_until > now() and plan_until < now() + interval '1 day'
                       returning telegram_id, plan_until""")
        avisos = cur.fetchall()
    sp = pytz.timezone('America/Sao_Paulo')
    for uid in vencidos:
        send_telegram(f'⏳ Seu acesso terminou. Para continuar recebendo os alertas, '
                      f'assine por R$ {PRECO:.2f}/mês:', uid, KB_ASSINAR)
    for uid, ate in avisos:
        send_telegram(f'⚠️ Seu acesso termina em {ate.astimezone(sp):%d/%m às %H:%M}. '
                      f'Para não ficar sem alertas, renove por R$ {PRECO:.2f}/mês:', uid, KB_ASSINAR)

def ativar_pro(pay_id, dias=30):
    """Idempotente: só ativa se o pagamento ainda estiver 'pending'."""
    with db() as cur:
        cur.execute("""update payments set status='paid'
                       where id=%s and status='pending' returning telegram_id""", (pay_id,))
        row = cur.fetchone()
        if not row:
            return None
        uid = row[0]
        cur.execute("""update users set plan='pro',
                       plan_until = greatest(coalesce(plan_until, now()), now()) + interval '30 days', warned=false
                       where telegram_id=%s""", (uid,))
    return uid

# ─── Busca de preço ──────────────────────────────────────
def fetch_price_twelve(ticker):
    sess = requests.Session()
    sess.headers.update({'User-Agent': 'curl/8.0', 'Accept': '*/*'})
    url = f'https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVE_KEY}'
    print(f'[TWELVE] GET quote {ticker}', flush=True)
    try:
        r = sess.get(url, timeout=(5, 10))
        print(f'[TWELVE] Status HTTP: {r.status_code}', flush=True)
    except Exception as e:
        print(f'[TWELVE] EXCEPTION na requisição: {type(e).__name__}: {e}', flush=True)
        raise
    data = r.json()
    if 'close' not in data:
        raise Exception(f'Sem dados para {ticker}: {data}')

    price = float(data['close'])
    return {
        'ticker': ticker, 'name': data.get('name') or ticker, 'price': price,
        'open': float(data.get('open', price)),
        'prev_close': float(data.get('previous_close', price)),
        'day_high': float(data.get('high', price)),
        'day_low': float(data.get('low', price)),
        'volume': float(data.get('volume', 0)),
        'avg_volume': float(data.get('average_volume', 0)),
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

# ─── Telegram (envio) ────────────────────────────────────
def send_telegram(msg, chat_id=None, markup=None):
    chat_id = chat_id or CHAT_ID
    if not TELEGRAM_TOKEN or not chat_id:
        return False
    payload = {'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML',
               'disable_web_page_preview': True}
    if markup:
        payload['reply_markup'] = markup
    try:
        r = requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                          json=payload, timeout=10)
        ok = r.json().get('ok', False)
        print(f'[TELEGRAM] {chat_id}: {"✅ Enviado" if ok else "❌ Erro: " + str(r.json())}')
        return ok
    except Exception as e:
        print(f'[TELEGRAM] Falha ao enviar para {chat_id}: {e}')
        return False

# ─── Checagem por ticker e usuário ───────────────────────
def check_ticker(ticker, data, chat_id, plan='free'):
    price      = data['price']
    prev_close = data['prev_close']
    cur        = data['cur']
    mkt        = data['mkt']
    pct_day    = ((price - prev_close) / prev_close * 100) if prev_close else 0
    now_str    = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    today      = datetime.now().strftime('%Y-%m-%d')

    uk       = f'{chat_id}_{ticker}'          # chave única por usuário+ticker
    is_pro   = (plan == 'pro')
    is_owner = bool(CHAT_ID) and str(chat_id) == str(CHAT_ID)

    def send(msg):
        return send_telegram(msg, chat_id)

    print(f'[{ticker}] {cur}{price:.2f} ({pct_day:+.2f}%) | {data["state"]} | user={chat_id}')

    # ── Queda/Alta acumulada no dia (todos os planos) ──
    down_step = intraday_pct(ticker)
    if down_step > 0:
        level_reached = int(abs(pct_day) // down_step) if pct_day < 0 else 0
        alert_key = f'{uk}_down_{today}_{level_reached}'
        if level_reached >= 1 and not target_alerted.get(alert_key):
            target_alerted[alert_key] = True
            send(
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
        alert_key_up = f'{uk}_up_{today}_{level_reached_up}'
        if level_reached_up >= 1 and not target_alerted.get(alert_key_up):
            target_alerted[alert_key_up] = True
            send(
                f'📈 <b>ALTA NO DIA — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Preço atual: <b>{cur}{price:.2f}</b>\n'
                f'📌 Fechamento anterior: {cur}{prev_close:.2f}\n'
                f'📈 Variação acumulada: <b>+{pct_day:.2f}%</b>\n'
                f'⏰ {now_str}\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

    last_known_price[ticker] = price

    # ── Recuperação (só Pro) ──
    if RECOVERY_ALERT and is_pro:
        lowest = lowest_since_drop.get(uk)
        if lowest is None or price < lowest:
            lowest_since_drop[uk] = price
        else:
            recovery = ((price - lowest) / lowest * 100)
            if recovery >= RECOVERY_PCT:
                send(
                    f'📈 <b>RECUPERAÇÃO — {ticker}</b>\n\n'
                    f'🏷 <b>{data["name"]}</b>\n'
                    f'💵 Preço atual: <b>{cur}{price:.2f}</b>\n'
                    f'📌 Mínima recente: {cur}{lowest:.2f}\n'
                    f'📈 Recuperação: <b>+{recovery:.2f}%</b>\n'
                    f'📊 Variação no dia: {pct_day:.2f}%\n'
                    f'⏰ {now_str}\n'
                    f'<i>⚠️ Não é recomendação de investimento.</i>'
                )
                lowest_since_drop[uk] = price

    # ── Volume anormal (só Pro) ──
    if VOLUME_ALERT and is_pro:
        volume     = data.get('volume', 0)
        avg_volume = data.get('avg_volume', 0)
        vol_key    = f'{uk}_{today}'
        if avg_volume > 0 and volume >= avg_volume * VOLUME_MULTIPLIER and not volume_alerted.get(vol_key):
            volume_alerted[vol_key] = True
            vol_ratio = volume / avg_volume
            send(
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

    # ── Preço alvo (só dono, pois o valor vem das variáveis de ambiente) ──
    if is_owner:
        t_price = float(os.environ.get(f'TARGET_PRICE_{ticker}', TARGET_PRICE))
        t_dir   = os.environ.get(f'TARGET_DIR_{ticker}', TARGET_DIR)
        if t_price > 0:
            crossed = price < t_price if t_dir == 'below' else price > t_price
            key = f'{uk}_{t_dir}_{t_price}'
            if crossed and not target_alerted.get(key):
                target_alerted[key] = True
                dir_label = 'caiu abaixo de' if t_dir == 'below' else 'subiu acima de'
                send(
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

    # ── Resumo diário: abertura e fechamento (só Pro) ──
    if DAILY_SUMMARY and is_pro:
        local_now, mins = minutes_now(mkt)
        open_min  = mkt['open'][0] * 60 + mkt['open'][1]
        close_min = mkt['close'][0] * 60 + mkt['close'][1]
        open_lbl  = f'{mkt["open"][0]}h{mkt["open"][1]:02d}'
        close_lbl = f'{mkt["close"][0]}h{mkt["close"][1]:02d}'

        if 0 <= mins - open_min <= 5 and local_now.weekday() < 5 \
                and not daily_done.get(f'{uk}_open_{today}'):
            daily_done[f'{uk}_open_{today}'] = True
            open_pct = ((data['open'] - prev_close) / prev_close * 100) if prev_close else 0
            send(
                f'🔔 <b>ABERTURA DO PREGÃO — {ticker}</b>\n\n'
                f'🏷 <b>{data["name"]}</b>\n'
                f'💵 Abertura: <b>{cur}{data["open"]:.2f}</b>\n'
                f'📌 Fech. anterior: {cur}{prev_close:.2f}\n'
                f'📊 Variação inicial: {open_pct:.2f}%\n'
                f'⏰ {now_str} ({open_lbl} local)\n'
                f'<i>⚠️ Não é recomendação de investimento.</i>'
            )

        if 0 <= mins - close_min <= 5 and local_now.weekday() < 5 \
                and not daily_done.get(f'{uk}_close_{today}'):
            daily_done[f'{uk}_close_{today}'] = True
            send(
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
    mkt = market_of(ticker)
    now, mins = minutes_now(mkt)
    if now.weekday() >= 5:
        return False
    open_min = mkt['open'][0] * 60 + mkt['open'][1]
    if is_b3(ticker):
        close_min = mkt['close'][0] * 60 + mkt['close'][1]
    else:
        close_min = 20 * 60
    return open_min <= mins <= close_min

def monitor_loop():
    print(f'[MONITOR] Modo: {"MULTIUSUÁRIO (banco)" if HAS_DB else "LEGADO (só dono)"} | Intervalo: {CHECK_INTERVAL}s')
    print(f'[MONITOR] TWELVE_KEY: {"SIM" if TWELVE_KEY else "NAO"} | BRAPI_TOKEN: {"SIM" if BRAPI_TOKEN else "NAO (modo teste)"}')
    print(f'[MONITOR] Silêncio fora do pregão: {"ATIVO" if MARKET_HOURS_ONLY else "INATIVO"} | '
          f'Recuperação: {"ATIVO" if RECOVERY_ALERT else "INATIVO"} | Volume: {"ATIVO" if VOLUME_ALERT else "INATIVO"}')
    while True:
        print(f'[MONITOR] Iniciando checagem... {datetime.now()}')
        if HAS_DB:
            try:
                checar_vencidos()
            except Exception as e:
                print(f'[ERRO] checar_vencidos: {e}')
        try:
            mapa = alvos()
        except Exception as e:
            print(f'[ERRO] alvos: {e}')
            mapa = {}

        for ticker, users in mapa.items():
            if MARKET_HOURS_ONLY and not is_market_hours(ticker):
                print(f'[MONITOR] {ticker}: fora do horário de pregão — pulando')
                continue
            try:
                data = fetch_price(ticker)       # 1 chamada de API por ticker
            except Exception as e:
                import traceback
                print(f'[ERRO] {ticker}: {e}')
                print(traceback.format_exc())
                continue
            for uid, plan in users:
                try:
                    check_ticker(ticker, data, uid, plan)
                except Exception as e:
                    print(f'[ERRO] {ticker} user={uid}: {e}')
        print(f'[MONITOR] Aguardando {CHECK_INTERVAL}s...')
        time.sleep(CHECK_INTERVAL)

# ─── Bot interativo (comandos) ───────────────────────────
bot = telebot.TeleBot(TELEGRAM_TOKEN or 'x', threaded=False)

TEXTO_PLANOS = (
    f'<b>Como funciona</b>\n\n'
    f'🎁 <b>{TRIAL_DIAS} dias grátis</b>, com tudo liberado, a partir do seu primeiro uso\n'
    f'⭐ Depois, <b>R$ {PRECO:.2f}/mês</b> para continuar recebendo os alertas\n\n'
    f'Você pode acompanhar até {LIMITE_ATIVOS} ativos (B3 e EUA).\n'
    f'<i>Os alertas são informativos e não constituem recomendação de investimento.</i>'
)

def teclado_assinar():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f'⭐ Assinar (R$ {PRECO:.2f}/mês)', callback_data='assinar'))
    return kb

def asaas(method, path, **kw):
    r = requests.request(method, f'{ASAAS_URL}{path}',
                         headers={'access_token': ASAAS_KEY}, timeout=15, **kw)
    data = r.json()
    if r.status_code >= 300:
        raise Exception(f'Asaas {r.status_code}: {data}')
    return data

def criar_pix(uid, customer_id):
    venc = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    pay = asaas('POST', '/payments', json={
        'customer': customer_id, 'billingType': 'PIX', 'value': PRECO,
        'dueDate': venc, 'externalReference': str(uid),
        'description': 'Bot Monitor - Assinatura 30 dias'})
    qr = asaas('GET', f'/payments/{pay["id"]}/pixQrCode')
    with db() as cur:
        cur.execute("insert into payments(id, telegram_id, amount) values (%s,%s,%s)",
                    (pay['id'], uid, PRECO))
    return qr['payload']

def iniciar_assinatura(uid, chat_id):
    if not ASAAS_KEY:
        return bot.send_message(chat_id, 'Assinatura indisponível no momento.')
    _, _, customer = ensure_user(uid)
    if not customer:
        return bot.send_message(
            chat_id,
            'Para gerar o Pix, envie seu CPF (só números) assim:\n<code>/cpf 12345678900</code>\n\n'
            'Ele é usado só para emitir a cobrança e não fica salvo no bot.',
            parse_mode='HTML')
    try:
        codigo = criar_pix(uid, customer)
    except Exception as e:
        print(f'[ASAAS] erro ao criar Pix: {e}')
        return bot.send_message(chat_id, 'Não consegui gerar o Pix agora. Tente de novo em instantes.')
    bot.send_message(chat_id, 'Copie o código Pix abaixo e pague no app do seu banco. '
                              'O plano é ativado automaticamente após o pagamento:')
    bot.send_message(chat_id, f'<code>{codigo}</code>', parse_mode='HTML')

@bot.message_handler(commands=['start', 'ajuda', 'help'])
def cmd_start(m):
    ensure_user(m.from_user.id)       # no primeiro uso, já inicia o teste grátis
    bot.send_message(m.chat.id,
        f'👋 Eu monitoro ações e aviso você no Telegram.\n\n'
        f'🎁 Você tem <b>{TRIAL_DIAS} dias grátis</b> para testar tudo. '
        f'Depois, R$ {PRECO:.2f}/mês.\n\n'
        '/add PETR4 — adicionar ativo\n'
        '/remove PETR4 — remover ativo\n'
        '/lista — seus ativos\n'
        '/meuplano — quanto falta do seu acesso\n'
        '/planos — como funciona e assinar\n\n'
        'Aceito ativos da B3 (ex.: PETR4, MGLU3) e dos EUA (ex.: AAPL).\n'
        '<i>Alertas informativos, não são recomendação de investimento.</i>',
        parse_mode='HTML')

@bot.message_handler(commands=['planos'])
def cmd_planos(m):
    bot.send_message(m.chat.id, TEXTO_PLANOS, parse_mode='HTML', reply_markup=teclado_assinar())

@bot.message_handler(commands=['assinar'])
def cmd_assinar(m):
    iniciar_assinatura(m.from_user.id, m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == 'assinar')
def cb_assinar(c):
    bot.answer_callback_query(c.id)
    iniciar_assinatura(c.from_user.id, c.message.chat.id)

@bot.message_handler(commands=['meuplano'])
def cmd_meuplano(m):
    uid = m.from_user.id
    plan, until, _ = ensure_user(uid)
    if acesso_ativo(plan, until):
        with db() as cur:
            cur.execute("select count(*) from payments where telegram_id=%s and status='paid'", (uid,))
            pagou = cur.fetchone()[0] > 0
        sp = pytz.timezone('America/Sao_Paulo')
        dias = max(0, (until - datetime.now(pytz.utc)).days)
        tipo = 'Assinatura ativa' if pagou else 'Teste grátis'
        bot.reply_to(m, f'✅ {tipo} até {until.astimezone(sp):%d/%m/%Y} (faltam ~{dias} dias).')
    else:
        bot.send_message(m.chat.id,
            f'Seu acesso terminou. Para continuar, assine por R$ {PRECO:.2f}/mês:',
            reply_markup=teclado_assinar())

@bot.message_handler(commands=['cpf'])
def cmd_cpf(m):
    uid = m.from_user.id
    p = m.text.split()
    try:
        bot.delete_message(m.chat.id, m.message_id)   # apaga a mensagem com o CPF
    except Exception:
        pass
    if len(p) < 2 or not ASAAS_KEY:
        return bot.send_message(m.chat.id, 'Use: /cpf 12345678900')
    doc = re.sub(r'\D', '', p[1])
    if len(doc) not in (11, 14):
        return bot.send_message(m.chat.id, 'CPF/CNPJ inválido. Envie só os números.')
    ensure_user(uid)
    try:
        cust = asaas('POST', '/customers', json={
            'name': m.from_user.first_name or 'Cliente', 'cpfCnpj': doc,
            'externalReference': str(uid)})
    except Exception as e:
        print(f'[ASAAS] erro ao criar cliente: {e}')
        return bot.send_message(m.chat.id, 'Não consegui validar esse CPF. Confira e tente de novo.')
    with db() as cur:
        cur.execute("update users set asaas_customer=%s where telegram_id=%s", (cust['id'], uid))
    bot.send_message(m.chat.id, '✅ Dados registrados. Toque em /assinar para gerar o Pix.')

@bot.message_handler(commands=['add'])
def cmd_add(m):
    p = m.text.split()
    if len(p) < 2:
        return bot.reply_to(m, 'Use: /add PETR4')
    t = p[1].upper()
    if not (is_b3(t) or re.fullmatch(r'[A-Z]{1,5}', t)):
        return bot.reply_to(m, 'Ticker inválido. Ex.: PETR4, MGLU3, AAPL.')
    uid = m.from_user.id
    plan, until, _ = ensure_user(uid)
    if not acesso_ativo(plan, until):
        return bot.send_message(m.chat.id,
            f'Seu acesso terminou. Para continuar, assine por R$ {PRECO:.2f}/mês:',
            reply_markup=teclado_assinar())
    with db() as cur:
        cur.execute("select count(*) from watchlist where telegram_id=%s", (uid,))
        qtd = cur.fetchone()[0]
        cur.execute("select 1 from watchlist where telegram_id=%s and ticker=%s", (uid, t))
        ja_tem = cur.fetchone() is not None
    if ja_tem:
        return bot.reply_to(m, f'{t} já está na sua lista.')
    if qtd >= LIMITE_ATIVOS:
        return bot.reply_to(m, f'Você atingiu o limite de {LIMITE_ATIVOS} ativos.')
    try:
        fetch_price(t)                      # confirma que o ativo existe
    except Exception:
        return bot.reply_to(m, f'Não encontrei dados para {t}. Confira o código.')
    with db() as cur:
        cur.execute("insert into watchlist(telegram_id, ticker) values (%s,%s) on conflict do nothing",
                    (uid, t))
    bot.reply_to(m, f'✅ {t} adicionado.')

@bot.message_handler(commands=['remove'])
def cmd_remove(m):
    p = m.text.split()
    if len(p) < 2:
        return bot.reply_to(m, 'Use: /remove PETR4')
    with db() as cur:
        cur.execute("delete from watchlist where telegram_id=%s and ticker=%s",
                    (m.from_user.id, p[1].upper()))
    bot.reply_to(m, '🗑 Removido.')

@bot.message_handler(commands=['lista'])
def cmd_lista(m):
    with db() as cur:
        cur.execute("select ticker from watchlist where telegram_id=%s order by ticker", (m.from_user.id,))
        ts = [r[0] for r in cur.fetchall()]
    bot.reply_to(m, ', '.join(ts) if ts else 'Lista vazia. Use /add PETR4')

# ─── Comandos de administrador ───────────────────────────
def eh_admin(m):
    return bool(CHAT_ID) and str(m.from_user.id) == str(CHAT_ID)

@bot.message_handler(commands=['id'])
def cmd_id(m):
    bot.reply_to(m, f'Seu ID: {m.from_user.id}')

@bot.message_handler(commands=['admin'])
def cmd_admin(m):
    if not eh_admin(m):
        return
    with db() as cur:
        cur.execute("""select count(*),
                              count(*) filter (where plan='pro' and plan_until > now())
                       from users""")
        total, ativos_u = cur.fetchone()
        cur.execute("select count(distinct telegram_id) from payments where status='paid'")
        pagantes = cur.fetchone()[0]
        cur.execute("select count(*) from watchlist")
        ativos = cur.fetchone()[0]
        cur.execute("""select coalesce(sum(amount),0) from payments
                       where status='paid' and created_at >= date_trunc('month', now())""")
        mes = cur.fetchone()[0]
    bot.reply_to(m, f'👥 Usuários: {total}\n'
                    f'✅ Com acesso ativo (teste, pagos e você): {ativos_u}\n'
                    f'💳 Já pagaram: {pagantes}\n'
                    f'📈 Ativos monitorados: {ativos}\n'
                    f'💰 Recebido no mês: R$ {float(mes):.2f}')

@bot.message_handler(commands=['darpro'])
def cmd_darpro(m):
    if not eh_admin(m):
        return
    p = m.text.split()
    if len(p) < 2 or not p[1].isdigit():
        return bot.reply_to(m, 'Use: /darpro ID [dias]')
    uid = int(p[1])
    dias = int(p[2]) if len(p) > 2 and p[2].isdigit() else 30
    with db() as cur:
        cur.execute("insert into users(telegram_id) values (%s) on conflict do nothing", (uid,))
        cur.execute("""update users set plan='pro', warned=false,
                       plan_until = greatest(coalesce(plan_until, now()), now()) + %s * interval '1 day'
                       where telegram_id=%s""", (dias, uid))
    send_telegram(f'🎁 Você ganhou {dias} dias de acesso!', uid)
    bot.reply_to(m, '✅ Acesso liberado.')

@bot.message_handler(commands=['avisar'])
def cmd_avisar(m):
    if not eh_admin(m):
        return
    import html
    texto = html.escape(m.text.partition(' ')[2].strip())
    if not texto:
        return bot.reply_to(m, 'Use: /avisar sua mensagem')
    with db() as cur:
        cur.execute("select telegram_id from users")
        ids = [r[0] for r in cur.fetchall()]
    enviados = 0
    for uid in ids:
        if send_telegram(texto, uid):
            enviados += 1
        time.sleep(0.05)
    bot.reply_to(m, f'Enviado para {enviados} de {len(ids)}.')

# ─── Flask (keepalive, status, webhooks) ─────────────────
@app.route('/')
def index():
    return jsonify({'status': 'online', 'modo': 'multiusuario' if HAS_DB else 'legado',
                    'precos': last_known_price})

@app.route('/ping')
def ping():
    return 'ok', 200

@app.route('/status')
def status():
    return jsonify(last_known_price)

@app.post('/tg/<secret>')
def tg_webhook(secret):
    if not TG_SECRET or secret != TG_SECRET:
        return '', 403
    try:
        update = types.Update.de_json(request.stream.read().decode('utf-8'))
        bot.process_new_updates([update])
    except Exception as e:
        print(f'[TG] erro ao processar update: {e}')
    return '', 200

@app.post('/webhook/pix')
def webhook_pix():
    if not WEBHOOK_TOKEN or request.headers.get('asaas-access-token') != WEBHOOK_TOKEN:
        return '', 401
    ev = request.get_json(silent=True) or {}
    if ev.get('event') in ('PAYMENT_RECEIVED', 'PAYMENT_CONFIRMED'):
        pay_id = (ev.get('payment') or {}).get('id')
        try:
            uid = ativar_pro(pay_id) if pay_id else None
            if uid:
                send_telegram('✅ Pagamento confirmado! Seu acesso está ativo por mais 30 dias.', uid)
        except Exception as e:
            print(f'[PIX] erro ao ativar: {e}')
            return '', 500          # Asaas tenta de novo
    return '', 200

# ─── Inicialização ───────────────────────────────────────
_started = False

def setup_telegram_webhook():
    if TELEGRAM_TOKEN and TG_SECRET and PUBLIC_URL:
        try:
            bot.remove_webhook()
            bot.set_webhook(url=f'{PUBLIC_URL.rstrip("/")}/tg/{TG_SECRET}')
            print('[TG] Webhook registrado')
        except Exception as e:
            print(f'[TG] Falha ao registrar webhook: {e}')
    else:
        print('[TG] Webhook NÃO registrado (faltam TG_SECRET ou PUBLIC_URL)')

def start_background():
    global _started
    if _started:
        return
    _started = True
    if HAS_DB:
        try:
            init_db()
            seed_owner()
            setup_telegram_webhook()
        except Exception as e:
            print(f'[INIT] Erro ao preparar banco: {e}')
    threading.Thread(target=monitor_loop, daemon=True).start()

start_background()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

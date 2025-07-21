import os, time, requests, pytz, logging
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv

# ========== ENV SETUP ==========
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
CONTAINER_USERNAME = os.getenv("CONTAINER_USERNAME")

# ========== MONGO ==========
mongo_db = None
def connect_to_mongo(uri, db_name):
    global mongo_db
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    mongo_db = client[db_name]
    logging.info("MongoDB connected")

def get_collection(name):
    if mongo_db is None:
        raise Exception("MongoDB not connected")
    return mongo_db[name]

def send_teams_alert(
    message, webhook_url=None, model_name="MonitoringBot",
    username=None, profile=None, model_title=None, notif_type="info"
):
    logging.info(f"{model_name}: {message}")

    api_url = os.getenv("NOTIFICATION_API_URL", "https://hyperloop.neuralschemait.com/api/hyperloop/v1/notifications")
    token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3NTE2MDg0MTMsInB1cnBvc2UiOiJiYWNrZ3JvdW5kX2pvYiJ9.xo2GOGNoFX2D7dEHB2lpXR3CXw2wvXuL2xFDvarRgpA'

    payload = {
        "title": model_title or model_name,
        "message": message,
        "username": username or CONTAINER_USERNAME,
        "profile": profile or "UNKNOWN_PROFILE",
        "model": model_name,
        "type": notif_type
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3NTE2MDg0MTMsInB1cnBvc2UiOiJiYWNrZ3JvdW5kX2pvYiJ9.xo2GOGNoFX2D7dEHB2lpXR3CXw2wvXuL2xFDvarRgpA"
    }
    print(Printing Header object, headers)
    try:
        response = requests.post(api_url, headers=headers, json=payload)
        print(response.status_code, response.text)
        if response.status_code != 201:
            logging.warning(f"Notification API call failed: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Notification API error: {e}")


def fetch_last_candle(base_url, token, instrument, granularity):
    headers = {"Authorization": f"Bearer {token}"}
    params = {"count": 1, "granularity": granularity, "price": "M"}
    url = f"{base_url}instruments/{instrument}/candles"
    r = requests.get(url, headers=headers, params=params)
    if r.status_code == 200:
        candle = r.json().get('candles', [])[0]
        o = float(candle['mid']['o'])
        c = float(candle['mid']['c'])
        return (c - o) * 10000
    return None

def fetch_candles(base_url, token, instrument, count, granularity):
    headers = {"Authorization": f"Bearer {token}"}
    params = {"count": count, "granularity": granularity, "price": "M"}
    url = f"{base_url}instruments/{instrument}/candles"
    r = requests.get(url, headers=headers, params=params)
    return r.json().get('candles', []) if r.status_code == 200 else []

def calculate_ema(prices, period):
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = (p * k) + (ema * (1 - k))
    return ema

def detect_trend(prices, short_period, long_period):
    buffer = 25
    ema_s = calculate_ema(prices[-(short_period + buffer):], short_period)
    ema_l = calculate_ema(prices[-(long_period + buffer):], long_period)
    if ema_s > ema_l:
        return "UPTREND", ema_s, ema_l
    elif ema_s < ema_l:
        return "DOWNTREND", ema_s, ema_l
    return "SIDEWAYS", ema_s, ema_l

def check_open_trades(base_url, token, account_id):
    url = f"{base_url}accounts/{account_id}/openTrades"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        trades = response.json().get('trades', [])
        if trades:
            trade = trades[0]
            pl = float(trade['unrealizedPL'])
            return True, pl
    return False, 0.0

def check_pending_orders(base_url, token, account_id):
    url = f"{base_url}accounts/{account_id}/pendingOrders"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    return response.json().get('orders', []) if response.status_code == 200 else []

def cancel_order(base_url, token, account_id, order_id):
    url = f"{base_url}accounts/{account_id}/orders/{order_id}/cancel"
    headers = {"Authorization": f"Bearer {token}"}
    requests.put(url, headers=headers)

def place_market_order(base_url, token, account_id, instrument, units, side, take_profit_pips, cfg):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    pricing_url = f"{base_url}accounts/{account_id}/pricing"
    price_response = requests.get(pricing_url, headers=headers, params={"instruments": instrument})
    prices = price_response.json().get('prices', [])[0]
    ask = float(prices['asks'][0]['price'])
    bid = float(prices['bids'][0]['price'])
    # --- Stop Loss Logic ---
    stop_loss_price = None
    stop_loss_pips = cfg.get("STOP_LOSS_PIPS")
    entry_price = ask if side == "BUY" else bid

    if stop_loss_pips:
        if side == "BUY":
            stop_loss_price = round(entry_price - (stop_loss_pips * 0.0001), 5)
        else:
            stop_loss_price = round(entry_price + (stop_loss_pips * 0.0001), 5)

    tp_price = round((ask if side == "BUY" else bid) + (take_profit_pips * 0.0001 if side == "BUY" else -take_profit_pips * 0.0001), 5)
    final_units = units if side == "BUY" else str(-abs(int(units)))

    order_data = {
        "order": {
            "units": final_units,
            "instrument": instrument,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "takeProfitOnFill": {"price": str(tp_price)}
        }
    }

    if stop_loss_price:
        order_data["order"]["stopLossOnFill"] = {"price": str(stop_loss_price)}

    order_url = f"{base_url}accounts/{account_id}/orders"
    r = requests.post(order_url, headers=headers, json=order_data)
    if r.status_code == 201:
        send_teams_alert(f"🚀 {side} order placed for {instrument} at TP {tp_price}", cfg['TEAMS_WEBHOOK_URL'], cfg['model_name'], CONTAINER_USERNAME, cfg['profile'], cfg['model_name'])

def run_model(cfg):
    base_url = "https://api-fxpractice.oanda.com/v3/" if cfg['env'].lower() == "practice" else "https://api-fxtrade.oanda.com/v3/"
    token = cfg['access_token']
    account_id = cfg['account_primary']
    instrument = cfg['instrument'].replace("-", "_")
    short = cfg['EMA_SHORT_PERIOD']
    long = cfg['EMA_LONG_PERIOD']
    pip_threshold = cfg['PIP_DIFF_THRESHOLD']
    take_profit = cfg['TAKE_PROFIT_PIPS']
    units = str(cfg['TRADE_UNITS'])

    candles = fetch_candles(base_url, token, instrument, long + 50, "M15")
    close_prices = [float(c['mid']['c']) for c in candles if c['complete']]
    if len(close_prices) < long + 25:
        send_teams_alert("⚠️ Not enough data to determine trend", cfg['TEAMS_WEBHOOK_URL'], cfg['model_name'], CONTAINER_USERNAME, cfg['profile'], cfg['model_name'])
        return

    trend, ema_s, ema_l = detect_trend(close_prices, short, long)
    send_teams_alert(f"📊 {trend} | EMA{short}: {ema_s:.5f} | EMA{long}: {ema_l:.5f}", cfg['TEAMS_WEBHOOK_URL'], cfg['model_name'], CONTAINER_USERNAME, cfg['profile'], cfg['model_name'])

    pip_m1 = fetch_last_candle(base_url, token, instrument, "M1")
    pip_m5 = fetch_last_candle(base_url, token, instrument, "M5")
    pip_m15 = fetch_last_candle(base_url, token, instrument, "M15")

    if None in [pip_m1, pip_m5, pip_m15]:
        send_teams_alert("⚠️ Candle fetch failed", cfg['TEAMS_WEBHOOK_URL'], cfg['model_name'], CONTAINER_USERNAME, cfg['profile'], cfg['model_name'])
        return

    passed = (
        (trend == "UPTREND" and pip_m1 > 0 and pip_m5 > 0 and pip_m15 > 0) or
        (trend == "DOWNTREND" and pip_m1 < 0 and pip_m5 < 0 and pip_m15 < 0)
    )
    report = f"M1: {pip_m1:.2f}, M5: {pip_m5:.2f}, M15: {pip_m15:.2f}"

    has_trade, _ = check_open_trades(base_url, token, account_id)
    if has_trade:
        send_teams_alert("⏳ Open trade exists ➔ Skipping", cfg['TEAMS_WEBHOOK_URL'], cfg['model_name'], CONTAINER_USERNAME, cfg['profile'], cfg['model_name'])
        return

    pending_orders = check_pending_orders(base_url, token, account_id)
    if pending_orders:
        order = pending_orders[0]
        order_price = float(order['price'])
        pricing = requests.get(f"{base_url}accounts/{account_id}/pricing", headers={"Authorization": f"Bearer {token}"}, params={"instruments": instrument}).json()['prices'][0]
        mid = (float(pricing['bids'][0]['price']) + float(pricing['asks'][0]['price'])) / 2
        pip_diff = abs(mid - order_price) * 10000

        if pip_diff > pip_threshold:
            cancel_order(base_url, token, account_id, order['id'])
            if passed:
                place_market_order(base_url, token, account_id, instrument, units, "BUY" if trend == "UPTREND" else "SELL", take_profit, cfg)
        else:
            send_teams_alert("⏳ Pending order within pip threshold ➔ No action", cfg['TEAMS_WEBHOOK_URL'], cfg['model_name'], CONTAINER_USERNAME, cfg['profile'], cfg['model_name'])
    elif passed:
        place_market_order(base_url, token, account_id, instrument, units, "BUY" if trend == "UPTREND" else "SELL", take_profit, cfg)
    else:
        send_teams_alert(f"❌ Conditions Failed ➔ {trend}\n{report}", cfg['TEAMS_WEBHOOK_URL'], cfg['model_name'], CONTAINER_USERNAME, cfg['profile'], cfg['model_name'])

def run_forever():
    logging.info("📡 Trend Monitor Started")
    connect_to_mongo(os.getenv("MONGO_URI"), os.getenv("MONGO_DB"))
    profiles = [p['name'] for p in get_collection("oanda_profiles").find({"username": CONTAINER_USERNAME})]
    configs = list(get_collection("model_configurations").find({"profile": {"$in": profiles}, "status": "A"}))
    accounts = list(get_collection("accounts").find({"status": "A", "profile": {"$in": profiles}}))
    account_map = {a['accountid']: a['accountkey'] for a in accounts}

    while True:
        for cfg in configs:
            cfg['access_token'] = account_map.get(cfg['account_primary'])
            if not cfg['access_token']:
                logging.warning(f"Skipping model {cfg['model_name']}: no access token")
                continue
            run_model(cfg)
            time.sleep(1)
        logging.info("⏳ Sleeping 5 min...")
        time.sleep(300)

if __name__ == "__main__":
    logging.info("✅ Monitor script starting...")
    run_forever()

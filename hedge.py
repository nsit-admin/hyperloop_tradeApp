import os, time, threading, requests, pytz, sys, logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_URL = os.getenv("API_BASE_URL", "https://api-fxpractice.oanda.com/v3/")
CONTAINER_USERNAME = os.getenv("CONTAINER_USERNAME") 

# ----------------------------
# MongoDB
# ----------------------------

mongo_db = None

def connect_to_mongo(uri, db_name):
    global mongo_db
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        mongo_db = client[db_name]
        logging.info(f"MongoDB connected to {db_name}")
    except Exception as e:
        logging.error(f"MongoDB connection error: {e}")
        mongo_db = None

def get_collection(name):
    if mongo_db is None:
        raise Exception("MongoDB is not connected.")
    return mongo_db[name]

# ----------------------------
# OANDA Helpers
# ----------------------------

def fetch_open_trade(account_id, token):
    url = f"{BASE_URL}accounts/{account_id}/openTrades"
    headers = {"Authorization": f"Bearer {token}"}
    logging.info(f"Fetching open trades for account {account_id}- {url} - {token}")
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        trades = response.json().get('trades', [])
        logging.info(f"Fetched {len(trades)} open trades")
        return trades[0] if trades else None
    logging.error(f"Failed to fetch trades: {response.status_code} {response.text}")
    return None

def fetch_last_candle(token, granularity, instrument):
    url = f"{BASE_URL}instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"count": 1, "granularity": granularity, "price": "M"}
    logging.info(f"Fetching {granularity} candle for {instrument}")
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        candle = response.json().get('candles', [])[0]
        open_price = float(candle['mid']['o'])
        close_price = float(candle['mid']['c'])
        logging.info(f"Fetched candle: open={open_price}, close={close_price}")
        return (close_price - open_price) * 10000
    logging.error(f"Failed to fetch candles: {response.status_code} {response.text}")
    return None

def validate_candles(token, expected_direction, instrument, webhook, model_name):
    pip_m1 = fetch_last_candle(token, "M1", instrument)
    pip_m5 = fetch_last_candle(token, "M5", instrument)
    pip_m15 = fetch_last_candle(token, "M15", instrument)

    if None in [pip_m1, pip_m5, pip_m15]:
        send_teams_alert("⚠️ Candle fetch error. Skipping validation.", webhook, model_name, CONTAINER_USERNAME, model_name)
        return False

    msg = f"🔎 Candle Validation:\nM1: {pip_m1:.2f}, M5: {pip_m5:.2f}, M15: {pip_m15:.2f}"
    logging.info(f"{model_name}: {msg}")
    send_teams_alert(msg, webhook, model_name, CONTAINER_USERNAME, model_name)

    if expected_direction == "BUY":
        return pip_m1 > 0 and pip_m5 > 0
    elif expected_direction == "SELL":
        return pip_m1 < 0 and pip_m5 < 0
    return False

def place_hedge_order(original_side, original_units, instrument, hedge_id, hedge_token, multiplier, webhook, model_name, config):
    existing = fetch_open_trade(hedge_id, hedge_token)
    if existing and existing['instrument'] == instrument:
        msg = "⚠️ Hedge NOT placed: trade already open on same instrument."
        logging.warning(f"{model_name}: {msg}")
        send_teams_alert(msg, webhook, model_name, CONTAINER_USERNAME, model_name)
        return

    hedge_side = "SELL" if original_side == "BUY" else "BUY"
    hedge_units = int(abs(original_units) * multiplier)
    hedge_units = -hedge_units if hedge_side == "SELL" else hedge_units

    stop_loss_pips = config.get("STOP_LOSS_PIPS")
    if stop_loss_pips is None:
        logging.info(f"{model_name}: No STOP_LOSS defined. Proceeding without stop loss.")

    url = f"{BASE_URL}accounts/{hedge_id}/orders"
    headers = {
        "Authorization": f"Bearer {hedge_token}",
        "Content-Type": "application/json"
    }

    pricing_url = f"{BASE_URL}accounts/{hedge_id}/pricing"
    pricing_res = requests.get(pricing_url, headers=headers, params={"instruments": instrument})
    prices = pricing_res.json().get("prices", [{}])[0]
    bid = float(prices.get("bids", [{}])[0].get("price", 0))
    ask = float(prices.get("asks", [{}])[0].get("price", 0))
    entry_price = ask if hedge_side == "BUY" else bid

    stop_loss_price = None
    if stop_loss_pips:
        if hedge_side == "BUY":
            stop_loss_price = round(entry_price - (stop_loss_pips * 0.0001), 5)
        else:
            stop_loss_price = round(entry_price + (stop_loss_pips * 0.0001), 5)

    body = {
        "order": {
            "units": str(hedge_units),
            "instrument": instrument,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
        }
    }

    if stop_loss_price:
        body["order"]["stopLossOnFill"] = {"price": str(stop_loss_price)}

    r = requests.post(url, headers=headers, json=body)
    if r.status_code == 201:
        msg = f"🔄 Hedge placed: {hedge_side} {abs(hedge_units)} units with SL {stop_loss_price or 'None'}"
        logging.info(f"{model_name}: {msg}")
        send_teams_alert(msg, webhook, model_name, CONTAINER_USERNAME, model_name)
    else:
        msg = f"❌ Hedge order failed: {r.status_code} {r.text}"
        logging.error(f"{model_name}: {msg}")
        send_teams_alert(msg, webhook, model_name, CONTAINER_USERNAME, model_name)

def close_all_trades(account_id, token, instrument):
    trade = fetch_open_trade(account_id, token)
    if trade and trade['instrument'] == instrument:
        url = f"{BASE_URL}accounts/{account_id}/trades/{trade['id']}/close"
        headers = {"Authorization": f"Bearer {token}"}
        requests.put(url, headers=headers)

# ----------------------------
# Alert Helper
# ----------------------------
def send_teams_alert(
    message, webhook_url=None, model_name="HedgeBot",
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
        if response.status_code != 201:
            logging.warning(f"Notification API call failed: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Notification API error: {e}")


# ----------------------------
# Core Logic
# ----------------------------

def run_monitor_with_latest_config(profile, model_name, side):
    config = get_collection("model_configurations").find_one({
        "profile": profile,
        "model_name": model_name,
        "status": "A"
    })
    if config:
        config = enrich_with_tokens(config)
        run_hedging_monitor(config, side)
    else:
        logging.warning(f"No active config found for {profile} - {model_name}")

def run_hedging_monitor(config, side):
    model_name = config.get("model_name") + f" ({side})"
    instrument = config.get("instrument", "EUR-USD").replace("-", "_")
    webhook = config.get("TEAMS_WEBHOOK_URL", "")
    loss_trigger = config.get("LOSS_TRIGGER_PIPS", -20)
    profit_trigger = config.get("COMBINED_PROFIT_CLOSE_PIPS", 10)
    multiplier = config.get("HEDGE_UNITS_MULTIPLIER", 2)

    if side == "primary":
        acc_id = config.get("account_primary")
        token = config.get("token_primary")
        hedge_id = config.get("account_secondary")
        hedge_token = config.get("token_secondary")
    else:
        acc_id = config.get("account_secondary")
        token = config.get("token_secondary")
        hedge_id = config.get("account_primary")
        hedge_token = config.get("token_primary")

    trade_primary = fetch_open_trade(acc_id, token)
    trade_hedge = fetch_open_trade(hedge_id, hedge_token)

    if trade_primary:
        units = int(trade_primary['currentUnits'])
        unrealized = float(trade_primary['unrealizedPL'])
        side = "BUY" if units > 0 else "SELL"
        pips_loss = (unrealized / abs(units)) * 10000

        if not trade_hedge:
            if pips_loss <= loss_trigger:
                if validate_candles(token, "BUY" if side == "SELL" else "SELL", instrument, webhook, model_name):
                    place_hedge_order(side, units, instrument, hedge_id, hedge_token, multiplier, webhook, model_name, config)
                    send_teams_alert(f"🛡️ Hedge triggered at {pips_loss:.2f} pips loss.", webhook, model_name, CONTAINER_USERNAME, model_name)
        else:
            pl_hedge = float(trade_hedge['unrealizedPL'])
            combined = unrealized + pl_hedge

            send_teams_alert(f"📈 Combined P/L: {combined:.2f}", webhook, model_name, CONTAINER_USERNAME, model_name)
            if combined >= profit_trigger:
                close_all_trades(acc_id, token, instrument)
                close_all_trades(hedge_id, hedge_token, instrument)
                send_teams_alert(f"🏁 Profit target hit ({combined:.2f} pips). Trades closed.", webhook, model_name, CONTAINER_USERNAME, model_name)

# ----------------------------
# Scheduler Setup
# ----------------------------

def enrich_with_tokens(config):
    accounts = get_collection("accounts").find({"status": "A", "profile": config["profile"]})
    account_map = {acc["accountid"]: acc["accountkey"] for acc in accounts}
    config['token_primary'] = account_map.get(config.get("account_primary"), "")
    config['token_secondary'] = account_map.get(config.get("account_secondary"), "")
    return config

def schedule_monitors():
    connect_to_mongo(os.getenv("MONGO_URI"), os.getenv("MONGO_DB"))
    profiles = [p['name'] for p in get_collection("oanda_profiles").find({"username": CONTAINER_USERNAME})]
    logging.info(f"Loaded profiles for CONTAINER_USERNAME={CONTAINER_USERNAME}: {profiles}")

    configs = list(get_collection("model_configurations").find({"profile": {"$in": profiles}, "status": "A"}))
    logging.info(f"Loaded {len(configs)} model configurations")

    scheduler = BackgroundScheduler(timezone=pytz.utc)
    for cfg in configs:
        base_name = f"{CONTAINER_USERNAME}_{cfg['profile']}_{cfg['model_name']}"
        if cfg.get("cron_schedule_primary"):
            logging.info(f"Scheduling primary job for {base_name}")
            scheduler.add_job(lambda p=cfg['profile'], m=cfg['model_name']: threading.Thread(
                target=run_monitor_with_latest_config, args=(p, m, "primary")
            ).start(),
            CronTrigger.from_crontab(cfg["cron_schedule_primary"]),
            name=f"{base_name}_primary")

        if cfg.get("cron_schedule_secondary"):
            logging.info(f"Scheduling secondary job for {base_name}")
            scheduler.add_job(lambda p=cfg['profile'], m=cfg['model_name']: threading.Thread(
                target=run_monitor_with_latest_config, args=(p, m, "secondary")
            ).start(),
            CronTrigger.from_crontab(cfg["cron_schedule_secondary"]),
            name=f"{base_name}_secondary")

    scheduler.start()
    logging.info("Hedge scheduler started.")
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        logging.info("Scheduler shutdown initiated.")
        scheduler.shutdown()

# ----------------------------
# Entry Point
# ----------------------------

if __name__ == "__main__":
    logging.info("✅ Hedge script starting...")
    schedule_monitors()

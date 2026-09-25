import os
import time
import requests
import threading
from flask import Flask

# =====================================================================
# FLASK HEALTH CHECK SERVER (Keeps Render Free Instance Active)
# =====================================================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Markov 2.0 Engine Active", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# =====================================================================
# CONFIGURATION & SAFETY GATES
# =====================================================================
PAPER_TRADING = True  # Set to False when ready for live SOL execution
SOL_TRADE_SIZE = 0.03 # Paper trade size in SOL
MIN_LIQUIDITY_USD = 3000.0
SIGNAL_THRESHOLD = 0.25 # Stride Trigger (S > +0.25)
FAMILIARS_API_KEY = os.environ.get("FAMILIARS_API_KEY", "")

# =====================================================================
# MARKOV 2.0 DIFFERENTIAL ENGINE
# =====================================================================
def get_dex_pair_data(token_mint):
    """Fetch live market data, liquidity, and volume metrics from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
        res = requests.get(url, timeout=10)
        
        if res.status_code != 200:
            return None
        
        data = res.json()
        pairs = data.get('pairs', [])
        if not pairs:
            return None
        
        pair = pairs[0]
        liquidity = pair.get('liquidity', {}).get('usd', 0)
        
        # Rule 1 Check: Minimum Liquidity Floor
        if liquidity < MIN_LIQUIDITY_USD:
            return None
            
        return pair
    except Exception:
        return None

def compute_markov_differential(pair):
    """
    Evaluates token velocity, momentum delta, and structural stability.
    Returns: float S (Stride Signal) or None if Anti-Top-Blast rules trigger.
    """
    price_change = pair.get('priceChange', {})
    m5 = price_change.get('m5', 0) or 0
    h1 = price_change.get('h1', 0) or 0
    h6 = price_change.get('h6', 0) or 0

    # Rule 2: Anti-Top-Blast Refusal (Prevents chasing overextended pumps)
    if h1 > 50.0 or m5 > 30.0:
        symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
        print(f"[REFUSAL] ${symbol} overextended (+{h1}% 1h / +{m5}% 5m). Refusing entry.")
        return None

    # Vector calculation
    v_m5 = m5 / 5.0
    v_h1 = h1 / 60.0
    delta_v = v_m5 - v_h1

    v_h6 = h6 / 360.0
    stability = 1.0 if abs(v_h1 - v_h6) < 0.5 else 0.5

    S = (delta_v * 0.6) + (v_m5 * 0.4) * stability
    return round(S, 2)

# =====================================================================
# FAMILIARS.FAMILY SOCIAL CALLOUT POSTING
# =====================================================================
def post_to_familiars(symbol, mint, signal_value):
    """Publishes agent callouts directly to familiars.family public feed."""
    if not FAMILIARS_API_KEY:
        print("[FAMILIARS] No API key configured. Skipping feed post.")
        return

    url = "https://familiars.family/api/posts"
    headers = {
        "Authorization": f"Bearer {FAMILIARS_API_KEY}",
        "Content-Type": "application/json"
    }
    content = f"🎯 [PAPER TRADE] Markov 2.0 triggered on ${symbol} (Signal S: +{signal_value}). Mint: {mint}"
    payload = {"content": content}

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code in [200, 201]:
            print(f"[FAMILIARS] Successfully posted ${symbol} trade callout!")
        else:
            print(f"[FAMILIARS ERROR] Status {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[FAMILIARS EXCEPTION] {e}")

# =====================================================================
# MAIN CONTINUOUS SCANNING LOOP
# =====================================================================
def run_trading_loop():
    print(f"--- Markov 2.0 Engine Starting (Paper Mode: {PAPER_TRADING}) ---")
    
    while True:
        try:
            # Fetch top trending tokens on Solana
            boost_url = "https://api.dexscreener.com/token-boosts/top/v1"
            res = requests.get(boost_url, timeout=10)
            
            if res.status_code == 200:
                data = res.json()
                tokens = data[:15] if isinstance(data, list) else []
            else:
                tokens = []

            for item in tokens:
                mint = item.get('tokenAddress')
                if not mint:
                    continue

                pair = get_dex_pair_data(mint)
                if not pair:
                    continue

                symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
                S = compute_markov_differential(pair)

                if S is not None:
                    if S >= SIGNAL_THRESHOLD:
                        print(f"[SIGNAL TRIGGERED] ${symbol} | Stride Signal S = +{S}")
                        
                        if PAPER_TRADING:
                            print(f"[PAPER TRADE] Simulated BUY: {SOL_TRADE_SIZE} SOL into ${symbol} ({mint})")
                            post_to_familiars(symbol, mint, S)
                            
                            # Pause 5 minutes to allow current signal play to evolve
                            print("[WAIT] Position opened. Sleeping 5 minutes to avoid duplicate entries...")
                            time.sleep(300)
                            break
                        else:
                            # Live execution hook goes here
                            pass

            time.sleep(30) # Scan loop interval
            
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            time.sleep(10)

# =====================================================================
# APPLICATION ENTRYPOINT
# =====================================================================
if __name__ == "__main__":
    # Start Flask server thread for Render health checks
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Run the continuous Markov scanning loop
    run_trading_loop()

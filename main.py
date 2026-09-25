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
# CONFIGURATION & STATE TRACKING
# =====================================================================
PAPER_TRADING = True  # Set to False when ready for live SOL execution
SOL_TRADE_SIZE = 0.03 # Paper trade size in SOL (Use 0.1 SOL for live)
MIN_LIQUIDITY_USD = 3000.0

# Market Cap Filters
MIN_MARKET_CAP = 10000.0   # $10k min market cap
MAX_MARKET_CAP = 250000.0  # $250k max market cap

# Volume & Trend Safety Filters
MIN_5M_VOLUME = 500.0      # Minimum $500 in 5m volume
MAX_MACRO_DRAWDOWN = -25.0 # Reject tokens down more than -25% on 6h or 24h

SIGNAL_THRESHOLD = 0.25 # Stride Trigger (S > +0.25)

TAKE_PROFIT_PCT = 0.35  # +35% TP
STOP_LOSS_PCT = 0.12    # -12% SL
BLACKLIST_COOLDOWN_SEC = 7200  # 2-hour cooldown after Stop Loss

MAX_CONCURRENT_POSITIONS = 1   # Single position limit for testing

FAMILIARS_API_KEY = os.environ.get("FAMILIARS_API_KEY", "")
BASE_FAMILIARS_URL = "https://familiars.family"

# State Tracking
active_positions = {}      # { mint: { "symbol": str, "entry_price": float } }
stopped_out_tokens = {}    # { mint: timestamp_when_stopped_out }

# Performance Tracker
trade_stats = {
    "total_closed": 0,
    "wins": 0,
    "losses": 0,
    "net_sol_pnl": 0.0
}

# =====================================================================
# ORGANIC CANDIDATE SCRAPER (MULTI-QUERY DEX + PROFILES + JUPITER)
# =====================================================================
def fetch_organic_candidates():
    """
    Fetches active Solana token mints across multiple DexScreener search terms
    and fallback endpoints to ensure the scanner always has candidates to evaluate.
    """
    candidate_mints = []

    # Source A: Multi-query search across active Solana DEX pairs
    search_terms = ["pump", "sol", "raydium", "moon"]
    for term in search_terms:
        try:
            url = f"https://api.dexscreener.com/latest/dex/search?q={term}"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                pairs = res.json().get('pairs', [])
                if pairs:
                    for p in pairs:
                        if p.get('chainId') == 'solana':
                            base_mint = p.get('baseToken', {}).get('address')
                            if base_mint and base_mint not in candidate_mints:
                                candidate_mints.append(base_mint)
        except Exception:
            pass

    # Source B: Token Profiles (Recent Updates)
    try:
        profile_url = "https://api.dexscreener.com/token-profiles/recent-updates/v1"
        res = requests.get(profile_url, timeout=5)
        if res.status_code == 200:
            profiles = res.json()
            if isinstance(profiles, list):
                for item in profiles:
                    if item.get('chainId') == 'solana':
                        addr = item.get('tokenAddress')
                        if addr and addr not in candidate_mints:
                            candidate_mints.append(addr)
    except Exception:
        pass

    # Source C: Fallback to Jupiter's Public Token API if candidates list is thin
    if len(candidate_mints) < 10:
        try:
            jup_url = "https://tokens.jup.ag/tokens?tags=verified"
            res = requests.get(jup_url, timeout=5)
            if res.status_code == 200:
                tokens = res.json()
                for t in tokens[:30]:
                    addr = t.get('address')
                    if addr and addr not in candidate_mints:
                        candidate_mints.append(addr)
        except Exception:
            pass

    print(f"📡 [SCRAPER] Fetched {len(candidate_mints)} candidate mints for evaluation.")
    return candidate_mints[:30]

# =====================================================================
# SAFETY & ORGANIC MOMENTUM FILTERS
# =====================================================================
def check_rugcheck_safety(token_mint):
    """Queries RugCheck API for mint/freeze risks and high holder concentration."""
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_mint}/report/summary"
        res = requests.get(url, timeout=5)
        
        if res.status_code == 200:
            data = res.json()
            risk_level = data.get('riskLevel', '')
            
            if risk_level in ['Danger', 'High']:
                return False

            risks = data.get('risks', [])
            for risk in risks:
                risk_name = risk.get('name', '')
                if risk_name in ['Single holder ownership', 'High holder concentration', 'Mint Authority Enabled', 'Freeze Authority Enabled']:
                    return False
        return True
    except Exception:
        return True

def get_dex_pair_data(token_mint):
    """Enforces Organic Volume Ratio, Market Cap ($10k-$250k), and Anti-Falling-Knife filters."""
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
        
        # Rule 1: Liquidity Floor Check
        if liquidity < MIN_LIQUIDITY_USD:
            return None

        # Rule 2: Market Cap Gate ($10k min, $250k max)
        mc = pair.get('marketCap') or pair.get('fdv', 0)
        if mc < MIN_MARKET_CAP or mc > MAX_MARKET_CAP:
            return None

        # Rule 3: 5-Minute Volume Floor ($500+)
        v5m = pair.get('volume', {}).get('m5', 0) or 0
        if v5m < MIN_5M_VOLUME:
            return None

        # Rule 4: Macro Downtrend Gate (Reject Falling Knives)
        price_change = pair.get('priceChange', {})
        h6 = price_change.get('h6', 0) or 0
        h24 = price_change.get('h24', 0) or 0

        if h6 < MAX_MACRO_DRAWDOWN or h24 < MAX_MACRO_DRAWDOWN:
            return None

        # Rule 5: Organic Buyer Velocity Gate (Buys must lead Sells)
        txns = pair.get('txns', {}).get('m5', {})
        buys = txns.get('buys', 0)
        sells = txns.get('sells', 0)

        if (buys + sells) < 12 or buys < (sells * 1.2):
            return None
            
        return pair
    except Exception:
        return None

def get_raw_price(token_mint):
    """Fetches purely the USD price for position management."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            pairs = res.json().get('pairs', [])
            if pairs:
                return float(pairs[0].get('priceUsd', 0) or 0)
        return None
    except Exception:
        return None

# =====================================================================
# MARKOV 2.0 DIFFERENTIAL ENGINE
# =====================================================================
def compute_markov_differential(pair):
    """Evaluates velocity acceleration and volume weighting."""
    price_change = pair.get('priceChange', {})
    m5 = price_change.get('m5', 0) or 0
    h1 = price_change.get('h1', 0) or 0
    h6 = price_change.get('h6', 0) or 0

    if h1 > 50.0 or m5 > 30.0:  # Anti-Top-Blast Refusal
        return None

    v_m5 = m5 / 5.0
    v_h1 = h1 / 60.0
    delta_v = v_m5 - v_h1

    v_h6 = h6 / 360.0
    stability = 1.0 if abs(v_h1 - v_h6) < 0.5 else 0.5

    S = (delta_v * 0.6) + (v_m5 * 0.4) * stability

    # Volume-Weighting Factor
    v5m = pair.get('volume', {}).get('m5', 0) or 0
    volume_weight = min(max(v5m / 1000.0, 0.5), 1.5)

    return round(S * volume_weight, 2)

def post_to_familiars(content):
    """Publishes agent callouts directly to familiars.family public feed."""
    if not FAMILIARS_API_KEY:
        return

    url = f"{BASE_FAMILIARS_URL}/api/posts"
    headers = {
        "Authorization": f"Bearer {FAMILIARS_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"content": content}

    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception:
        pass

# =====================================================================
# POSITION MANAGEMENT & METRICS TRACKER
# =====================================================================
def check_active_positions():
    """Monitors open positions for Take Profit (+35%) or Stop Loss (-12%)."""
    global active_positions, stopped_out_tokens, trade_stats
    
    if not active_positions:
        return

    mints_to_close = []

    for mint, info in active_positions.items():
        symbol = info['symbol']
        entry_price = info['entry_price']
        
        current_price = get_raw_price(mint)
        
        if not current_price or entry_price == 0:
            print(f"⏳ [MONITOR] Watching ${symbol} | CA: {mint} | Awaiting price feed...")
            continue

        pnl_pct = (current_price - entry_price) / entry_price
        print(f"📈 [POSITION CHECK] ${symbol} | CA: {mint} | Current: ${current_price:.8f} | Entry: ${entry_price:.8f} | PnL: {pnl_pct*100:+.2f}%")

        # 1. Take Profit (+35%)
        if pnl_pct >= TAKE_PROFIT_PCT:
            sol_gained = SOL_TRADE_SIZE * pnl_pct
            trade_stats["total_closed"] += 1
            trade_stats["wins"] += 1
            trade_stats["net_sol_pnl"] += sol_gained
            win_rate = (trade_stats["wins"] / trade_stats["total_closed"]) * 100

            print(f"\n🎯 [TAKE PROFIT HIT] ${symbol} | CA: {mint} | Gain: +{pnl_pct*100:.2f}% (+{sol_gained:.4f} SOL)")
            print(f"📊 [STATS UPDATE] Closed: {trade_stats['total_closed']} | Win Rate: {win_rate:.1f}% | Net SOL: {trade_stats['net_sol_pnl']:+.4f} SOL")
            
            post_to_familiars(f"🎉 [PAPER TP] Closed ${symbol} ({mint}) at +{pnl_pct*100:.2f}%! Win Rate: {win_rate:.1f}%")
            mints_to_close.append((mint, "TP"))

        # 2. Stop Loss (-12%)
        elif pnl_pct <= -STOP_LOSS_PCT:
            sol_lost = SOL_TRADE_SIZE * pnl_pct
            trade_stats["total_closed"] += 1
            trade_stats["losses"] += 1
            trade_stats["net_sol_pnl"] += sol_lost
            win_rate = (trade_stats["wins"] / trade_stats["total_closed"]) * 100

            print(f"\n🛑 [STOP LOSS HIT] ${symbol} | CA: {mint} | Loss: {pnl_pct*100:.2f}% ({sol_lost:.4f} SOL)")
            print(f"📊 [STATS UPDATE] Closed: {trade_stats['total_closed']} | Win Rate: {win_rate:.1f}% | Net SOL: {trade_stats['net_sol_pnl']:+.4f} SOL")
            
            post_to_familiars(f"🛑 [PAPER SL] Closed ${symbol} ({mint}) at {pnl_pct*100:.2f}%. Win Rate: {win_rate:.1f}%")
            stopped_out_tokens[mint] = time.time()
            mints_to_close.append((mint, "SL"))

    for mint, exit_type in mints_to_close:
        del active_positions[mint]

# =====================================================================
# MAIN CONTINUOUS SCANNING LOOP
# =====================================================================
def run_trading_loop():
    print(f"--- Markov 2.0 Engine Active (Organic Mode | Paper: {PAPER_TRADING}) ---")
    
    while True:
        try:
            check_active_positions()

            if len(active_positions) >= MAX_CONCURRENT_POSITIONS:
                time.sleep(30)
                continue

            print(f"🔎 [SCANNING] Screening organic Solana volume... (Active Positions: {len(active_positions)})")
            mints = fetch_organic_candidates()

            for mint in mints:
                if len(active_positions) >= MAX_CONCURRENT_POSITIONS:
                    break

                if mint in active_positions:
                    continue

                if mint in stopped_out_tokens:
                    if time.time() - stopped_out_tokens[mint] < BLACKLIST_COOLDOWN_SEC:
                        continue
                    else:
                        del stopped_out_tokens[mint]

                pair = get_dex_pair_data(mint)
                if not pair:
                    continue

                symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
                current_price = float(pair.get('priceUsd', 0) or 0)
                mc = pair.get('marketCap') or pair.get('fdv', 0)

                if not check_rugcheck_safety(mint):
                    print(f"⚠️ [REJECTED] ${symbol} ({mint}) | Failed RugCheck")
                    continue

                S = compute_markov_differential(pair)
                print(f"📊 [EVALUATING] ${symbol} ({mint}) | MC: ${mc:,.0f} | Stride S: {S}")

                if S is not None and S >= SIGNAL_THRESHOLD:
                    print(f"\n[ORGANIC SIGNAL TRIGGERED] ${symbol} | CA: {mint} | MC: ${mc:,.0f} | Stride Signal S = +{S}")
                    
                    if PAPER_TRADING:
                        active_positions[mint] = {
                            "symbol": symbol,
                            "entry_price": current_price
                        }
                        print(f"[PAPER TRADE] Simulated BUY: {SOL_TRADE_SIZE} SOL into ${symbol} | CA: {mint} @ ${current_price:.8f}")
                        post_to_familiars(f"🎯 [PAPER BUY] Markov 2.0 entered ${symbol} (CA: {mint}) | MC: ${mc:,.0f} | Signal S: +{S}")
                        break

            time.sleep(30)
            
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            time.sleep(10)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_trading_loop()

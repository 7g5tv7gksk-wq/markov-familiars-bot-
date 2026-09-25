import os
import time
import requests
import threading
from flask import Flask

# =====================================================================
# CONFIGURATION & STATE TRACKING
# =====================================================================
PAPER_TRADING = True  # Set to False when ready for live SOL execution
SOL_TRADE_SIZE = 0.1  # Paper trade size in SOL
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
# MULTI-SOURCE ORGANIC CANDIDATE SCRAPER (DEX + PROFILES + JUPITER)
# =====================================================================
def fetch_organic_candidates():
    """
    Fetches active Solana token mints across DexScreener search terms,
    recent profile updates, and Jupiter endpoints.
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

    # Source C: Fallback to Jupiter API if candidate list is thin
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
# GMGN & RUGCHECK SECURITY GUARDS
# =====================================================================
def check_gmgn_security(token_mint):
    """Checks GMGN endpoint for hidden risks (Bundlers > 10%, high rug ratio)."""
    try:
        url = f"https://gmgn.ai/defi/quotation/v1/tokens/sol/{token_mint}"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200:
            data = res.json().get('data', {}).get('token', {})
            bundler_pct = float(data.get('bundler_pct', 0) or 0)
            rug_ratio = float(data.get('rug_ratio', 0) or 0)
            
            if bundler_pct > 10.0:
                print(f"⚠️ [GMGN REJECT] {token_mint} | Bundler Cluster High: {bundler_pct:.1f}%")
                return False
                
            if rug_ratio > 0.30:
                print(f"⚠️ [GMGN REJECT] {token_mint} | High Rug Risk Score: {rug_ratio:.2f}")
                return False
                
            return True
    except Exception:
        return True

def check_rugcheck_safety(token_mint):
    """Queries RugCheck API for mint/freeze risks and holder concentration."""
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

# =====================================================================
# DEX PAIR DATA & MARKOV DIFFERENTIAL
# =====================================================================
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
        
        if liquidity < MIN_LIQUIDITY_USD:
            return None

        mc = pair.get('marketCap') or pair.get('fdv', 0)
        if mc < MIN_MARKET_CAP or mc > MAX_MARKET_CAP:
            return None

        v5m = pair.get('volume', {}).get('m5', 0) or 0
        if v5m < MIN_5M_VOLUME:
            return None

        price_change = pair.get('priceChange', {})
        h6 = price_change.get('h6', 0) or 0
        h24 = price_change.get('h24', 0) or 0
        if h6 < MAX_MACRO_DRAWDOWN or h24 < MAX_MACRO_DRAWDOWN:
            return None

        txns = pair.get('txns', {}).get('m5', {})
        buys = txns.get('buys', 0)
        sells = txns.get('sells', 0)
        if (buys + sells) < 12 or buys < (sells * 1.2):
            return None
            
        return pair
    except Exception:
        return None

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
    v5m = pair.get('volume', {}).get('m5', 0) or 0
    volume_weight = min(max(v5m / 1000.0, 0.5), 1.5)

    return round(S * volume_weight, 2)

# =====================================================================
# JUPITER REAL-TIME PRICE FEED (V2) & 1-SECOND MONITOR THREAD
# =====================================================================
def get_realtime_price(token_mint):
    """Fetches real-time execution price directly from Jupiter API v2."""
    try:
        url = f"https://api.jup.ag/price/v2?ids={token_mint}"
        res = requests.get(url, timeout=2)
        if res.status_code == 200:
            data = res.json().get('data', {}).get(token_mint, {})
            price = data.get('price')
            if price:
                return float(price)
    except Exception:
        pass
    
    # Fallback to DexScreener
    try:
        res = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}", timeout=3)
        if res.status_code == 200:
            pairs = res.json().get('pairs', [])
            if pairs:
                return float(pairs[0].get('priceUsd', 0) or 0)
    except Exception:
        pass
    return None

def run_position_monitor():
    """Dedicated 1-second background thread for instant TP/SL execution."""
    global active_positions, stopped_out_tokens, trade_stats
    print("⚡ [THREAD START] Jupiter Real-Time Position Monitor Active (1s Interval)...")
    
    while True:
        try:
            if active_positions:
                mints_to_close = []
                
                for mint, info in list(active_positions.items()):
                    symbol = info['symbol']
                    entry_price = info['entry_price']
                    
                    current_price = get_realtime_price(mint)
                    if not current_price or entry_price == 0:
                        continue

                    pnl_pct = (current_price - entry_price) / entry_price

                    # 1. TAKE PROFIT (+35%)
                    if pnl_pct >= TAKE_PROFIT_PCT:
                        sol_gained = SOL_TRADE_SIZE * pnl_pct
                        trade_stats["total_closed"] += 1
                        trade_stats["wins"] += 1
                        trade_stats["net_sol_pnl"] += sol_gained
                        win_rate = (trade_stats["wins"] / trade_stats["total_closed"]) * 100

                        print(f"\n🎯 [TAKE PROFIT HIT] ${symbol} | Gain: +{pnl_pct*100:.2f}% (+{sol_gained:.4f} SOL)")
                        print(f"📊 [STATS] Closed: {trade_stats['total_closed']} | WR: {win_rate:.1f}% | Net PnL: {trade_stats['net_sol_pnl']:+.4f} SOL")
                        mints_to_close.append(mint)

                    # 2. STOP LOSS (-12%)
                    elif pnl_pct <= -STOP_LOSS_PCT:
                        sol_lost = SOL_TRADE_SIZE * pnl_pct
                        trade_stats["total_closed"] += 1
                        trade_stats["losses"] += 1
                        trade_stats["net_sol_pnl"] += sol_lost
                        win_rate = (trade_stats["wins"] / trade_stats["total_closed"]) * 100

                        print(f"\n🛑 [STOP LOSS HIT] ${symbol} | Loss: {pnl_pct*100:.2f}% ({sol_lost:.4f} SOL)")
                        print(f"📊 [STATS] Closed: {trade_stats['total_closed']} | WR: {win_rate:.1f}% | Net PnL: {trade_stats['net_sol_pnl']:+.4f} SOL")
                        stopped_out_tokens[mint] = time.time()
                        mints_to_close.append(mint)

                for mint in mints_to_close:
                    if mint in active_positions:
                        del active_positions[mint]

            time.sleep(1)
        except Exception as e:
            print(f"[MONITOR ERROR] {e}")
            time.sleep(1)

# =====================================================================
# MAIN CONTINUOUS SCANNING LOOP
# =====================================================================
def run_trading_loop():
    print(f"--- Markov 2.0 Engine Active (Organic Mode | Paper: {PAPER_TRADING}) ---")
    
    while True:
        try:
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

                # Security Checks: RugCheck + GMGN Bundlers
                if not check_rugcheck_safety(mint):
                    print(f"⚠️ [REJECTED] ${symbol} ({mint}) | Failed RugCheck")
                    continue

                if not check_gmgn_security(mint):
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
                        break

            time.sleep(30)
            
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            time.sleep(10)

# =====================================================================
# FLASK SERVER & MAIN EXECUTION
# =====================================================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Markov 2.0 Engine Active", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    monitor_thread = threading.Thread(target=run_position_monitor, daemon=True)
    monitor_thread.start()

    run_trading_loop()

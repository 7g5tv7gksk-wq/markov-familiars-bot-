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
# MULTI-SOURCE ORGANIC CANDIDATE SCRAPER (PACED TO PREVENT 429)
# =====================================================================
def fetch_organic_candidates():
    """
    Fetches active Solana token mints across DexScreener search terms,
    recent profile updates, and Jupiter endpoints with built-in pacing.
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
            elif res.status_code == 429:
                print("⚠️ [SCRAPER RATE LIMIT] Throttled on search term. Backing off...")
                time.sleep(2)
        except Exception:
            pass
        time.sleep(0.3)  # Pacing to avoid hitting 429 rate limit

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
# SECURITY GUARDS (GMGN & RUGCHECK)
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
                print(f"⚠️ [GMGN REJECT] {token_mint[:6]}... | Bundler Cluster High: {bundler_pct:.1f}%")
                return False
                
            if rug_ratio > 0.30:
                print(f"⚠️ [GMGN REJECT] {token_mint[:6]}... | High Rug Risk Score: {rug_ratio:.2f}")
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
# BATCH DEX PAIR FETCHING & MARKOV DIFFERENTIAL
# =====================================================================
def get_batch_dex_pairs(token_mints):
    """
    Fetches up to 30 token pair profiles in 1 single HTTP request
    to completely prevent DexScreener rate-limiting.
    """
    if not token_mints:
        return {}
    
    mint_csv = ",".join(token_mints[:30])
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_csv}"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 429:
            print("⚠️ [DEX BATCH RATE LIMIT] Throttled by DexScreener. Pausing 5s...")
            time.sleep(5)
            return {}
            
        if res.status_code != 200:
            return {}

        data = res.json()
        pairs = data.get('pairs', [])
        
        # Group highest liquidity pair per base token mint
        mint_pair_map = {}
        if pairs:
            for pair in pairs:
                if pair.get('chainId') == 'solana':
                    base_mint = pair.get('baseToken', {}).get('address')
                    if base_mint and base_mint not in mint_pair_map:
                        mint_pair_map[base_mint] = pair
                        
        return mint_pair_map
    except Exception:
        return {}

def evaluate_pair_safety(pair):
    """Enforces Organic Volume Ratio, Market Cap ($10k-$250k), and Anti-Falling-Knife filters."""
    if not pair:
        return False

    liquidity = float(pair.get('liquidity', {}).get('usd', 0) or 0)
    mc = float(pair.get('marketCap') or pair.get('fdv', 0) or 0)

    if liquidity < MIN_LIQUIDITY_USD or mc < MIN_MARKET_CAP or mc > MAX_MARKET_CAP:
        return False

    v5m = float(pair.get('volume', {}).get('m5', 0) or 0)
    if v5m < MIN_5M_VOLUME:
        return False

    price_change = pair.get('priceChange', {})
    h6 = float(price_change.get('h6', 0) or 0)
    h24 = float(price_change.get('h24', 0) or 0)
    if h6 < MAX_MACRO_DRAWDOWN or h24 < MAX_MACRO_DRAWDOWN:
        return False

    txns = pair.get('txns', {}).get('m5', {})
    buys = txns.get('buys', 0)
    sells = txns.get('sells', 0)
    if (buys + sells) < 12 or buys < (sells * 1.2):
        return False

    return True

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
# JUPITER REAL-TIME PRICE FEED & 1-SECOND MONITOR THREAD
# =====================================================================
def get_realtime_price(token_mint):
    """Fetches real-time price directly from Jupiter API v2."""
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
                        post_to_familiars(f"🎉 [PAPER TP] Closed ${symbol} ({mint[:6]}...) at +{pnl_pct*100:.2f}%!")
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
                        post_to_familiars(f"🛑 [PAPER SL] Closed ${symbol} ({mint[:6]}...) at {pnl_pct*100:.2f}%.")
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
                time.sleep(15)
                continue

            print(f"\n🔎 [SCANNING] Screening organic Solana volume... (Active Positions: {len(active_positions)})")
            mints = fetch_organic_candidates()

            if not mints:
                print("⚠️ [SCRAPER] No candidates returned this cycle. Retrying in 10s...")
                time.sleep(10)
                continue

            # Clean up active/blacklisted mints from batch lookup list
            eval_mints = [
                m for m in mints 
                if m not in active_positions and 
                (m not in stopped_out_tokens or time.time() - stopped_out_tokens[m] >= BLACKLIST_COOLDOWN_SEC)
            ]

            print(f"⚙️ [EVALUATING] Batch fetching {len(eval_mints)} candidates from DexScreener...")
            pair_map = get_batch_dex_pairs(eval_mints)

            for mint in eval_mints:
                if len(active_positions) >= MAX_CONCURRENT_POSITIONS:
                    break

                pair = pair_map.get(mint)
                if not pair or not evaluate_pair_safety(pair):
                    continue

                symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
                current_price = float(pair.get('priceUsd', 0) or 0)
                mc = pair.get('marketCap') or pair.get('fdv', 0)

                # Security Checks: RugCheck + GMGN Bundlers
                if not check_rugcheck_safety(mint):
                    print(f"⚠️ [REJECTED] ${symbol} ({mint[:6]}...) | Failed RugCheck")
                    continue

                if not check_gmgn_security(mint):
                    continue

                S = compute_markov_differential(pair)
                print(f"📊 [PASSED FILTERS] ${symbol} ({mint[:6]}...) | MC: ${mc:,.0f} | Stride S: {S}")

                if S is not None and S >= SIGNAL_THRESHOLD:
                    print(f"\n🚀 [SIGNAL TRIGGERED] ${symbol} | CA: {mint} | MC: ${mc:,.0f} | Stride S = +{S}")
                    
                    if PAPER_TRADING:
                        active_positions[mint] = {
                            "symbol": symbol,
                            "entry_price": current_price
                        }
                        print(f"🟢 [PAPER ENTRY] Simulated BUY: {SOL_TRADE_SIZE} SOL into ${symbol} @ ${current_price:.8f}")
                        break

            time.sleep(15)
            
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
l
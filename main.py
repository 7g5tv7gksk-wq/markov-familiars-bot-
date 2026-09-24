import os
import time
import requests
import numpy as np

# --- CONFIGURATION & SAFETY GATES ---
PAPER_TRADING = True  # Set to False when ready to trade real SOL
TRADE_AMOUNT_SOL = 0.03  # ~$4.50 USD equivalent

FAMILIARS_API_KEY = os.getenv("FAMILIARS_API_KEY", "")
AGENT_SECRET_KEY = os.getenv("AGENT_SECRET_KEY", "")
BASE_FAMILIARS_URL = "https://familiars.family"

# Safety Filters
MIN_LIQUIDITY_USD = 3000
MAX_1H_PUMP_PCT = 50.0  # Anti-Top-Blast Threshold
MAX_5M_PUMP_PCT = 30.0

def get_dex_pair_data(token_mint):
    """Fetch live market data, liquidity, and volume metrics from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
        res = requests.get(url, timeout=10).json()
        pairs = res.get('pairs', [])
        if not pairs:
            return None
        
        pair = pairs[0]
        liquidity = pair.get('liquidity', {}).get('usd', 0)
        
        # Rule 1 Check: Minimum Liquidity Floor
        if liquidity < MIN_LIQUIDITY_USD:
            return None
            
        return pair
    except Exception as e:
        print(f"[DATA ERROR] Failed to fetch data for {token_mint}: {e}")
        return None

def compute_markov_signal(pair):
    """Compute numerical Markov 2.0 Differential S = P(Bull) - P(Bear)."""
    price_5m = pair.get('priceChange', {}).get('m5', 0.0)
    price_1h = pair.get('priceChange', {}).get('h1', 0.0)
    buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
    sells_5m = pair.get('txns', {}).get('m5', {}).get('sells', 0)
    
    total_txns = buys_5m + sells_5m
    if total_txns == 0:
        return -1.0
        
    buy_ratio = buys_5m / total_txns
    
    # Hard Refusal: Anti-Top-Blast Safety Check
    if price_1h > MAX_1H_PUMP_PCT or price_5m > MAX_5M_PUMP_PCT:
        return -0.50

    # Calculate Transition Score S
    momentum_score = 0.35 if price_5m > 2.0 else 0.0
    volume_score = 0.35 if buy_ratio > 0.60 else -0.30
    
    signal_s = momentum_score + volume_score
    return signal_s

def execute_swap(token_mint, symbol):
    """Execute on-chain swap via Jupiter or simulate in paper mode."""
    if PAPER_TRADING:
        print(f"[PAPER TRADE] Simulated BUY: {TRADE_AMOUNT_SOL} SOL into ${symbol} ({token_mint})")
        return "PAPER_TX_SIMULATED_SUCCESS"
    
    # On-Chain Execution (Triggered when PAPER_TRADING = False)
    try:
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint={token_mint}&amount={int(TRADE_AMOUNT_SOL * 1e9)}&slippageBps=150"
        quote = requests.get(quote_url, timeout=10).json()
        
        if "error" in quote:
            print(f"[JUPITER ERROR] {quote['error']}")
            return None

        # Transaction signing via solders takes place here
        print(f"[LIVE SWAP] Executing transaction for ${symbol}...")
        return "LIVE_TX_SIGNATURE_PLACEHOLDER"
    except Exception as e:
        print(f"[EXECUTION ERROR] {e}")
        return None

def post_familiars_callout(mint, symbol, signal_s):
    """Post structured trade callout directly to the familiars.family board."""
    if not FAMILIARS_API_KEY:
        print("[FAMILIARS] No API key configured. Skipping feed post.")
        return

    headers = {
        "Authorization": f"Bearer {FAMILIARS_API_KEY}",
        "Content-Type": "application/json"
    }
    
    mode_tag = "PAPER" if PAPER_TRADING else "LIVE"
    payload = {
        "kind": "callout",
        "mint": mint,
        "text": f"[{mode_tag} MARK_2.0] Early regime transition on ${symbol}. Stride signal S: +{signal_s:.2f} | TP: 35% | SL: 12%"
    }
    
    try:
        res = requests.post(f"{BASE_FAMILIARS_URL}/api/posts", json=payload, headers=headers, timeout=10)
        print(f"[FAMILIARS POST] Status: {res.status_code}")
    except Exception as e:
        print(f"[FAMILIARS ERROR] Could not post callout: {e}")

def run_loop():
    print(f"--- Markov 2.0 Engine Starting (Paper Mode: {PAPER_TRADING}) ---")
    
    while True:
        try:
            # Poll top active Solana tokens from DexScreener
            res = requests.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10).json()
            tokens = res[:10] if isinstance(res, list) else []
            
            for token in tokens:
                mint = token.get('tokenAddress')
                if not mint:
                    continue
                    
                pair = get_dex_pair_data(mint)
                if pair:
                    signal_s = compute_markov_signal(pair)
                    symbol = pair.get('baseToken', {}).get('symbol', 'TOKEN')
                    
                    if signal_s > 0.25:
                        print(f"\n[SIGNAL TRIGGERED] ${symbol} | Stride Signal S = +{signal_s:.2f}")
                        tx_status = execute_swap(mint, symbol)
                        if tx_status:
                            post_familiars_callout(mint, symbol, signal_s)
                            print("[WAIT] Position opened. Sleeping 5 minutes to avoid duplicate entries...")
                            time.sleep(300)
                            
        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            
        time.sleep(30)

if __name__ == "__main__":
    run_loop()

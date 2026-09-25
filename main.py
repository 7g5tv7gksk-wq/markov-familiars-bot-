import os
import time
import logging
import threading
import requests
from flask import Flask

# Configure structured logging output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# Configuration Constants
LOOP_INTERVAL_SECONDS = 15
DEXSCREENER_BATCH_ENDPOINT = "https://api.dexscreener.com/latest/dex/tokens/{mints}"

# =====================================================================
# 1. LIGHTWEIGHT FLASK HEALTH CHECK (Satisfies Render & Uptime Pings)
# =====================================================================
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return "Markov 2.0 Engine Active", 200

def run_flask_server():
    port = int(os.environ.get("PORT", 10000))
    # Suppress default Flask WSGI development server logs to keep console clean
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=port)

# =====================================================================
# 2. CANDIDATE SCRAPER (Jupiter V2 API)
# =====================================================================
def fetch_jupiter_candidates():
    """
    Polls Jupiter Tokens API V2 endpoints for fresh/trending Solana mints.
    Returns a deduplicated list of up to 30 candidate mint address strings.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # Backup Jupiter endpoints to ensure candidate flow if one indexer delays
    urls = [
        "https://tokens.jup.ag/tokens?tags=community",
        "https://api.jup.ag/tokens/v2/recent"
    ]

    mints = []

    for url in urls:
        try:
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                
                # Format A: Plain list of token objects
                if isinstance(data, list):
                    for item in data[:40]:
                        if isinstance(item, dict):
                            addr = item.get("address") or item.get("mint")
                            if addr:
                                mints.append(addr)
                        elif isinstance(item, str):
                            mints.append(item)
                
                # Format B: Dictionary wrapper containing token arrays
                elif isinstance(data, dict):
                    token_list = data.get("tokens", []) or data.get("mints", []) or data.get("data", [])
                    for item in token_list[:40]:
                        if isinstance(item, dict):
                            addr = item.get("address") or item.get("mint")
                            if addr:
                                mints.append(addr)
                        elif isinstance(item, str):
                            mints.append(item)

            if mints:
                break  # Exit loop as soon as candidates are captured
        except Exception as err:
            logging.error(f"❌ [SCRAPER ERROR] {err}")

    # Deduplicate while preserving order and constrain batch size to 30
    unique_mints = list(dict.fromkeys(mints))[:30]
    logging.info(f"📡 [SCRAPER] Fetched {len(unique_mints)} candidate mints from Jupiter.")
    return unique_mints

# =====================================================================
# 3. BATCH DEXSCREENER EVALUATOR (Single Request)
# =====================================================================
def evaluate_batch_dexscreener(mints):
    """
    Evaluates up to 30 candidate mints using ONE single batch request to DexScreener.
    """
    if not mints:
        return []

    mint_query_str = ",".join(mints)
    url = DEXSCREENER_BATCH_ENDPOINT.format(mints=mint_query_str)
    logging.info(f"⚙️ [EVALUATING] Batch fetching {len(mints)} candidates from DexScreener...")

    try:
        response = requests.get(url, timeout=8)

        if response.status_code == 200:
            data = response.json()
            pairs = data.get("pairs", [])
            logging.info(f"✅ [EVALUATED] Successfully retrieved {len(pairs)} active trading pairs.")
            return pairs

        elif response.status_code == 429:
            logging.warning("⚠️ [DEX BATCH 429] Throttled by DexScreener. Backing off 15s...")
            time.sleep(15)
            return []
        else:
            logging.warning(f"⚠️ [EVALUATOR] DexScreener status code: {response.status_code}")
            return []

    except Exception as err:
        logging.error(f"❌ [EVALUATION ERROR] {err}")
        return []

# =====================================================================
# 4. PROCESSOR & MAIN SCANNER LOOP
# =====================================================================
def process_pairs(pairs):
    """
    Evaluates market parameters (liquidity, volume, price action).
    """
    if not pairs:
        return

    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue

        base_token = pair.get("baseToken", {})
        symbol = base_token.get("symbol", "UNKNOWN")
        mint = base_token.get("address", "")
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume_m5 = float(pair.get("volume", {}).get("m5", 0) or 0)
        mc = float(pair.get("marketCap") or pair.get("fdv", 0) or 0)

        # Basic activity filter logging
        if liquidity >= 3000 and volume_m5 >= 500:
            logging.info(f"📊 [PASSED FILTERS] ${symbol} ({mint[:6]}...) | MC: ${mc:,.0f} | 5m Vol: ${volume_m5:,.0f}")

def run_scanner_loop():
    logging.info("🚀 [BOT STARTED] Solana volume scanner active. Target interval: 15s")
    
    while True:
        cycle_start = time.time()
        logging.info("🔎 [SCANNING] Screening organic Solana volume... (Active Positions: 0)")

        # Step 1: Scrape candidates via Jupiter
        candidate_mints = fetch_jupiter_candidates()

        # Step 2: Single-batch evaluation via DexScreener
        if candidate_mints:
            pairs = evaluate_batch_dexscreener(candidate_mints)
            process_pairs(pairs)
        else:
            logging.warning("⚠️ [SCRAPER] No candidates returned this cycle. Retrying in 15s...")

        # Step 3: Maintain exact 15-second loop timing
        elapsed = time.time() - cycle_start
        sleep_time = max(0.0, LOOP_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)

# =====================================================================
# ENTRY POINT
# =====================================================================
if __name__ == "__main__":
    # Start Flask HTTP web server on background thread so Render detects port binding
    server_thread = threading.Thread(target=run_flask_server, daemon=True)
    server_thread.start()

    # Execute trading engine loop
    run_scanner_loop()

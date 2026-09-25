import time
import logging
import requests

# Configure logging output
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# Configuration Constants
LOOP_INTERVAL_SECONDS = 15
JUPITER_RECENT_ENDPOINT = "https://api.jup.ag/tokens/v2/recent"
DEXSCREENER_BATCH_ENDPOINT = "https://api.dexscreener.com/latest/dex/tokens/{mints}"


def fetch_jupiter_candidates():
    """
    Polls Jupiter Tokens API V2 for recent token mints.
    Returns a list of mint address strings.
    """
    headers = {
        "User-Agent": "SolanaBot/1.0"
        # Optional: Add your free API key from portal.jup.ag if you have one:
        # "x-api-key": "YOUR_JUPITER_API_KEY"
    }

    try:
        response = requests.get(JUPITER_RECENT_ENDPOINT, headers=headers, timeout=8)
        
        if response.status_code == 200:
            data = response.json()
            
            # Extract mint addresses depending on JSON return structure
            mints = []
            if isinstance(data, list):
                for item in data:
                    # Capture address from object representation
                    addr = item.get("address") or item.get("mint")
                    if addr:
                        mints.append(addr)
            elif isinstance(data, dict):
                mints = data.get("mints", [])

            # Deduplicate and restrict to first 30 candidate mints for single-batch evaluation
            unique_mints = list(dict.fromkeys(mints))[:30]
            logging.info(f"📡 [SCRAPER] Fetched {len(unique_mints)} candidate mints from Jupiter.")
            return unique_mints

        elif response.status_code == 429:
            logging.warning("⚠️ [JUPITER 429] Rate limited on Jupiter Tokens API. Skipping cycle...")
            return []
        else:
            logging.warning(f"⚠️ [SCRAPER] Jupiter returned unexpected status: {response.status_code}")
            return []

    except Exception as err:
        logging.error(f"❌ [SCRAPER ERROR] Exception fetching candidates: {err}")
        return []


def evaluate_batch_dexscreener(mints):
    """
    Sends up to 30 candidate mints to DexScreener in ONE single HTTP request.
    Returns list of active pair dictionaries.
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
            logging.warning("⚠️ [DEX BATCH RATE LIMIT] Throttled by DexScreener. Pacing delay needed...")
            return []
        else:
            logging.warning(f"⚠️ [EVALUATOR] DexScreener returned status: {response.status_code}")
            return []

    except Exception as err:
        logging.error(f"❌ [EVALUATION ERROR] Exception batch querying DexScreener: {err}")
        return []


def process_pairs(pairs):
    """
    Custom liquidity, volume, and contract filter engine logic.
    """
    if not pairs:
        return

    for pair in pairs:
        token_name = pair.get("baseToken", {}).get("name", "Unknown")
        liquidity = pair.get("liquidity", {}).get("usd", 0)
        volume_h1 = pair.get("volume", {}).get("h1", 0)
        
        # Example condition checks
        if liquidity > 5000 and volume_h1 > 10000:
            logging.info(f"🎯 [MATCH FOUND] Token: {token_name} | Liquidity: ${liquidity:,.2f} | 1h Vol: ${volume_h1:,.2f}")


def run_scanner_loop():
    """
    Main orchestration execution loop running every 15 seconds.
    """
    logging.info("🚀 [BOT STARTED] Solana volume scanner active. Target interval: 15s")
    
    while True:
        cycle_start = time.time()
        logging.info("🔎 [SCANNING] Screening organic Solana volume... (Active Positions: 0)")

        # Step 1: Candidate Sourcing via Jupiter (1 request)
        candidate_mints = fetch_jupiter_candidates()

        # Step 2: Evaluation via DexScreener Batch Call (1 request)
        if candidate_mints:
            pairs = evaluate_batch_dexscreener(candidate_mints)
            process_pairs(pairs)
        else:
            logging.warning("⚠️ [SCRAPER] No candidates returned this cycle. Pacing loop...")

        # Step 3: Calculate loop execution duration and maintain exactly 15s cycles
        elapsed = time.time() - cycle_start
        sleep_time = max(0.0, LOOP_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    run_scanner_loop()

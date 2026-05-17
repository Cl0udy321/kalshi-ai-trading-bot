import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List

from src.clients.kalshi_client import KalshiClient
from src.utils.database import DatabaseManager, Position

# --- CONFIGURATION ---
MAX_HOURS_TO_EXPIRY = 72   # Expanded to 72 hours to catch more active near-term markets
MIN_VOLUME = 100           # Lowered for the Kalshi Demo environment
MIN_PRICE = 0.25           # Highly uncertain / volatile range
MAX_PRICE = 0.75           
MAX_RISK_PER_TRADE = 20.00  # Cap risk to a flat $20.00 per trade

# Skip lists to avoid illiquid or broken experimental markets
SKIP_PREFIXES = ["KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXUFC", "KXPGA", "KXATP"]

def should_skip(ticker: str) -> bool:
    upper = ticker.upper()
    return any(upper.startswith(p) for p in SKIP_PREFIXES)

async def run_risky_strategy():
    print("🔥 INITIALIZING RISKY FLIPPER STRATEGY...")
    client = KalshiClient()
    db_manager = DatabaseManager(db_path="trading_system.db")
    await db_manager.initialize()

    bal = await client.get_balance()
    cash = bal.get("balance", 0) / 100.0
    print(f"💰 Available Cash: ${cash:.2f}")

    print("📡 Fetching active markets from Kalshi API...")
    all_markets = []
    seen_tickers = set()
    cursor = None
    
    # Page through active events with nested markets enabled
    for page in range(15):
        params = {
            "status": "open", 
            "limit": 100,
            "with_nested_markets": "true" # CRITICAL: Tells Kalshi to return market lists
        }
        if cursor: 
            params["cursor"] = cursor
            
        try:
            resp = await client._make_authenticated_request("GET", "/trade-api/v2/events", params=params)
            events = resp.get("events", [])
            if not events: 
                break
                
            for event in events:
                for m in event.get("markets", []):
                    ticker = m.get("ticker", "")
                    if ticker and ticker not in seen_tickers:
                        seen_tickers.add(ticker)
                        all_markets.append(m)
                        
            cursor = resp.get("cursor")
            if not cursor: 
                break
            await asyncio.sleep(0.1)
        except Exception as e:
            print(f"⚠️ Error fetching page {page}: {e}")
            break

    print(f"🔍 Filtering {len(all_markets)} markets for high volatility & near expiry...")
    now = datetime.now(timezone.utc)
    candidates = []

    for m in all_markets:
        ticker = m.get("ticker", "")
        if should_skip(ticker): 
            continue

        volume = float(m.get("volume_fp", 0) or m.get("volume", 0) or 0)
        if volume < MIN_VOLUME: 
            continue

        yes_last = float(m.get("last_price_dollars", 0) or m.get("last_price", 0) or 0)
        if yes_last > 1.0: 
            yes_last /= 100.0

        # Look for 50/50 volatile coin tosses
        if not (MIN_PRICE <= yes_last <= MAX_PRICE): 
            continue

        close_time = m.get("close_time", "")
        if not close_time: 
            continue
        
        try:
            expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            hours_to_expiry = (expiry - now).total_seconds() / 3600
        except Exception: 
            continue

        if 0 < hours_to_expiry <= MAX_HOURS_TO_EXPIRY:
            m["_hours_to_expiry"] = hours_to_expiry
            m["_yes_last"] = yes_last
            candidates.append(m)

    print(f"📊 Found {len(candidates)} highly volatile candidates. Checking orderbooks...")
    
    # Deduplicate against existing positions/resting orders to avoid double queueing
    try:
        positions_resp = await client.get_positions()
        pos_tickers = {p["ticker"] for p in positions_resp.get("market_positions", [])}
    except Exception:
        pos_tickers = set()

    placed = 0
    for m in candidates[:30]:  # Cap to top 30 to stay within rate-limits
        ticker = m["ticker"]
        if ticker in pos_tickers:
            continue

        try:
            ob_resp = await client.get_orderbook(ticker, depth=5)
            ob = ob_resp.get("orderbook_fp", ob_resp.get("orderbook", {}))
        except Exception: 
            continue

        yes_bids = ob.get("yes_dollars", ob.get("yes", []))
        if not yes_bids: 
            continue

        try:
            highest_yes_bid = max(float(b[0]) for b in yes_bids)
            if highest_yes_bid > 1.0: 
                highest_yes_bid /= 100.0
        except (ValueError, TypeError): 
            continue

        # Place our bid slightly above the highest existing bid
        our_price = highest_yes_bid + 0.01 
        if our_price > MAX_PRICE: 
            continue

        # Risk management position sizing
        investment = min(MAX_RISK_PER_TRADE, cash)
        contracts = int(investment // our_price)
        if contracts < 1: 
            continue

        print(f"  🔥 [DRY] YES x{contracts} @ ${our_price:.2f} | expiry: {m['_hours_to_expiry']:.1f}h | {ticker}")
        
        pos = Position(
            market_id=ticker,
            side="YES",
            entry_price=our_price,
            quantity=contracts,
            timestamp=datetime.now(),
            rationale=f"Risky Volatility Play: Expiring in {m['_hours_to_expiry']:.1f} hours with high near-term price movement.",
            confidence=0.50,  # 50/50 coin flip momentum play
            live=False,
            strategy="risky_flipper"
        )
        
        await db_manager.add_position(pos)
        placed += 1
        cash -= (contracts * our_price)

    print(f"\n✅ Queued {placed} risky trades to the GUI database!")

if __name__ == "__main__":
    asyncio.run(run_risky_strategy())
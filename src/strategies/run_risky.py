import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import asyncio
import logging
import time
import uuid
from src.utils.expiration_filter import filter_markets_by_expiration
from datetime import datetime, timezone
from typing import Dict, List

from src.clients.kalshi_client import KalshiClient
from src.utils.database import DatabaseManager, Position
from src.data.sentiment_analyzer import SentimentAnalyzer

# --- SPECULATIVE RISK PARAMETERS ---
MAX_RISK_PER_TRADE = 20.00  # Cap risk to a flat $20.00 per trade to protect bankroll
SKIP_PREFIXES = ["KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXUFC", "KXPGA", "KXATP"]

def should_skip(ticker: str) -> bool:
    upper = ticker.upper()
    return any(upper.startswith(p) for p in SKIP_PREFIXES)

async def run_risky_strategy():
    print("🔥 INITIALIZING DEMO-OPTIMIZED RISKY FLIPPER...")
    client = KalshiClient()
    db_manager = DatabaseManager(db_path="trading_system.db")
    sentiment_analyzer = SentimentAnalyzer()
    await db_manager.initialize()

    bal = await client.get_balance()
    cash = bal.get("balance", 0) / 100.0
    from src.config.settings import settings
    if not getattr(settings.trading, 'live_trading_enabled', False) and cash < 10.0:
        cash = 1000.0
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
            "with_nested_markets": "true"
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
                    # RESTOREDPermissive check: we rely on events endpoint 'open' status and future expiry
                    if ticker and ticker not in seen_tickers:
                        seen_tickers.add(ticker)
                        all_markets.append(m)
                        
            cursor = resp.get("cursor")
            if not cursor: 
                break
            await asyncio.sleep(0.05)
        except Exception as e:
            print(f"⚠️ Error fetching page {page}: {e}")
            break

    # =========================================================================
    # WIDENED EXPIRATION FILTER: 0 to 36500 days
    # =========================================================================
    print(f"📥 Raw markets fetched: {len(all_markets)}")
    all_markets = filter_markets_by_expiration(all_markets, min_days=0, max_days=36500)
    print(f"⏱️ Filtered down to {len(all_markets)} markets expiring in the next 36500 days.")
    # =========================================================================

    print(f"🔍 Analyzing {len(all_markets)} open markets for speculative risk profiles...")
    now = datetime.now(timezone.utc)
    scored_candidates = []

    for m in all_markets:
        ticker = m.get("ticker", "")
        if should_skip(ticker): 
            continue

        # Extract last traded price safely
        yes_last = float(m.get("last_price_dollars", 0) or m.get("last_price", 0) or 0)
        if yes_last > 1.0: 
            yes_last /= 100.0

        # Default untraded markets to 0.50 (perfect uncertainty)
        if yes_last == 0.0:
            yes_last = 0.50

        # Calculate closeness to $0.50 (Coin-Flip Factor)
        coin_flip_score = 1.0 - (2.0 * abs(yes_last - 0.50))

        close_time = m.get("close_time", "")
        hours_to_expiry = 2400.0  # Default fallback
        if close_time:
            try:
                expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                # Skip any markets that have already closed
                if expiry <= now:
                    continue
                hours_to_expiry = (expiry - now).total_seconds() / 3600
            except Exception: 
                pass

        # Calculate Time-Decay Factor (Shorter expiry = more speculative)
        time_score = 100.0 / (hours_to_expiry + 1.0)

        # Dynamic Speculative Score
        speculative_score = (coin_flip_score * 0.6) + (min(1.0, time_score / 10.0) * 0.4)

        scored_candidates.append({
            "market": m,
            "ticker": ticker,
            "yes_last": yes_last,
            "hours_to_expiry": hours_to_expiry,
            "speculative_score": speculative_score
        })

    # Sort candidates: Highest Speculative Score first
    scored_candidates.sort(key=lambda x: -x["speculative_score"])

    # Gather existing open positions to prevent duplication
    try:
        positions_resp = await client.get_positions()
        pos_tickers = {p["ticker"] for p in positions_resp.get("market_positions", [])}
    except Exception:
        pos_tickers = set()

    print(f"📊 Speculative Grading Complete. Filtering duplicate selections...")
    
    # Pre-filter valid candidates to avoid hitting the API for already held positions
    valid_candidates = []
    for item in scored_candidates:
        if item["ticker"] not in pos_tickers:
            valid_candidates.append(item)
        if len(valid_candidates) >= 20:
            break
            
    print(f"📰 Fetching latest news for sentiment analysis on top {len(valid_candidates)} candidates...")
    await sentiment_analyzer._news.fetch_all()
    
    for item in valid_candidates:
        market_title = item["market"].get("title", "")
        
        # Get market sentiment
        relevant = sentiment_analyzer._news.get_relevant_articles(market_title, max_articles=5)
        if relevant:
            articles = [p[0] for p in relevant]
            scores = [p[1] for p in relevant]
            sentiment = await sentiment_analyzer.analyze_market_sentiment(market_title, articles, scores)
            ws = sentiment.relevance_weighted_score
            
            # Apply Option A: Boost or penalize speculative score based on sentiment
            # ws ranges from -1.0 to 1.0, we scale it to +/- 0.3 impact
            sentiment_impact = ws * 0.3
            item["speculative_score"] += sentiment_impact
            item["sentiment_summary"] = sentiment_analyzer._format_summary(market_title, sentiment)
        else:
            item["sentiment_summary"] = "No relevant news found. Sentiment neutral."
            
    # Re-sort after sentiment adjustment
    valid_candidates.sort(key=lambda x: -x["speculative_score"])
    
    placed = 0
    for item in valid_candidates:
        if placed >= 12:
            break
            
        ticker = item["ticker"]
        m = item["market"]
        
        our_price = item["yes_last"]
        
        # Keep inside bounds
        if our_price > 0.85: our_price = 0.85
        if our_price < 0.15: our_price = 0.15

        # Risk Management Sizing
        investment = min(MAX_RISK_PER_TRADE, cash)
        contracts = int(investment // our_price)
        if contracts < 1: 
            continue

        print(f"  🔥 [DRY] YES x{contracts} @ ${our_price:.2f} | score: {item['speculative_score']:.2f} | expiry: {item['hours_to_expiry']:.1f}h | {ticker}")
        
        rationale = (
            f"High Variance Speculative Play. Priced at ${our_price:.2f} "
            f"with {item['hours_to_expiry']:.1f} hours remaining. "
            f"Speculative Grader Score: {item['speculative_score']:.2f}.\n\n"
            f"{item['sentiment_summary']}"
        )
        
        pos = Position(
            market_id=ticker,
            side="YES",
            entry_price=our_price,
            quantity=contracts,
            timestamp=datetime.now(),
            rationale=rationale,
            confidence=item['speculative_score'],  
            live=False,
            strategy="risky_flipper"
        )
        
        await db_manager.add_position(pos)
        placed += 1
        cash -= (contracts * our_price)

    print(f"\n✅ Successfully queued {placed} active high-volatility risky trades to the GUI database!")
    
    await sentiment_analyzer.close()

if __name__ == "__main__":
    asyncio.run(run_risky_strategy())
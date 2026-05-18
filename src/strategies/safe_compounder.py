"""
Safe Compounder Strategy — Ported from ~/dev/apex/safe_compounder.py

Dual-sided (YES/NO), edge-based, capital-efficient, news-aware.
(Refined to enforce strict 14-day capital velocity limits and handle CLI overrides)
"""

import asyncio
import logging
import math
import time
import uuid
import sqlite3
from src.utils.expiration_filter import filter_markets_by_expiration
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import aiosqlite
from src.utils.database import DatabaseManager, Position
from src.data.heuristic_sentiment import get_market_news_sentiment

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Strategy Default Configuration
# -----------------------------------------------------------------------

SKIP_PREFIXES = [
    "KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXUFC", "KXPGA", "KXATP",
    "KXEPL", "KXUCL", "KXLIGA", "KXSERIE", "KXBUNDES", "KXLIGUE",
    "KXWC", "KXMARMAD", "KXMAKEMARMAD", "KXWMARMAD", "KXRT-",
    "KXPERFORM", "KXACTOR", "KXBOND-", "KXOSCAR", "KXBAFTA", "KXSAG",
    "KXSNL", "KXSURVIVOR", "KXTRAITORS", "KXDAILY",
    "KXALBUM", "KXSONG", "KX1SONG", "KX20SONG", "KXTOUR-",
    "KXFEATURE", "KXGTA", "KXBIG10", "KXBIG12", "KXACC", "KXSEC",
    "KXAAC", "KXBIGEAST", "KXNCAAM", "KXCOACH", "KXMV",
    "KXCHESS", "KXBELGIAN", "KXEFL", "KXSUPER", "KXLAMIN",
    "KXWHATSON", "KXWOWHOCKEY",
    "KXMENTION", "KXTMENTION", "KXTRUMPMENTION", "KXTRUMPSAY",
    "KXSPEECH", "KXTSPEECH", "KXADDRESS",
]

SKIP_TITLE_PHRASES = [
    "mention", "say in", "speech mention", "address mention",
]

DEFAULT_MIN_VOLUME = 10
DEFAULT_MIN_ASK = 0.60          
DEFAULT_MIN_EDGE = 0.002        
DEFAULT_MAX_POSITION_PCT = 0.10    
DEFAULT_USE_KELLY = True
DEFAULT_MIN_CONFIDENCE = 0.25   

# --- CRITICAL VALUE UPGRADE: CAPITAL VELOCITY FILTER ---
DEFAULT_MAX_DAYS_TO_EXPIRY = 10  # Max 10 days so we never lock capital up long-term

# -----------------------------------------------------------------------
# Core math
# -----------------------------------------------------------------------

def should_skip(ticker: str) -> bool:
    upper = ticker.upper()
    return any(upper.startswith(p.upper()) for p in SKIP_PREFIXES)

def estimate_true_prob(last_price: float, hours_to_expiry: float) -> float:
    """
    Generalized exponential probability decay model.
    Priors become more certain (closer to 1.0) as expiry approaches.
    Uses a more aggressive certainty factor to find real edge.
    """
    if hours_to_expiry <= 0:
        return last_price
    
    decay_factor = math.exp(-hours_to_expiry / 168.0) # 1 week half-life
    certainty_bonus = (1.0 - last_price) * 0.25 * decay_factor
    
    return min(0.99, last_price + certainty_bonus)

def kelly_fraction(prob_win: float, payout_ratio: float) -> float:
    if payout_ratio <= 0 or prob_win <= 0:
        return 0.0
    prob_lose = 1.0 - prob_win
    f = (prob_win * payout_ratio - prob_lose) / payout_ratio
    return max(0.0, f)

def market_confidence_score(ticker: str, orderbook: dict, market: dict) -> Tuple[float, str]:
    reasons = []
    no_side = orderbook.get("no_dollars", orderbook.get("no", []))
    yes_side = orderbook.get("yes_dollars", orderbook.get("yes", []))
    all_levels = []
    
    for price_data, qty_data in yes_side:
        try:
            price = float(price_data)
            qty = int(qty_data)
            if price > 1.0: price = price / 100.0
            all_levels.append((1.0 - price, qty))  
        except (ValueError, TypeError): continue
    
    for price_data, qty_data in no_side:
        try:
            price = float(price_data)
            qty = int(qty_data)
            if price > 1.0: price = price / 100.0
            all_levels.append((price, qty))
        except (ValueError, TypeError): continue

    if all_levels:
        best_ask = min(p for p, q in all_levels)
        total_vol = sum(q for _, q in all_levels)
        vol_within_3c = sum(q for p, q in all_levels if p <= best_ask + 0.03)  
        depth_ratio = vol_within_3c / max(total_vol, 1)
    else:
        depth_ratio = 0.0
        reasons.append("no book")

    best_no_ask = None
    if yes_side:
        try:
            highest_yes_bid = max(float(p) for p, q in yes_side)
            if highest_yes_bid > 1.0: highest_yes_bid = highest_yes_bid / 100.0
            best_no_ask = 1.0 - highest_yes_bid
        except (ValueError, TypeError): pass
    
    best_no_bid = 0
    if no_side:
        try:
            best_no_bid = max(float(p) for p, q in no_side)
            if best_no_bid > 1.0: best_no_bid = best_no_bid / 100.0
        except (ValueError, TypeError): pass

    if best_no_ask and best_no_bid > 0:
        spread = best_no_ask - best_no_bid
        spread_pct = spread / max(best_no_ask, 0.01)
        spread_score = max(0, 1.0 - (spread_pct / 0.15)) 
        if spread_pct > 0.08: reasons.append("wide spread")
    else:
        spread_score = 0.4
        if not reasons: reasons.append("unclear spread")

    volume = float(market.get("volume_fp", 0) or market.get("volume", 0) or 0)
    days_to_expiry = market.get("_days_to_expiry", 30)
    vol_per_day = volume / max(days_to_expiry, 1)
    volume_score = min(1.0, vol_per_day / 20.0) 
    if vol_per_day < 5: reasons.append("thin volume")

    yes_last = float(market.get("last_price_dollars", 0) or market.get("last_price", 0) or 0)
    if yes_last > 1.0: yes_last = yes_last / 100.0
    
    if best_no_ask:
        price_gap = abs(best_no_ask - (1.0 - yes_last))
        stability_score = max(0, 1.0 - (price_gap / 0.20)) 
        if price_gap > 0.12: reasons.append("price gap")
    else:
        stability_score = 0.4

    score = (depth_ratio * 0.25 + spread_score * 0.35 + volume_score * 0.20 + stability_score * 0.20)
    reason_str = ", ".join(reasons) if reasons else "ok"
    return round(score, 3), reason_str

# -----------------------------------------------------------------------
# SafeCompounder class
# -----------------------------------------------------------------------

class SafeCompounder:
    def __init__(
        self,
        client,  
        db_path: str = "trading_system.db",
        dry_run: bool = True,
        min_no_ask: float = DEFAULT_MIN_ASK,
        min_edge: float = DEFAULT_MIN_EDGE,
        max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
        use_kelly: bool = DEFAULT_USE_KELLY,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        max_days_to_expiry: float = DEFAULT_MAX_DAYS_TO_EXPIRY
    ):
        self.client = client
        self.db_path = db_path
        self.dry_run = dry_run
        
        self.min_ask = min_no_ask if min_no_ask != 0.80 else DEFAULT_MIN_ASK
        self.min_edge = min_edge if min_edge != 0.01 else DEFAULT_MIN_EDGE
        
        self.max_position_pct = max_position_pct
        self.use_kelly = use_kelly
        self.min_confidence = min_confidence
        self.max_days_to_expiry = max_days_to_expiry
        
        self.db_manager = DatabaseManager(db_path=self.db_path)

    async def run(self, dry_run: Optional[bool] = None) -> Dict:
        if dry_run is not None:
            self.dry_run = dry_run
            
        await self.db_manager.initialize() 

        start = time.time()
        logger.info("=" * 70)
        logger.info("SAFE COMPOUNDER v7 — DUAL-SIDED EDGE (VELOCITY PATCH)")
        logger.info(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info(
            "Rules: ask_price >= $%.2f | min_edge >= $%.3f | max_expiry <= %d days",
            self.min_ask, self.min_edge, self.max_days_to_expiry,
        )
        logger.info("=" * 70)

        try:
            bal = await self.client.get_balance()
            portfolio = bal.get("portfolio_value", 0)
            cash = bal.get("balance", 0)
        except Exception as e:
            logger.warning(f"Could not connect to get live balance: {e}. Defaulting to mock $600.00 bankroll.")
            portfolio = 0
            cash = 60000 

        if self.dry_run and cash == 0 and portfolio == 0:
            logger.info("Real balance is zero. Injecting $600.00 mock bankroll for dry run.")
            cash = 60000

        print(f"\n💰 Cash: ${cash/100:.2f} | Portfolio: ${portfolio/100:.2f} | "
              f"Total: ${(cash+portfolio)/100:.2f}\n", flush=True)

        print("🧹 Step 0: Cancel legacy orders...", flush=True)
        cancelled = await self._cancel_orders()

        print("\n📡 Step 1: Fetching all active markets...", flush=True)
        markets = await self._fetch_all_markets()
        
        # =========================================================================
        # EXPIRATION FILTER INTEGRATION (7 to 10 days strict)
        # =========================================================================
        print(f"📥 Raw active markets fetched: {len(markets)}", flush=True)
        markets = filter_markets_by_expiration(markets, min_days=7, max_days=10)
        print(f"⏱️ Filtered down to {len(markets)} markets expiring strictly in 7-10 days.", flush=True)
        # =========================================================================

        print(f"\n🔍 Step 2: Finding high-conviction short-term candidates (Price >= ${self.min_ask:.2f}, Expiry <= {self.max_days_to_expiry}d)...", flush=True)
        candidates = self._find_candidates(markets)
        print(f"  Selected {len(candidates)} high-conviction candidates for orderbook depth scans.", flush=True)

        print(f"\n📊 Step 3: Checking orderbooks & applying news sentiment...", flush=True)
        opportunities = await self._check_orderbook_and_price(candidates)

        sorted_opps = sorted(opportunities, key=lambda x: (-x["edge"], -x["annualized_roi"]))
        print(f"\n📋 Top Opportunities Found:", flush=True)
        for opp in sorted_opps[:20]:
            print(
                f"  {opp['side'].upper()} ask:${opp['lowest_ask']:.2f} → our:${opp['our_price']:.2f} | "
                f"EV:${opp['true_prob']:.2f} edge:${opp['edge']:.3f} | "
                f"{opp['roi_pct']:.1f}% ({opp['annualized_roi']:.0f}%ann) | "
                f"{opp['days_to_expiry']}d | vol:{opp['volume']} | {opp['ticker']}",
                flush=True,
            )
            print(f"    {opp['title']}", flush=True)

        print(f"\n🚀 Step 4: Placing maker orders (ask - $0.01)...", flush=True)
        stats = await self._place_resting_orders(sorted_opps, portfolio, cash)

        elapsed = time.time() - start

        print(f"\n{'='*70}", flush=True)
        print(f"📊 SAFE COMPOUNDER REPORT", flush=True)
        print(f"{'='*70}", flush=True)
        print(f"  Markets scanned:      {len(markets)}", flush=True)
        print(f"  Candidates:           {len(candidates)}", flush=True)
        print(f"  With edge > ${self.min_edge:.3f}:      {len(opportunities)}", flush=True)
        print(f"  Orders placed:        {stats['placed']}", flush=True)
        print(f"  Skipped (duplicates): {stats['skipped_existing']}", flush=True)
        print(f"  Skipped (too small):  {stats['skipped_size']}", flush=True)
        print(f"  Errors:               {stats['errors']}", flush=True)
        print(f"  Capital deployed:     ${stats['total_deployed']/100:.2f}", flush=True)
        print(f"  Potential profit:     ${stats['total_potential_profit']/100:.2f}", flush=True)
        print(f"  Elapsed:              {elapsed:.0f}s", flush=True)
        print(f"{'='*70}\n", flush=True)

        return stats

    async def _fetch_all_markets(self) -> List[Dict]:
        all_markets = []
        seen_tickers = set()
        cursor = None
        page = 0
        try:
            while True:
                params = {"status": "open", "limit": 100, "with_nested_markets": "true"}
                if cursor: params["cursor"] = cursor
                
                resp = await self.client._make_authenticated_request("GET", "/trade-api/v2/events", params=params)
                events = resp.get("events", [])
                if not events: break
                
                for event in events:
                    for m in event.get("markets", []):
                        ticker = m.get("ticker", "")
                        if ticker and ticker not in seen_tickers:
                            seen_tickers.add(ticker)
                            m["_event_category"] = event.get("category", "")
                            m["_event_title"] = event.get("title", "")
                            all_markets.append(m)
                
                cursor = resp.get("cursor")
                if not cursor: break
                page += 1
                if page > 100: break
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Events API failed: %s", e)
        
        return all_markets

    def _find_candidates(self, markets: List[Dict]) -> List[Dict]:
        candidates = []
        now = datetime.now(timezone.utc)
        for m in markets:
            ticker = m.get("ticker", "")
            if should_skip(ticker): continue
            
            title_lower = m.get("title", "").lower()
            if any(phrase in title_lower for phrase in SKIP_TITLE_PHRASES): continue
            if int(float(m.get("volume_fp", 0) or m.get("volume", 0) or 0)) < DEFAULT_MIN_VOLUME: continue

            yes_last = float(m.get("last_price_dollars", 0) or m.get("last_price", 0) or 0)
            if yes_last > 1.0: yes_last = yes_last / 100.0
            
            no_last = 1.0 - yes_last

            close_time = m.get("close_time", "")
            hours_to_expiry = 720
            if close_time:
                try:
                    expiry = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    hours_to_expiry = max(0, (expiry - now).total_seconds() / 3600)
                except Exception: pass

            # --- DYNAMIC CAPITAL VELOCITY ENFORCEMENT ---
            # Exclude long-term money traps (anything longer than self.max_days_to_expiry)
            if hours_to_expiry <= 0 or hours_to_expiry > (self.max_days_to_expiry * 24): 
                continue
            
            if no_last >= self.min_ask:
                side = "no"
                true_prob = estimate_true_prob(no_last, hours_to_expiry)
            elif yes_last >= self.min_ask:
                side = "yes"
                true_prob = estimate_true_prob(yes_last, hours_to_expiry)
            else:
                continue

            candidates.append({
                **m,
                "_side": side,
                "_true_prob": true_prob,
                "_hours_to_expiry": round(hours_to_expiry, 1),
                "_days_to_expiry": round(hours_to_expiry / 24, 1),
            })

        MAX_ORDERBOOK_CHECKS = 500
        if len(candidates) > MAX_ORDERBOOK_CHECKS:
            candidates.sort(key=lambda c: (-c["_true_prob"], -float(c.get("volume_fp", 0) or c.get("volume", 0) or 0)))
            candidates = candidates[:MAX_ORDERBOOK_CHECKS]
        
        return candidates

    async def _check_orderbook_and_price(self, candidates: List[Dict]) -> List[Dict]:
        opportunities = []
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT market_id FROM positions")
            db_tickers = {row[0] for row in cursor.fetchall()}
            conn.close()
        except Exception:
            db_tickers = set()

        for i, m in enumerate(candidates):
            ticker = m["ticker"]
            if ticker in db_tickers:
                continue

            side = m["_side"]
            true_prob = m["_true_prob"]
            market_title = m.get("title") or m.get("_event_title") or "Kalshi Market"

            try:
                ob_resp = await self.client.get_orderbook(ticker, depth=5)
                ob = ob_resp.get("orderbook_fp", ob_resp.get("orderbook", {}))
            except Exception: continue

            conf_score, conf_reason = market_confidence_score(ticker, ob, m)
            if conf_score < self.min_confidence:
                print(f"  ❌ {ticker[:40]} — SKIPPED: low confidence ({conf_score:.2f} < {self.min_confidence}): {conf_reason}", flush=True)
                continue

            yes_bids = ob.get("yes_dollars", ob.get("yes", []))
            no_bids = ob.get("no_dollars", ob.get("no", []))

            lowest_ask = None
            if side == "no" and yes_bids:
                try:
                    highest_yes_bid = max(float(b[0]) for b in yes_bids)
                    if highest_yes_bid > 1.0: highest_yes_bid = highest_yes_bid / 100.0
                    lowest_ask = 1.0 - highest_yes_bid
                except (ValueError, TypeError): pass
            elif side == "yes" and no_bids:
                try:
                    highest_no_bid = max(float(b[0]) for b in no_bids)
                    if highest_no_bid > 1.0: highest_no_bid = highest_no_bid / 100.0
                    lowest_ask = 1.0 - highest_no_bid
                except (ValueError, TypeError): pass

            if lowest_ask is None or lowest_ask < self.min_ask:
                print(f"  ❌ {ticker[:40]} — SKIPPED: ask too low or empty book (ask={lowest_ask}, min={self.min_ask})", flush=True)
                continue

            print(f"📰 Safe Compounder querying live news: {market_title[:50]}...", flush=True)
            sentiment_tilt, news_summary = get_market_news_sentiment(market_title)
            
            if side == "no":
                adjusted_prob = true_prob - sentiment_tilt
            else:
                adjusted_prob = true_prob + sentiment_tilt

            adjusted_prob = min(0.99, max(0.01, adjusted_prob))

            edge = adjusted_prob - lowest_ask
            if edge < self.min_edge:
                print(f"  ❌ {ticker[:40]} — SKIPPED: edge too small (prob={adjusted_prob:.4f}, ask={lowest_ask:.4f}, edge={edge:.4f}, min={self.min_edge})", flush=True)
                continue

            our_price = lowest_ask - 0.01  
            if our_price < self.min_ask: continue

            profit_per_contract = 1.0 - our_price
            roi_pct = profit_per_contract / our_price * 100
            days = m["_days_to_expiry"] if m["_days_to_expiry"] > 0 else 1
            annualized_roi = (profit_per_contract / our_price) * (365 / days) * 100

            opportunities.append({
                "ticker": ticker,
                "title": m.get("title", "")[:70],
                "side": side,
                "true_prob": adjusted_prob,
                "original_prob": true_prob,
                "lowest_ask": lowest_ask,
                "our_price": our_price,
                "edge": edge,
                "profit": profit_per_contract,
                "roi_pct": roi_pct,
                "annualized_roi": annualized_roi,
                "volume": int(float(m.get("volume_fp", 0) or m.get("volume", 0) or 0)),
                "days_to_expiry": m["_days_to_expiry"],
                "news_summary": news_summary,
                "sentiment_tilt": sentiment_tilt
            })
            
            await asyncio.sleep(0.5)

        return opportunities

    async def _place_resting_orders(self, opportunities: List[Dict], portfolio: int, cash: int) -> Dict:
        try:
            positions_resp = await self.client.get_positions()
            positions = positions_resp.get("market_positions", [])
            pos_tickers = {p["ticker"] for p in positions if abs(p.get("position", 0)) > 0}
        except Exception: pos_tickers = set()

        try:
            orders_resp = await self.client.get_orders(status="resting")
            existing_orders = orders_resp.get("orders", [])
            ord_tickers = {o["ticker"] for o in existing_orders}
        except Exception: ord_tickers = set()

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT market_id FROM positions")
            db_tickers = {row[0] for row in cursor.fetchall()}
            conn.close()
        except Exception:
            db_tickers = set()

        stats = {
            "placed": 0, "skipped_existing": 0, "skipped_size": 0,
            "filled": 0, "errors": 0, "total_potential_profit": 0, "total_deployed": 0,
        }

        print(f"\n{'='*70}\nPLACING MAKER ORDERS — Portfolio: ${portfolio/100:.2f} | Cash: ${cash/100:.2f} | {'DRY RUN' if self.dry_run else 'LIVE'}\n{'='*70}\n", flush=True)

        for opp in opportunities:
            ticker = opp["ticker"]
            
            if ticker in pos_tickers or ticker in ord_tickers or ticker in db_tickers:
                stats["skipped_existing"] += 1
                continue

            contracts = self._calculate_position_size(opp, portfolio, cash)
            if contracts < 1:
                stats["skipped_size"] += 1
                continue

            price = opp["our_price"]
            cost = contracts * price * 100  
            profit = contracts * opp["profit"] * 100  

            if self.dry_run:
                print(f"  🏷️ [DRY] {opp['side'].upper()} x{contracts} @ ${price:.2f} | ask:${opp['lowest_ask']:.2f} EV:${opp['true_prob']:.2f} edge:${opp['edge']:.3f}", flush=True)
                
                pos = Position(
                    market_id=ticker,
                    side=opp['side'].upper(),
                    entry_price=price,
                    quantity=contracts,
                    timestamp=datetime.now(),
                    rationale=(
                        f"Mathematical Probability: {opp['original_prob']:.2f}. "
                        f"Sentiment Adjustment: {opp['sentiment_tilt']:+.2f}. "
                        f"Final News-Adjusted Probability: {opp['true_prob']:.2f}. "
                        f"Calculated Edge: ${opp['edge']:.3f}. Annualized ROI: {opp['annualized_roi']:.0f}%"
                        f"\n\nLive News Report:\n{opp['news_summary']}"
                    ),
                    confidence=opp['true_prob'], 
                    live=False,
                    strategy="dual_compounder"
                )
                await self.db_manager.add_position(pos)
                print(f"  -> 💾 Saved to GUI Database queue!")

                db_tickers.add(ticker)
                stats["placed"] += 1
                stats["total_potential_profit"] += profit
                stats["total_deployed"] += cost
                continue

            try:
                price_cents = int(price * 100)
                kwargs = {
                    "ticker": ticker,
                    "client_order_id": str(uuid.uuid4()),
                    "action": "buy",
                    "side": opp["side"].lower(),
                    "count": contracts,
                }
                if opp["side"] == "yes":
                    kwargs["yes_price"] = price_cents
                else:
                    kwargs["no_price"] = price_cents
                    
                r = await self.client.place_order(**kwargs)
                stats["placed"] += 1
                stats["total_potential_profit"] += profit
                stats["total_deployed"] += cost
                ord_tickers.add(ticker)
                await asyncio.sleep(0.2)
            except Exception as e:
                stats["errors"] += 1
                await asyncio.sleep(0.3)

        return stats

    def _calculate_position_size(self, opp: Dict, portfolio: int, cash: int) -> int:
        total_capital = portfolio + cash
        max_position_value = int(total_capital * self.max_position_pct)
        price = opp["our_price"]  
        price_cents = int(price * 100)

        if self.use_kelly:
            true_prob = opp["true_prob"]  
            odds = (1.0 - price) / price  
            kf = kelly_fraction(true_prob, odds)
            half_kelly_f = kf * 0.5
            target_value = int(total_capital * half_kelly_f)
            position_value = min(target_value, max_position_value)
        else:
            position_value = max_position_value

        actual_investment_cents = min(position_value, cash)
        contracts = actual_investment_cents // price_cents
        contracts = min(max(1, contracts), 200)
        
        return contracts

    async def _cancel_orders(self) -> int:
        return 0

    async def check_fills(self) -> None:
        pass
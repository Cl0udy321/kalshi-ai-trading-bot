import logging
from datetime import datetime, timedelta, timezone
from typing import List, Any

logger = logging.getLogger(__name__)

def filter_markets_by_expiration(markets: List[Any], min_days: int = 5, max_days: int = 7) -> List[Any]:
    """
    Filters Kalshi markets to strictly return those expiring within a specified day range.
    Handles both raw dictionary responses and Kalshi SDK objects.
    
    Args:
        markets: List of Kalshi market objects or dictionaries.
        min_days: Minimum number of days until expiration (default: 5)
        max_days: Maximum number of days until expiration (default: 7)
        
    Returns:
        List of markets that fall strictly within the expiration window.
    """
    filtered_markets = []
    now_utc = datetime.now(timezone.utc)
    
    # Calculate exact future time boundaries
    min_time = now_utc + timedelta(days=min_days)
    max_time = now_utc + timedelta(days=max_days)

    for market in markets:
        # Handle both dictionary (raw JSON) and Kalshi SDK object property access
        if isinstance(market, dict):
            # Kalshi v2 typically uses 'close_time', but we check alternatives just in case
            expiration_str = market.get('close_time') or market.get('close_date') or market.get('expiration_ts')
            ticker = market.get('ticker', 'Unknown')
        else:
            expiration_str = getattr(market, 'close_time', None) or getattr(market, 'close_date', None) or getattr(market, 'expiration_ts', None)
            ticker = getattr(market, 'ticker', 'Unknown')
        
        if not expiration_str:
            continue
            
        try:
            # Parse ISO 8601 string. Replace 'Z' with '+00:00' for Python compatibility
            if isinstance(expiration_str, str):
                expiration_str = expiration_str.replace('Z', '+00:00')
                expiration_date = datetime.fromisoformat(expiration_str)
            elif isinstance(expiration_str, datetime):
                expiration_date = expiration_str
            else:
                continue
            
            # Ensure the datetime object is timezone-aware (UTC)
            if expiration_date.tzinfo is None:
                expiration_date = expiration_date.replace(tzinfo=timezone.utc)

            # Strict check: Does expiration fall in the 5 to 7 day window?
            if min_time <= expiration_date <= max_time:
                filtered_markets.append(market)
                
        except (ValueError, TypeError) as e:
            logger.debug(f"Could not parse expiration date for market {ticker}: {e}")
            continue

    logger.info(f"Filtered {len(markets)} total markets down to {len(filtered_markets)} expiring in {min_days}-{max_days} days.")
    return filtered_markets
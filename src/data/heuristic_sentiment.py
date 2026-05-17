import re
import urllib.parse
import xml.etree.ElementTree as ET
import urllib.request
from typing import Dict, List, Tuple

# Simple but highly effective dictionary of trading-focused sentiment keywords
SENTIMENT_LEXICON = {
    # Positive / Bullish keywords
    "bullish": 0.4, "approved": 0.5, "passed": 0.5, "success": 0.4, "agreed": 0.3,
    "won": 0.4, "grows": 0.3, "rising": 0.3, "surges": 0.5, "positive": 0.3,
    "consensus": 0.3, "deal": 0.4, "resolved": 0.3, "settled": 0.3, "confirmed": 0.4,
    "victory": 0.5, "climb": 0.3, "higher": 0.2, "breakthrough": 0.5, "support": 0.2,
    
    # Negative / Bearish keywords
    "bearish": -0.4, "rejected": -0.5, "failed": -0.5, "blocked": -0.4, "denied": -0.4,
    "lost": -0.4, "drops": -0.3, "falling": -0.3, "plummets": -0.5, "negative": -0.3,
    "disagreement": -0.3, "deadlock": -0.4, "delay": -0.3, "canceled": -0.5, "halted": -0.4,
    "deficit": -0.3, "lower": -0.2, "opposed": -0.3, "crisis": -0.4, "vetoed": -0.5
}

def clean_search_query(title: str) -> str:
    """Cleans a market title to produce a clean Google News search string."""
    # Remove question marks, common punctuation, and filler words
    cleaned = re.sub(r'[?.,\/#!$%\^&\*;:{}=\-_`~()]', '', title)
    words = cleaned.split()
    # Filter out extremely common words to avoid search dilution
    stopwords = {"will", "be", "the", "on", "at", "by", "for", "who", "what", "how", "many", "price", "of", "to", "in", "after", "any"}
    filtered_words = [w for w in words if w.lower() not in stopwords]
    return " ".join(filtered_words[:4]) # Keep to top 4 search terms

def fetch_google_news_rss(query: str) -> List[str]:
    """Fetches headlines from Google News RSS feed for free without API keys."""
    headlines = []
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
        
        # Fake a user-agent to bypass basic bot blockers
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            xml_data = response.read()
            
        root = ET.fromstring(xml_data)
        # Parse titles from the feed items
        for item in root.findall('.//item')[:8]:  # Analyze top 8 relevant articles
            title_elem = item.find('title')
            if title_elem is not None and title_elem.text:
                headlines.append(title_elem.text)
    except Exception:
        # Silently fail if network or Google News blocks requests
        pass
    return headlines

def analyze_sentiment(headlines: List[str]) -> Tuple[float, List[str]]:
    """
    Scores the news list based on keyword sentiment rules.
    Returns a score between -1.0 (extremely negative/bearish) and +1.0 (extremely positive/bullish).
    """
    if not headlines:
        return 0.0, []

    total_score = 0.0
    matched_highlights = []

    for h in headlines:
        h_lower = h.lower()
        article_score = 0.0
        matched_words = []
        
        for word, value in SENTIMENT_LEXICON.items():
            # Use word boundaries to prevent substring false-positives
            pattern = r'\b' + re.escape(word) + r'\b'
            if re.search(pattern, h_lower):
                article_score += value
                matched_words.append(word)
                
        if article_score != 0.0:
            total_score += article_score
            sign = "+" if article_score > 0 else "-"
            matched_highlights.append(f"({sign}) {h[:50]}... [matches: {', '.join(matched_words)}]")

    # Normalize score between -1.0 and +1.0
    num_matches = len(matched_highlights)
    if num_matches > 0:
        avg_score = total_score / num_matches
        normalized = max(-1.0, min(1.0, avg_score))
        return round(normalized, 3), matched_highlights
    
    return 0.0, []

def get_market_news_sentiment(market_title: str) -> Tuple[float, str]:
    """
    Main orchestrator: queries Google News and scores the market.
    Returns:
       sentiment_tilt: float between -0.10 and +0.10 (to adjust probabilities gently)
       summary_rationale: string describing what was found
    """
    query = clean_search_query(market_title)
    headlines = fetch_google_news_rss(query)
    
    if not headlines:
        return 0.0, "No live news articles found for this market topic."
        
    score, highlights = analyze_sentiment(headlines)
    
    # Scale score down so we never tilt probabilities by more than 10% (staying mathematically safe)
    sentiment_tilt = score * 0.10
    
    if sentiment_tilt > 0:
        summary = f"Positive news detected (Score: {score:.2f}). Recent Headlines:\n" + "\n".join(highlights[:3])
    elif sentiment_tilt < 0:
        summary = f"Negative news detected (Score: {score:.2f}). Recent Headlines:\n" + "\n".join(highlights[:3])
    else:
        summary = "Neutral news coverage. No strong positive or negative indicators found."
        
    return sentiment_tilt, summary
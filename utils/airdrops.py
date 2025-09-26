
import feedparser
from typing import List, Tuple

AIR_FEEDS = [
    "https://airdrops.io/latest/feed",
    "https://cryptorank.io/airdrops/feed",
]

def fetch_airdrops(query: str = "", limit: int = 6) -> List[Tuple[str,str,str]]:
    out = []
    for url in AIR_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                title = (getattr(e, "title", "") or "").strip()
                link  = (getattr(e, "link", "") or "").strip()
                summary = (getattr(e, "summary", "") or "")
                if not title or not link:
                    continue
                if query and query.lower() not in title.lower():
                    continue
                out.append((title, link, summary))
                if len(out) >= limit:
                    break
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out[:limit]

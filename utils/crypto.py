
import requests
from datetime import datetime, timezone

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_GLOBAL  = "https://api.coingecko.com/api/v3/global"
COINGECKO_MC      = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"
COINGECKO_OHLC    = "https://api.coingecko.com/api/v3/coins/{id}/ohlc"

SYMBOL_MAP = {
    "btc":"bitcoin","xbt":"bitcoin","eth":"ethereum","bnb":"binancecoin","sol":"solana",
    "usdt":"tether","usdc":"usd-coin","xrp":"ripple","ada":"cardano","doge":"dogecoin","ton":"toncoin",
    "dot":"polkadot","matic":"matic-network","avax":"avalanche-2","ltc":"litecoin","shib":"shiba-inu",
    "link":"chainlink","trx":"tron","op":"optimism","arb":"arbitrum","sui":"sui","sei":"sei-network",
    "near":"near","atom":"cosmos","cake":"pancakeswap-token"
}

def norm_symbol(sym: str) -> str:
    return SYMBOL_MAP.get(sym.lower().lstrip("$"), sym.lower().lstrip("$"))

def fmt_price(val, fiat):
    try:
        if fiat == "idr": return f"Rp {val:,.0f}".replace(",", ".")
        if fiat in ("usd","usdt"): return f"${val:,.2f}"
        if fiat == "eur": return f"€{val:,.2f}"
        return f"{val:,.4f} {fiat.upper()}"
    except Exception:
        return f"{val} {fiat.upper()}"

def fetch_price(ids, fiat: str) -> dict:
    r = requests.get(COINGECKO_SIMPLE,
        params={"ids": ",".join(ids), "vs_currencies": fiat, "include_24hr_change": "true"},
        timeout=20)
    r.raise_for_status()
    return r.json()

def cg_time(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

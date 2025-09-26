# bot.py
# Telegram bot konversi crypto multi-koin + AI fallback parser

import os, re, json, time, logging, requests
from typing import Dict, Optional, Tuple
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("Harap isi TELEGRAM_BOT_TOKEN di .env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crypto-bot")

CG_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
CG_LIST   = "https://api.coingecko.com/api/v3/coins/list?include_platform=false"

FIAT_ALIASES = {"idr":"idr","rupiah":"idr","rp":"idr","usd":"usd","dollar":"usd","eur":"eur","euro":"eur"}

TICKER_MAP: Dict[str,str] = {"btc":"bitcoin","eth":"ethereum","bnb":"binancecoin","xrp":"ripple",
    "doge":"dogecoin","ada":"cardano","sol":"solana","dot":"polkadot","trx":"tron","avax":"avalanche-2",
    "matic":"matic-network","usdt":"tether","usdc":"usd-coin","busd":"binance-usd","shib":"shiba-inu"}

_PRICE_CACHE: Dict[Tuple[str,str],Tuple[float,dict]] = {}
PRICE_TTL=30.0
_DYNAMIC_MAP: Dict[str,str]={}
_DYNAMIC_LAST=0.0
DYNAMIC_TTL=3600.0

RE_PATTERNS=[re.compile(r"(?P<amount>[\d\.,]+)\s*(?P<src>[A-Za-z0-9\-]+)\s*(?:to|ke)?\s*(?P<dst>[A-Za-z0-9\-]+)", re.I)]

def normalize_number(s:str)->float:
    s=s.strip().replace(",","")
    return float(s)

def format_idr(x:float)->str:
    return "Rp "+f"{int(round(x)):,}".replace(",",".")

def now(): return time.time()

def fetch_dynamic_map():
    global _DYNAMIC_MAP,_DYNAMIC_LAST
    if now()-_DYNAMIC_LAST<DYNAMIC_TTL and _DYNAMIC_MAP: return
    try:
        r=requests.get(CG_LIST,timeout=30);r.raise_for_status()
        data=r.json();tmp={}
        for it in data: sym=(it.get("symbol") or "").lower().strip(); cid=it.get("id")
        if sym and cid and sym not in tmp: tmp[sym]=cid
        _DYNAMIC_MAP=tmp;_DYNAMIC_LAST=now()
    except Exception as e: log.warning("Gagal ambil list: %s",e)

def resolve_ticker(t:str)->Optional[str]:
    k=t.lower().strip()
    if k in FIAT_ALIASES: return None
    if k in TICKER_MAP: return TICKER_MAP[k]
    fetch_dynamic_map(); return _DYNAMIC_MAP.get(k,k)

def get_price(ids,vs)->dict:
    key=(ids,vs); t=now()
    if key in _PRICE_CACHE and t-_PRICE_CACHE[key][0]<PRICE_TTL: return _PRICE_CACHE[key][1]
    r=requests.get(CG_SIMPLE,params={"ids":ids,"vs_currencies":vs},timeout=20);r.raise_for_status()
    data=r.json(); _PRICE_CACHE[key]=(t,data); return data

def parse_query(text:str)->Optional[dict]:
    for p in RE_PATTERNS:
        m=p.search(text)
        if m: return {"amount":normalize_number(m["amount"]),"src":m["src"].lower(),"dst":m["dst"].lower()}
    return None

async def handle_text(update:Update,context:ContextTypes.DEFAULT_TYPE):
    text=(update.message.text or "").strip()
    q=parse_query(text)
    if not q: 
        await update.message.reply_text("Format salah. Contoh: 0.1 btc idr"); return
    amount,src_t,dst_t=q["amount"],q["src"],q["dst"]
    if dst_t in FIAT_ALIASES:
        vs=FIAT_ALIASES[dst_t]; src_id=resolve_ticker(src_t)
        if not src_id: await update.message.reply_text(f"Ticker {src_t} tidak dikenali"); return
        prices=get_price(src_id,vs); unit=prices[src_id][vs]; total=amount*unit
        msg=f"{amount:g} {src_t.upper()} ≈ {format_idr(total) if vs=='idr' else f'{total:,.6f} {vs.upper()}'}"
        await update.message.reply_text(msg); return
    src_id=resolve_ticker(src_t); dst_id=resolve_ticker(dst_t)
    prices=get_price(f"{src_id},{dst_id}","usd")
    usd_value=amount*prices[src_id]["usd"]; dst_amount=usd_value/prices[dst_id]["usd"]
    await update.message.reply_text(f"{amount:g} {src_t.upper()} ≈ {dst_amount:.6f} {dst_t.upper()} (≈ ${usd_value:,.2f} USD)")

def main():
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    log.info("Bot started"); app.run_polling()

if __name__=="__main__": main()

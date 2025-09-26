#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AirdropCore SUPER Bot (AI + Crypto)
Fitur:
- AI Smart (/ask, smarter system prompt)
- Harga: /price, /prices, /convert, auto-detect
- Market: /top, /dominance, /fear, /gas (opsional Etherscan)
- Alerts: /alert add|del & /alerts (cek tiap 60s)
- OHLC: /ohlc <sym> <fiat> [days]
- Chart: /chart <sym> <fiat> [days] (PNG grafik harga)
- Portfolio: /addport, /delport, /portfolio, /clearport
- News: /news [query] [n] (ringkas pakai AI jika tersedia)
- Inline query: @bot "btc usd"
- Per-chat FIAT preference & rate-limit

By AirdropCore.com
"""

import os, json, logging, re, time, io
from pathlib import Path
from typing import Dict, List, Tuple
from datetime import datetime, timezone

import requests

from telegram import (
    Update, InlineQueryResultArticle, InputTextMessageContent,
    InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, InlineQueryHandler
)

# Optional helpers
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

# AI
client = None
def init_openai(api_key: str):
    global client
    if not api_key or "PASTE_" in api_key: return
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        logging.info("OpenAI client ready")
    except Exception as e:
        logging.error("OpenAI init failed: %s", e)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
if load_dotenv and ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH, override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_TELEGRAM_BOT_TOKEN_DI_SINI").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "PASTE_OPENAI_API_KEY_DI_SINI").strip()
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "").strip()

DEFAULT_GLOBAL_FIAT = os.getenv("FIAT_DEFAULT", "usd").lower()
FIAT_ALLOWED = {"usd", "usdt", "idr", "eur"}

COOLDOWN_S_GLOBAL = float(os.getenv("COOLDOWN_GLOBAL", "1.0"))
COOLDOWN_S_PER_USER = float(os.getenv("COOLDOWN_PER_USER", "1.0"))

SETTINGS_FILE  = BASE_DIR / "settings.json"
ALERTS_FILE    = BASE_DIR / "alerts.json"
PORTFOLIO_FILE = BASE_DIR / "portfolio.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("airdropcore-super-bot")

# ============== Persistence ==============
def load_json(p: Path, default):
    if p.exists():
        try: return json.loads(p.read_text("utf-8"))
        except Exception: return default
    return default

def save_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

SETTINGS  = load_json(SETTINGS_FILE, {})
ALERTS    = load_json(ALERTS_FILE, [])
PORTFOLIO = load_json(PORTFOLIO_FILE, {})  # { chat_id: {sym: amount, ...} }

def get_chat_fiat(chat_id: int) -> str:
    return SETTINGS.get(str(chat_id), {}).get("fiat", DEFAULT_GLOBAL_FIAT)

def set_chat_fiat(chat_id: int, fiat: str):
    SETTINGS.setdefault(str(chat_id), {})["fiat"] = fiat
    save_json(SETTINGS_FILE, SETTINGS)

# ============== Rate Limit ==============
_last_global = 0.0
_last_user: Dict[int, float] = {}

def allowed_now(user_id: int) -> bool:
    global _last_global
    now = time.time()
    if now - _last_global < COOLDOWN_S_GLOBAL: return False
    if now - _last_user.get(user_id, 0) < COOLDOWN_S_PER_USER: return False
    _last_global = now; _last_user[user_id] = now; return True

# ============== Crypto helpers ==============
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

PRICE_WORD = re.compile(r"(?i)^(harga|price)\b")
PAIR_PATTERN = re.compile(r"(?i)^(?:harga|price)\s+([a-z0-9$.,]+)(?:[\/\s]+([a-z]{2,6}))?$")
CONVERT_PATTERN = re.compile(r"(?i)^(\d+(?:[.,]\d+)?)\s*([a-z0-9$]{2,12})\s*(?:ke|to)\s*([a-z]{2,6})$")

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

def fetch_price(ids: List[str], fiat: str) -> dict:
    r = requests.get(COINGECKO_SIMPLE,
        params={"ids": ",".join(ids), "vs_currencies": fiat, "include_24hr_change": "true"},
        timeout=20)
    r.raise_for_status(); return r.json()

def cg_time(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

# ============== AI helpers ==============
def ai_chat(prompt: str, temp=0.4, max_tokens=450) -> str:
    if not client: return "⚠️ AI nonaktif (OPENAI_API_KEY belum diisi)."
    system = (
        "You are AIRA, an expert crypto & trading assistant for Telegram users. "
        "Be concise, precise, and helpful. When asked about prices, warn that markets are volatile; "
        "avoid financial advice wording. Use bullet points and short paragraphs."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content": system},
                {"role":"user","content": prompt}
            ],
            temperature=temp, max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.exception("ai_chat error")
        return f"❌ Error AI: {e}"

# ============== COMMANDS ==============
HELP_TEXT = (
    "🤖 *AirdropCore SUPER Bot* — AI + Crypto\n"
    "• /start, /help — menu\n"
    "• /ask <q> — tanya AI (lebih pintar)\n"
    "• /price <sym> [fiat] — harga 1 koin\n"
    "• /prices <sym1,sym2,...> [fiat] — multi koin\n"
    "• /convert <amount> <sym> <fiat>\n"
    "• /setfiat <idr|usd|usdt|eur>\n"
    "• /top [N], /dominance, /fear, /gas\n"
    "• /ohlc <sym> <fiat> [days]\n"
    "• /chart <sym> <fiat> [days]\n"
    "• /alerts, /alert add|del\n"
    "• /addport <sym> <amount>, /delport <sym> [amount]\n"
    "• /portfolio, /clearport\n"
    "• /news [query] [n]\n"
    "Contoh natural: `harga btc`, `0.1 btc ke idr`\n"
    "— AirdropCore.com"
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(HELP_TEXT)
help_cmd = start

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: {get_chat_fiat(update.effective_chat.id).upper()}\n"
            f"Format: /setfiat {'|'.join(sorted(FIAT_ALLOWED))}"
        ); return
    fiat = ctx.args[0].lower()
    if fiat not in FIAT_ALLOWED:
        await update.message.reply_text(f"❌ Fiat tidak dikenal. Pilih: {', '.join(sorted(FIAT_ALLOWED))}"); return
    set_chat_fiat(update.effective_chat.id, fiat)
    await update.message.reply_text(f"✅ FIAT default di-set ke {fiat.upper()}")

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; fiat = get_chat_fiat(chat_id)
    t0 = time.time()
    try: fetch_price(["bitcoin"], fiat); cg = f"✅ {(time.time()-t0)*1000:.0f} ms"
    except Exception as e: cg = f"❌ {e.__class__.__name__}"
    if client:
        t1 = time.time()
        try:
            client.responses.create(model="gpt-4o-mini", input="ping"); ai = f"✅ {(time.time()-t1)*1000:.0f} ms"
        except Exception as e: ai = f"❌ {e.__class__.__name__}"
    else: ai = "— (nonaktif)"
    await update.message.reply_text(f"🩺 Status:\n• CoinGecko: {cg}\n• OpenAI: {ai}\n• FIAT chat: {fiat.upper()}")

async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args) if ctx.args else ""
    if not prompt: await update.message.reply_text("Format: /ask <pertanyaan>"); return
    if not allowed_now(update.effective_user.id): return
    await update.message.reply_text(ai_chat(prompt))

# ---- Price / Prices / Convert ----
async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\nex: /price btc usdt"); return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else get_chat_fiat(update.effective_chat.id)).lower()
    await _reply_price(update, sym, fiat)

async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices <sym1,sym2,...> [fiat]\nex: /prices btc,eth idr"); return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else get_chat_fiat(update.effective_chat.id)).lower()
    await _reply_prices(update, syms, fiat)

async def convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amount> <coin> <fiat>\nex: /convert 0.25 btc idr"); return
    try: amount = float(str(ctx.args[0]).replace(",", "."))
    except ValueError: await update.message.reply_text("Jumlah tidak valid."); return
    sym = ctx.args[1]; fiat = ctx.args[2].lower()
    await _reply_convert(update, amount, sym, fiat)

# ---- Market ----
async def top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = 10
    if ctx.args:
        try: n = max(1, min(50, int(ctx.args[0])))
        except ValueError: pass
    r = requests.get(COINGECKO_MARKETS, params={
        "vs_currency":"usd","order":"market_cap_desc","per_page":n,"page":1,"price_change_percentage":"24h"
    }, timeout=20)
    r.raise_for_status(); coins = r.json()
    lines = []
    for i, c in enumerate(coins, 1):
        sym = c.get("symbol","").upper(); price = c.get("current_price")
        chg = c.get("price_change_percentage_24h")
        chg_txt = f"{chg:+.2f}%" if isinstance(chg,(int,float)) else "n/a"
        lines.append(f"{i:>2}. {sym:<6} ${price:,.2f}  ({chg_txt})")
    await update.message.reply_text("🏆 Top Market Cap:\n" + "\n".join(lines))

async def dominance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = requests.get(COINGECKO_GLOBAL, timeout=15); r.raise_for_status(); data = r.json()["data"]["market_cap_percentage"]
    btc_dom = data.get("btc"); await update.message.reply_text(f"👑 BTC Dominance: {btc_dom:.2f}%")

async def fear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = requests.get("https://api.alternative.me/fng/", timeout=15).json()
    val = r["data"][0]["value"]; classification = r["data"][0]["value_classification"]
    await update.message.reply_text(f"📉 Fear & Greed Index: {val} ({classification})")

async def gas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ETHERSCAN_API_KEY:
        await update.message.reply_text("⚠️ ETHERSCAN_API_KEY belum diisi."); return
    r = requests.get("https://api.etherscan.io/api",
        params={"module":"gastracker","action":"gasoracle","apikey":ETHERSCAN_API_KEY}, timeout=15).json()
    if r.get("status") != "1": await update.message.reply_text("❌ Tidak bisa ambil gas data."); return
    g = r["result"]
    await update.message.reply_text(f"⛽ Gas ETH\n• Safe: {g['SafeGasPrice']} gwei\n• Propose: {g['ProposeGasPrice']} gwei\n• Fast: {g['FastGasPrice']} gwei")

# ---- OHLC ----
async def ohlc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Format: /ohlc <sym> <fiat> [days]\ncontoh: /ohlc btc usd 7"); return
    sym = ctx.args[0].lower(); fiat = ctx.args[1].lower()
    days = 1
    if len(ctx.args) >= 3:
        try: days = int(ctx.args[2])
        except ValueError: days = 1
    if days not in (1, 7, 14, 30, 90, 180, 365): days = 1
    cid = norm_symbol(sym)
    url = COINGECKO_OHLC.format(id=cid)
    try:
        r = requests.get(url, params={"vs_currency": fiat, "days": days}, timeout=20); r.raise_for_status(); arr = r.json()
        if not isinstance(arr, list) or not arr: await update.message.reply_text("Data OHLC tidak tersedia."); return
        last = arr[-5:] if len(arr) >= 5 else arr
        lines = [f"{cg_time(t)} UTC  O:{fmt_price(o,fiat)}  H:{fmt_price(h,fiat)}  L:{fmt_price(l,fiat)}  C:{fmt_price(c,fiat)}"
                 for t,o,h,l,c in last]
        await update.message.reply_text(f"🕯️ OHLC {sym.upper()}/{fiat.upper()} (days={days})\n" + "\n".join(lines))
    except Exception as e:
        logging.exception("ohlc error"); await update.message.reply_text(f"❌ Error OHLC: {e}")

# ---- Chart (PNG) ----
async def chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Format: /chart <sym> <fiat> [days]\ncontoh: /chart btc usd 30"); return
    sym = ctx.args[0].lower(); fiat = ctx.args[1].lower(); days = int(ctx.args[2]) if len(ctx.args)>=3 and ctx.args[2].isdigit() else 30
    cid = norm_symbol(sym)
    try:
        r = requests.get(COINGECKO_MC.format(id=cid), params={"vs_currency": fiat, "days": days}, timeout=20); r.raise_for_status()
        data = r.json()
        prices = data.get("prices") or []  # [ [ts_ms, price], ... ]
        if not prices: await update.message.reply_text("Data chart tidak tersedia."); return

        # build arrays
        xs = [datetime.fromtimestamp(p[0]/1000, tz=timezone.utc) for p in prices]
        ys = [p[1] for p in prices]

        # plot with matplotlib (no style, single chart, no custom colors)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig = plt.figure()
        plt.plot(xs, ys)
        plt.title(f"{sym.upper()}/{fiat.upper()} — {days}D")
        plt.xlabel("Time (UTC)")
        plt.ylabel(f"Price ({fiat.upper()})")
        plt.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=140)
        plt.close(fig); buf.seek(0)
        await update.message.reply_photo(photo=InputFile(buf, filename=f"{sym}_{fiat}_{days}d.png"))
    except Exception as e:
        logging.exception("chart error"); await update.message.reply_text(f"❌ Error chart: {e}")

# ---- Alerts ----
async def alerts_list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; mine = [a for a in ALERTS if a["chat_id"] == chat_id]
    if not mine: await update.message.reply_text("📭 Tidak ada alert aktif."); return
    lines = [f"{i}. {a['sym'].upper()} {a['fiat'].upper()} {a['op']} {a['price']}" for i,a in enumerate(mine,1)]
    await update.message.reply_text("⏰ Alerts kamu:\n" + "\n".join(lines))

async def alert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format:\n• /alert add <sym> <fiat> above|below <price>\n• /alert del <index>\n• /alerts")
        return
    sub = ctx.args[0].lower(); chat_id = update.effective_chat.id
    if sub == "add":
        if len(ctx.args) < 5:
            await update.message.reply_text("Format: /alert add <sym> <fiat> above|below <price>"); return
        sym = ctx.args[1].lower(); fiat = ctx.args[2].lower(); op = ctx.args[3].lower()
        try: price = float(str(ctx.args[4]).replace(",", ""))
        except ValueError: await update.message.reply_text("Harga tidak valid."); return
        if op not in ("above", "below"): await update.message.reply_text("Operator harus 'above' atau 'below'."); return
        cid = norm_symbol(sym)
        ALERTS.append({"chat_id": chat_id, "sym": sym, "cid": cid, "fiat": fiat, "op": op, "price": price})
        save_json(ALERTS_FILE, ALERTS)
        await update.message.reply_text(f"✅ Alert: {sym.upper()} {fiat.upper()} {op} {price} ditambahkan.")
        return
    if sub == "del":
        if len(ctx.args) < 2: await update.message.reply_text("Format: /alert del <index>"); return
        mine_idx = [i for i,a in enumerate(ALERTS) if a["chat_id"] == chat_id]
        if not mine_idx: await update.message.reply_text("📭 Tidak ada alert aktif."); return
        try: idx = int(ctx.args[1]) - 1
        except ValueError: await update.message.reply_text("Index tidak valid."); return
        if idx < 0 or idx >= len(mine_idx): await update.message.reply_text("Index di luar jangkauan."); return
        real_idx = mine_idx[idx]; a = ALERTS.pop(real_idx); save_json(ALERTS_FILE, ALERTS)
        await update.message.reply_text(f"🗑️ Alert dihapus: {a['sym'].upper()} {a['fiat'].upper()} {a['op']} {a['price']}"); return
    await update.message.reply_text("Perintah tidak dikenal. Gunakan /alerts untuk melihat daftar.")

async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    if not ALERTS: return
    by_fiat: Dict[str, List[dict]] = {}
    for a in ALERTS: by_fiat.setdefault(a["fiat"], []).append(a)
    to_remove = []
    for fiat, arr in by_fiat.items():
        ids = list({a["cid"] for a in arr})
        try: data = fetch_price(ids, fiat)
        except Exception: continue
        for a in list(ALERTS):
            if a["fiat"] != fiat: continue
            cid = a["cid"]
            try: cur = data[cid][fiat]
            except Exception: continue
            hit = (a["op"] == "above" and cur >= a["price"]) or (a["op"] == "below" and cur <= a["price"])
            if hit:
                try:
                    txt = f"⏰ Alert hit: {a['sym'].upper()} {fiat.upper()} {a['op']} {a['price']}\nNow: {fmt_price(cur, fiat)}"
                    await context.bot.send_message(chat_id=a["chat_id"], text=txt)
                except Exception: pass
                to_remove.append(a)
    if to_remove:
        for a in to_remove:
            try: ALERTS.remove(a)
            except ValueError: pass
        save_json(ALERTS_FILE, ALERTS)

# ---- Portfolio ----
def port_get(chat_id: int) -> Dict[str, float]:
    return PORTFOLIO.get(str(chat_id), {})

def port_set(chat_id: int, d: Dict[str, float]):
    PORTFOLIO[str(chat_id)] = d; save_json(PORTFOLIO_FILE, PORTFOLIO)

async def addport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2: await update.message.reply_text("Format: /addport <sym> <amount>"); return
    sym = ctx.args[0].lower()
    try: amount = float(str(ctx.args[1]).replace(",", "."))
    except ValueError: await update.message.reply_text("Jumlah tidak valid."); return
    p = port_get(update.effective_chat.id); p[sym] = p.get(sym, 0.0) + amount; port_set(update.effective_chat.id, p)
    await update.message.reply_text(f"✅ Ditambahkan: {amount:g} {sym.upper()} ke portofolio.")

async def delport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 1: await update.message.reply_text("Format: /delport <sym> [amount]"); return
    sym = ctx.args[0].lower(); p = port_get(update.effective_chat.id)
    if sym not in p: await update.message.reply_text("Koin tidak ada di portofolio."); return
    if len(ctx.args) >= 2:
        try: amount = float(str(ctx.args[1]).replace(",", "."))
        except ValueError: await update.message.reply_text("Jumlah tidak valid."); return
        p[sym] -= amount; 
        if p[sym] <= 0: del p[sym]
    else:
        del p[sym]
    port_set(update.effective_chat.id, p); await update.message.reply_text("✅ Portofolio diperbarui.")

async def clearport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    port_set(update.effective_chat.id, {}); await update.message.reply_text("🧹 Portofolio dikosongkan.")

async def portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = port_get(update.effective_chat.id)
    if not p: await update.message.reply_text("📭 Portofolio kosong.\nTambah dengan: /addport btc 0.1"); return
    fiat = get_chat_fiat(update.effective_chat.id)
    ids = [norm_symbol(s) for s in p.keys()]
    try:
        data = fetch_price(ids, fiat)
        total = 0.0; lines = []
        for sym, amt in p.items():
            cid = norm_symbol(sym)
            price = data.get(cid, {}).get(fiat)
            if price is None:
                lines.append(f"{sym.upper():>5}  {amt:g}  = n/a"); continue
            val = float(price) * float(amt); total += val
            lines.append(f"{sym.upper():>5}  {amt:g}  = {fmt_price(val, fiat)}  (1={fmt_price(price, fiat)})")
        lines.append(f"\nTotal = {fmt_price(total, fiat)}")
        await update.message.reply_text("💼 Portofolio:\n" + "\n".join(lines))
    except Exception as e:
        logging.exception("portfolio error"); await update.message.reply_text(f"❌ Error portfolio: {e}")

# ---- News (RSS + AI summarize) ----
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed"
]

def fetch_news(query: str = "", limit: int = 6) -> List[Tuple[str,str]]:
    out = []
    import xml.etree.ElementTree as ET
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, timeout=15); r.raise_for_status()
            root = ET.fromstring(r.text)
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link") or "").strip()
                if not title or not link: continue
                if query and query.lower() not in title.lower(): continue
                out.append((title, link))
        except Exception:
            continue
        if len(out) >= limit: break
    return out[:limit]

async def news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = None; n = 6
    if ctx.args:
        # if last arg is digit -> treat as N
        if ctx.args[-1].isdigit():
            n = max(1, min(10, int(ctx.args[-1]))); query = " ".join(ctx.args[:-1]) if len(ctx.args)>1 else ""
        else:
            query = " ".join(ctx.args)
    items = fetch_news(query or "", n)
    if not items:
        await update.message.reply_text("Tidak ada berita yang cocok."); return
    if client:
        joined = "\n".join([f"- {t} ({u})" for t,u in items])
        summary = ai_chat(f"Ringkas berita berikut (Indonesia), fokus poin penting dan dampak pasar:\n{joined}", temp=0.3, max_tokens=380)
        await update.message.reply_text("📰 Ringkasan berita:\n" + summary)
    else:
        lines = [f"• {t}\n{u}" for t,u in items]
        await update.message.reply_text("📰 Berita:\n" + "\n\n".join(lines))

# ---- Text/Inline Router ----
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not allowed_now(update.effective_user.id): return

    m2 = CONVERT_PATTERN.match(text)
    if m2:
        amt = float(m2.group(1).replace(",", ".")); sym, fiat = m2.group(2), m2.group(3).lower()
        await _reply_convert(update, amt, sym, fiat); return

    m = PAIR_PATTERN.match(text) if PRICE_WORD.match(text) else None
    if m:
        raw, fiat = m.groups(); fiat = (fiat or get_chat_fiat(update.effective_chat.id)).lower()
        syms = [s.strip() for s in raw.replace(" ", "").split(",")]
        if len(syms) == 1: await _reply_price(update, syms[0], fiat)
        else: await _reply_prices(update, syms, fiat)
        return

    if client:
        await update.message.reply_text(ai_chat(text, temp=0.6, max_tokens=280))

async def inline_query(update, ctx):
    q = (update.inline_query.query or "").strip()
    if not q: return
    m = re.match(r"(?i)^([a-z0-9$.,]+)(?:[\/\s]+([a-z]{2,6}))?$", q)
    if not m: return
    sym, fiat = m.groups(); fiat = (fiat or DEFAULT_GLOBAL_FIAT).lower()
    try:
        cid = norm_symbol(sym); data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]: return
        p = data[cid][fiat]; chg = data[cid].get(f"{fiat}_24h_change")
        text = f"💰 {sym.upper()} = {fmt_price(p, fiat)}" + (f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else "")
        await update.inline_query.answer([
            InlineQueryResultArticle(id="1", title=text, input_message_content=InputTextMessageContent(text))
        ], cache_time=10, is_personal=True)
    except Exception:
        pass

# ---- Reply helpers ----
async def _reply_price(update: Update, sym: str, fiat: str):
    try:
        fiat = fiat.lower(); cid = norm_symbol(sym); data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]: await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan."); return
        price_val = data[cid][fiat]; chg = data[cid].get(f"{fiat}_24h_change"); chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price_val, fiat)}{chg_txt}")
    except Exception as e:
        logging.exception("price error"); await update.message.reply_text(f"❌ Error harga: {e}")

async def _reply_prices(update: Update, syms: List[str], fiat: str):
    try:
        ids = [norm_symbol(s) for s in syms]; data = fetch_price(ids, fiat.lower())
        lines = []
        for s, cid in zip(syms, ids):
            if cid in data and fiat.lower() in data[cid]:
                p = data[cid][fiat.lower()]; chg = data[cid].get(f"{fiat.lower()}_24h_change"); chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
                lines.append(f"{s.upper():>5} = {fmt_price(p, fiat)}{chg_txt}")
            else:
                lines.append(f"{s.upper():>5} = n/a")
        await update.message.reply_text("📊 Harga:\n" + "\n".join(lines))
    except Exception as e:
        logging.exception("prices error"); await update.message.reply_text(f"❌ Error harga: {e}")

async def _reply_convert(update: Update, amount: float, sym: str, fiat: str):
    try:
        cid = norm_symbol(sym); data = fetch_price([cid], fiat.lower())
        if cid not in data or fiat.lower() not in data[cid]: await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan."); return
        p = data[cid][fiat.lower()]; total = amount * float(p)
        await update.message.reply_text(f"🔁 {amount:g} {sym.upper()} ≈ {fmt_price(total, fiat.lower())} (1 {sym.upper()} = {fmt_price(p, fiat.lower())})")
    except Exception as e:
        logging.exception("convert error"); await update.message.reply_text(f"❌ Error konversi: {e}")

# ---- Errors & Main ----
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled error: %s", context.error)

def main():
    if not BOT_TOKEN or "PASTE_" in BOT_TOKEN:
        raise RuntimeError("Isi BOT_TOKEN di .env terlebih dahulu.")
    init_openai(OPENAI_API_KEY)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # base
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    # ai & price
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("convert", convert))
    # market
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("dominance", dominance))
    app.add_handler(CommandHandler("fear", fear))
    app.add_handler(CommandHandler("gas", gas))
    # charts
    app.add_handler(CommandHandler("ohlc", ohlc))
    app.add_handler(CommandHandler("chart", chart))
    # alerts
    app.add_handler(CommandHandler("alerts", alerts_list_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.job_queue.run_repeating(check_alerts, interval=60, first=5)
    # portfolio
    app.add_handler(CommandHandler("addport", addport))
    app.add_handler(CommandHandler("delport", delport))
    app.add_handler(CommandHandler("clearport", clearport))
    app.add_handler(CommandHandler("portfolio", portfolio))
    # news
    app.add_handler(CommandHandler("news", news))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_error_handler(on_error)

    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

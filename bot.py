import os, re, logging, requests, html, time
from typing import List, Tuple, Dict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI
import feedparser

# ===== Konfigurasi dasar =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_TOKEN_KAMU")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "ISI_API_KEY_KAMU")
FIAT_DEFAULT = "usd"

client = None
if OPENAI_API_KEY and not OPENAI_API_KEY.startswith("ISI"):
    client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("bot")

# ======= Coin resolver (lengkap) =======
# Cache: symbol -> coin_id
_COINS_CACHE: Dict[str, str] = {}
_COINS_CACHE_TS = 0

def _refresh_coins_cache(force=False):
    global _COINS_CACHE_TS
    now = time.time()
    if not force and now - _COINS_CACHE_TS < 6*60*60 and _COINS_CACHE:
        return
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/list",
                         params={"include_platform": "false"}, timeout=25)
        r.raise_for_status()
        coins = r.json()
        tmp = {}
        for c in coins:
            sym = str(c.get("symbol","")).lower()
            cid = c.get("id","")
            # prefer first seen; we'll override via /search for better relevance later
            if sym and cid and sym not in tmp:
                tmp[sym] = cid
        _COINS_CACHE.clear()
        _COINS_CACHE.update(tmp)
        _COINS_CACHE_TS = now
        log.info("Coins cache loaded: %d symbols", len(_COINS_CACHE))
    except Exception:
        log.exception("refresh coins cache failed")

def resolve_coin_id(sym: str) -> str:
    """
    Kembalikan CoinGecko ID untuk simbol apa pun.
    Urutan:
    1) mapping statis populer
    2) cache /coins/list
    3) /search (paling relevan)
    """
    mapping = {
        "btc":"bitcoin","eth":"ethereum","bnb":"binancecoin","usdt":"tether",
        "usdc":"usd-coin","sol":"solana","ada":"cardano","xrp":"ripple",
        "dot":"polkadot","doge":"dogecoin","trx":"tron","matic":"polygon",
        "ton":"the-open-network","op":"optimism","arb":"arbitrum",
        "inj":"injective","atom":"cosmos","avax":"avalanche-2","sui":"sui",
        "sei":"sei-network","apt":"aptos","tia":"celestia"
    }
    s = sym.lower()
    if s in mapping:
        return mapping[s]

    _refresh_coins_cache()
    if s in _COINS_CACHE:
        return _COINS_CACHE[s]

    # fallback ke /search
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search",
                         params={"query": s}, timeout=15)
        if r.ok:
            coins = r.json().get("coins", [])
            if coins:
                cid = coins[0].get("id")
                if cid:
                    _COINS_CACHE[s] = cid
                    return cid
    except Exception:
        log.exception("search resolve failed")

    return s  # biar ketahuan error di fetch_price

# ===== Utilitas harga =====
def fetch_price(symbols, fiat="usd"):
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        r = requests.get(url, params={
            "ids": ",".join(symbols),
            "vs_currencies": fiat,
            "include_24hr_change": "true"
        }, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("fetch_price error")
        return {}

def fmt_price(val, fiat):
    try:
        return f"{float(val):,.4f} {fiat.upper()}"
    except Exception:
        return f"{val} {fiat.upper()}"

# ====== Airdrop helpers ======
AIR_SOURCES = [
    "https://airdrops.io/feed/",
    "https://airdropsmob.com/feed/",
    "https://cryptoairdrops.io/feed/",
]

def _normalize_entry(e) -> Tuple[str,str,str]:
    title = html.unescape(e.get("title","")).strip()
    link = (e.get("link") or e.get("id") or "").strip()
    summary = html.unescape(re.sub(r"<[^>]+>", "", e.get("summary",""))).strip()
    return title, link, summary

def fetch_airdrops(keyword: str = "", limit: int = 10) -> List[Tuple[str,str]]:
    out: List[Tuple[str,str]] = []
    kw = keyword.lower().strip()
    for url in AIR_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:20]:
                title, link, summary = _normalize_entry(e)
                text = f"{title} {summary}".lower()
                if kw and kw not in text:
                    continue
                out.append((title, link))
        except Exception:
            log.exception("rss error: %s", url)
    # unik
    seen=set(); uniq=[]
    for t,l in out:
        if l and l not in seen:
            uniq.append((t,l)); seen.add(l)
    return uniq[:limit]

# ====== Regex intent tanpa slash ======
PRICE_TEXT = re.compile(r"^(harga|price)\s+([a-z0-9,]+)(?:\s+([a-z]{2,6}))?$", re.I)
CONVERT_TEXT = re.compile(r"^(convert|konversi)\s+([\d\.,]+)\s+([a-z0-9]+)\s+([a-z]{2,6})$", re.I)
PAIR_ONLY   = re.compile(r"^([a-z0-9]+)[/ ]([a-z]{2,6})$", re.I)

# ===== Command handlers =====
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("📊 Market", callback_data="menu_top")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di AirdropCore Bot!\nKetik: harga btc usdt | convert 0.25 btc idr | /help",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah & teks natural:\n"
        "• /price <sym> [fiat]  atau  harga btc usdt\n"
        "• /prices btc,eth [fiat]  atau  harga btc,eth idr\n"
        "• /convert <amt> <sym> <fiat>  atau  convert 0.25 btc idr\n"
        "• /setfiat idr|usd|usdt|eur\n"
        "• /airdrops [keyword], /hunt <keyword>\n"
        "• /top [n], /dominance, /fear, /gas\n"
        "• /ask <pertanyaan> (AI)"
    )

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: {FIAT_DEFAULT.upper()}\nFormat: /setfiat idr|usd|usdt|eur"
        ); return
    fiat = ctx.args[0].lower()
    if fiat not in {"idr","usd","usdt","eur"}:
        await update.message.reply_text("❌ Fiat tidak valid."); return
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default: {fiat.upper()}")

async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>"); return
    if not client:
        await update.message.reply_text("❌ OPENAI_API_KEY belum diatur."); return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=400, temperature=0.45
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("AI error"); await update.message.reply_text(f"❌ Error AI: {e}")

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]"); return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat)

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth [fiat]"); return
    syms_part = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    syms = [s.strip() for s in syms_part.split(",") if s.strip()]
    await reply_prices(update, syms, fiat)

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args)<3:
        await update.message.reply_text("Format: /convert <amt> <sym> <fiat>"); return
    amt = ctx.args[0]; sym = ctx.args[1]; fiat = ctx.args[2].lower()
    await reply_convert(update, amt, sym, fiat)

# ---- Market extras ----
async def top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = int(ctx.args[0]) if ctx.args else 10
    url = "https://api.coingecko.com/api/v3/coins/markets"
    try:
        r = requests.get(url, params={"vs_currency": FIAT_DEFAULT,
                                      "order":"market_cap_desc",
                                      "per_page": n, "page": 1}, timeout=20)
        r.raise_for_status()
        data = r.json()
        lines = [f"{i+1}. {c['symbol'].upper()} = {fmt_price(c['current_price'], FIAT_DEFAULT)}"
                 for i, c in enumerate(data)]
        await update.message.reply_text("🏆 Top Coins:\n" + "\n".join(lines))
    except Exception:
        log.exception("top error"); await update.message.reply_text("❌ Gagal ambil data top coins")

async def dominance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=20); r.raise_for_status()
        d = r.json()["data"]["market_cap_percentage"]
        await update.message.reply_text(f"📊 Dominance:\nBTC: {d['btc']:.2f}%\nETH: {d['eth']:.2f}%")
    except Exception:
        log.exception("dom error"); await update.message.reply_text("❌ Gagal ambil dominasi.")

async def fear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=20); r.raise_for_status()
        v = r.json()["data"][0]
        await update.message.reply_text(f"😨 Fear & Greed Index: {v['value']} ({v['value_classification']})")
    except Exception:
        log.exception("fng error"); await update.message.reply_text("❌ Gagal ambil F&G.")

async def gas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get("https://owlracle.info/eth/gas", timeout=20)
        if r.ok and isinstance(r.json(), dict):
            g = r.json()
            await update.message.reply_text(
                f"⛽ Gas (ETH): Low {g.get('safe','?')} | Avg {g.get('normal','?')} | High {g.get('rapid','?')} gwei"
            ); return
    except Exception:
        pass
    await update.message.reply_text("⚠️ Gas endpoint tidak tersedia saat ini.")

# ---- Airdrops ----
async def airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kw = " ".join(ctx.args).strip()
    rows = fetch_airdrops(kw, limit=10)
    if not rows:
        msg = "Belum ada listing airdrop." if not kw else f"Tidak ada airdrop cocok '{kw}'."
        await update.message.reply_text(msg); return
    lines = [f"• {t}\n  {l}" for t,l in rows]
    head = "🎁 Airdrops terbaru" + (f" (filter: {kw})" if kw else "")
    await update.message.reply_text(head + ":\n" + "\n".join(lines))

async def hunt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /hunt <keyword>"); return
    kw = " ".join(ctx.args).strip()
    rows = fetch_airdrops(kw, limit=10)
    if not rows:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{kw}'."); return
    lines = [f"• {t}\n  {l}" for t,l in rows]
    await update.message.reply_text("🔎 Hasil pencarian:\n" + "\n".join(lines))

# ===== Helpers balasan harga =====
async def reply_price(update: Update, sym: str, fiat: str):
    try:
        cid = resolve_coin_id(sym)
        data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]:
            await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan."); return
        p = data[cid][fiat]
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(p, fiat)}{chg_txt}")
    except Exception as e:
        log.exception("price error"); await update.message.reply_text(f"❌ Error harga: {e}")

async def reply_prices(update: Update, syms: List[str], fiat: str):
    ids = [resolve_coin_id(s) for s in syms]
    data = fetch_price(ids, fiat)
    lines=[]
    for s,cid in zip(syms, ids):
        if cid in data and fiat in data[cid]:
            p = data[cid][fiat]
            chg = data[cid].get(f"{fiat}_24h_change")
            chg_txt = f" ({chg:+.2f}%/24h)" if isinstance(chg,(int,float)) else ""
            lines.append(f"• {s.upper()} = {fmt_price(p,fiat)}{chg_txt}")
        else:
            lines.append(f"• {s.upper()} = ❌ not found")
    await update.message.reply_text("\n".join(lines))

async def reply_convert(update: Update, amt_str: str, sym: str, fiat: str):
    try:
        amt = float(amt_str.replace(",",""))
        cid = resolve_coin_id(sym)
        data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]:
            await update.message.reply_text("❌ Pair tidak ditemukan."); return
        total = amt * float(data[cid][fiat])
        await update.message.reply_text(f"{amt:g} {sym.upper()} ≈ {fmt_price(total, fiat)}")
    except Exception as e:
        await update.message.reply_text(f"❌ Format salah: {e}")

# ===== Menu callback =====
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = ("Contoh:\n"
               "• harga btc usdt\n"
               "• harga btc,eth idr\n"
               "• convert 0.25 btc idr")
    elif data == "menu_top":
        txt = ("• /top 10\n• /dominance\n• /fear\n• /gas")
    elif data == "menu_air":
        txt = ("• /airdrops\n• /airdrops zk\n• /hunt monad")
    else:
        txt = "• /ask pertanyaan apa saja"
    await q.edit_message_text(txt)

# ===== Router teks (tanpa slash) =====
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # convert/konversi …
    m = CONVERT_TEXT.match(text)
    if m:
        _, amt, sym, fiat = m.groups()
        await reply_convert(update, amt, sym, fiat.lower()); return

    # harga / price single atau list
    m = PRICE_TEXT.match(text)
    if m:
        _, syms_part, fiat_opt = m.groups()
        fiat = (fiat_opt or FIAT_DEFAULT).lower()
        if "," in syms_part:
            syms = [s.strip() for s in syms_part.split(",") if s.strip()]
            await reply_prices(update, syms, fiat)
        else:
            await reply_price(update, syms_part, fiat)
        return

    # pasangan sederhana: "btc/usdt" atau "btc usdt"
    m = PAIR_ONLY.match(text)
    if m:
        sym, fiat = m.groups()
        await reply_price(update, sym, fiat.lower()); return

    # fallback ke AI
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": text}],
                max_tokens=220, temperature=0.6
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer)
        except Exception as e:
            log.exception("AI fallback"); await update.message.reply_text(f"❌ Error: {e}")

# ===== Runner =====
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("dominance", dominance))
    app.add_handler(CommandHandler("fear", fear))
    app.add_handler(CommandHandler("gas", gas))
    app.add_handler(CommandHandler("airdrops", airdrops))
    app.add_handler(CommandHandler("hunt", hunt))
    # menu & text
    app.add_handler(CallbackQueryHandler(on_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

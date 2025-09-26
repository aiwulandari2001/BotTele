# bot.py — AirdropCore Super Bot (Crypto + Airdrops + AI)
import os, re, time, math, logging, json, html
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# =====================
# ENV & OpenAI (opsional)
# =====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        client = None

# =====================
# Logging
# =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# =====================
# Regex untuk input bebas (harga/convert)
# =====================
RX_AMOUNT_PAIR = re.compile(r"^\s*([\d\.,]+)\s+([a-z0-9\-]+)\s+([a-z]{2,6})\s*$", re.I)   # "0.1 btc idr"
RX_PAIR_ONLY   = re.compile(r"^\s*([a-z0-9\-]{2,15})[/\s]+([a-z]{2,6})\s*$", re.I)        # "btc/usdt" / "btc usd"
RX_PRICE_WORD  = re.compile(r"^(?:harga|price)\s+([a-z0-9\-]{2,15})(?:[/\s]+([a-z]{2,6}))?$", re.I)

# =====================
# Resolver simbol → CoinGecko ID (ribuan koin)
# =====================
CG_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
CG_SIMPLE_PRICE = "https://api.coingecko.com/api/v3/simple/price"
UA = {"User-Agent": "Mozilla/5.0 (AirdropCoreBot)"}

_coin_cache: Dict[str, str] = {}     # symbol -> id
_coin_cache_time: float = 0.0
COIN_CACHE_TTL = 24 * 3600  # 24 jam

STATIC_MAP = {
    "btc":"bitcoin","xbt":"bitcoin",
    "eth":"ethereum",
    "usdt":"tether",
    "usdc":"usd-coin","usdc.e":"usd-coin",
    "bnb":"binancecoin",
    "sol":"solana",
    "ada":"cardano",
    "xrp":"ripple",
    "dot":"polkadot",
    "doge":"dogecoin",
    "trx":"tron",
    "matic":"polygon",
    "ton":"the-open-network",
    "arb":"arbitrum","op":"optimism","avax":"avalanche-2",
    "link":"chainlink","uni":"uniswap","inj":"injective-protocol",
    # Tambah override di sini bila perlu, contoh:
    # "pi":"pi-network",
}

def _refresh_coin_cache(force: bool=False) -> None:
    global _coin_cache, _coin_cache_time
    now = time.time()
    if _coin_cache and not force and (now - _coin_cache_time) < COIN_CACHE_TTL:
        return
    try:
        r = requests.get(CG_LIST_URL, headers=UA, timeout=30)
        r.raise_for_status()
        coins = r.json()
        cache = {}
        for c in coins:
            sym = (c.get("symbol") or "").lower()
            cid = c.get("id")
            if not sym or not cid:
                continue
            cache.setdefault(sym, cid)
        for k, v in STATIC_MAP.items():  # alias prioritas
            cache[k] = v
        _coin_cache = cache
        _coin_cache_time = now
        log.info("Coin cache terisi: %d symbol", len(_coin_cache))
    except Exception:
        log.exception("Gagal refresh coins list")

def get_coin_id(symbol: str) -> str:
    if not symbol: return ""
    s = symbol.lower()
    if s in STATIC_MAP: return STATIC_MAP[s]
    _refresh_coin_cache()
    return _coin_cache.get(s, s)

def fetch_price(ids: List[str], fiat: str="usd") -> Dict:
    if not ids: return {}
    try:
        r = requests.get(
            CG_SIMPLE_PRICE,
            params={"ids": ",".join(ids), "vs_currencies": fiat, "include_24hr_change": "true"},
            headers=UA, timeout=25
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("fetch_price error")
        return {}

def fmt_price(val: float, fiat: str) -> str:
    try:
        v = float(val)
    except Exception:
        return f"{val} {fiat.upper()}"
    if v == 0: return f"0 {fiat.upper()}"
    if v < 1:
        digits = max(2, min(8, -int(math.floor(math.log10(v)))+2))
        return f"{v:.{digits}f} {fiat.upper()}"
    return f"{v:,.2f} {fiat.upper()}"

# =====================
# AIRDROP SCRAPER
# =====================
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: str = ""
    reward: str = ""
    link: str = ""
    status: str = ""       # ongoing/upcoming/ended
    tasks: List[str] = field(default_factory=list)
    source: str = ""       # airdrops.io / cryptorank

CACHE_FILE = "airdrops_cache.json"
CACHE_TTL  = 6*3600

def _load_cache() -> Tuple[float, List[Dict]]:
    try:
        if not os.path.exists(CACHE_FILE): return 0, []
        data = json.load(open(CACHE_FILE, "r", encoding="utf-8"))
        return data.get("time",0), data.get("items",[])
    except Exception:
        return 0, []

def _save_cache(items: List[Dict]) -> None:
    try:
        json.dump({"time": time.time(), "items": items}, open(CACHE_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass

def extract_tasks(html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    tasks = []
    for ul in soup.select("ul, ol"):
        lis = [li.get_text(" ", strip=True) for li in ul.select("li")]
        if not lis: continue
        blob = " ".join(lis).lower()
        if any(k in blob for k in ["quest","galxe","zealy","discord","twitter","x.com","bridge","swap","mint","faucet","testnet","task"]):
            tasks.extend(lis); break
    if not tasks:
        paras = [p.get_text(" ", strip=True) for p in soup.select("p") if len(p.get_text(strip=True))>60]
        tasks = paras[:6]
    clean = []
    for t in tasks:
        t = re.sub(r"\s+"," ", t).replace("·","-").strip()
        if t and t not in clean: clean.append(t)
    return clean[:12]

def extract_meta(html_text: str) -> Dict[str,str]:
    soup = BeautifulSoup(html_text, "html.parser")
    txt = soup.get_text(" ", strip=True)
    meta = {}
    m = re.search(r"Chain\s*[:\-]\s*([A-Za-z0-9 \-_/]+)", txt, re.I)
    if m: meta["chain"] = m.group(1)[:50]
    m = re.search(r"Reward\s*[:\-]\s*([A-Za-z0-9 \$\.\,\+\-\(\)]+)", txt, re.I)
    if m: meta["reward"] = m.group(1)[:80]
    return meta

def dedupe(items: List[Airdrop]) -> List[Airdrop]:
    seen, out = set(), []
    for it in items:
        key = (it.name.lower(), it.link)
        if key in seen: continue
        seen.add(key); out.append(it)
    return out

def scrape_airdrops_io() -> List[Airdrop]:
    out: List[Airdrop] = []
    pages = [("https://airdrops.io/ongoing/","ongoing"), ("https://airdrops.io/upcoming/","upcoming")]
    for url, status in pages:
        try:
            r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("div.project, div.card, article, div.airdrop")
            if not cards: cards = soup.select("a[href*='/airdrop/'], a[href*='airdrops.io/']")
            for c in cards:
                a = c.select_one("a[href]")
                title = (a.get_text(strip=True) if a else c.get_text(" ", strip=True))[:120]
                href  = (a["href"] if a and a.has_attr("href") else "").strip()
                if href.startswith("/"): href = "https://airdrops.io"+href
                if not title or not href: continue
                slug = re.sub(r"[^a-z0-9]+","-", title.lower()).strip("-")
                item = Airdrop(slug=slug, name=title, link=href, status=status, source="airdrops.io")
                try:
                    rr = requests.get(href, headers=UA, timeout=25)
                    if rr.ok:
                        item.tasks = extract_tasks(rr.text) or item.tasks
                        meta = extract_meta(rr.text)
                        item.chain = meta.get("chain","") or item.chain
                        item.reward = meta.get("reward","") or item.reward
                except Exception:
                    pass
                out.append(item)
        except Exception:
            log.warning("Scrape airdrops.io gagal: %s", url, exc_info=True)
    return dedupe(out)

def scrape_cryptorank() -> List[Airdrop]:
    url = "https://cryptorank.io/airdrops"
    out: List[Airdrop] = []
    try:
        r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("a[href*='/airdrops/']")
        for a in rows:
            title = a.get_text(" ", strip=True)
            href  = a.get("href","")
            if href.startswith("/"): href = "https://cryptorank.io"+href
            if not title or not href: continue
            slug = re.sub(r"[^a-z0-9]+","-", title.lower()).strip("-")
            out.append(Airdrop(slug=slug, name=title, link=href, status="ongoing", source="cryptorank"))
    except Exception:
        log.warning("Scrape cryptorank gagal", exc_info=True)
    return dedupe(out)

def scrape_all(force: bool=False) -> List[Airdrop]:
    ts, cached = _load_cache()
    if cached and not force and (time.time()-ts) < CACHE_TTL:
        return [Airdrop(**c) for c in cached]
    items: List[Airdrop] = []
    items += scrape_airdrops_io()
    items += scrape_cryptorank()
    items = dedupe(items)
    _save_cache([i.__dict__ for i in items])
    return items

def fuzzy_find(items: List[Airdrop], q: str) -> Optional[Airdrop]:
    ql = q.lower().strip()
    if not ql: return None
    for a in items:
        if ql == a.slug or ql == a.name.lower() or ql in a.name.lower() or ql in a.slug:
            return a
    best, score = None, 0
    for a in items:
        s = a.name.lower()
        common = len(set(ql.split()) & set(s.split()))
        if common > score:
            best, score = a, common
    return best

# =====================
# UI / Handlers
# =====================
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔁 Convert", callback_data="menu_convert")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")]
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Selamat datang di *AirdropCore Bot*!\n"
        "• `btc usd`, `0.1 eth idr`\n"
        "• /price <coin> [fiat], /convert <amt> <coin> <fiat>\n"
        "• /airdrops [keyword], /airupdate (refresh)\n"
        "• /ask <pertanyaan> (AI)",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah:\n"
        "• /price <coin> [fiat]\n"
        "• /convert <amt> <coin> <fiat>\n"
        "• /airdrops [keyword]\n"
        "• /airupdate  (paksa refresh scraper)\n"
        "• /ask <pertanyaan>\n"
        f"FIAT default: {FIAT_DEFAULT.upper()}",
        disable_web_page_preview=True
    )

async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = "Contoh:\n• `/price btc usdt`\n• `btc usd`"
    elif data == "menu_convert":
        txt = "Contoh:\n• `/convert 0.25 eth idr`\n• `0.25 eth idr`"
    elif data == "menu_air":
        txt = ("Airdrop:\n• `/airdrops`\n• `/airdrops monad`\n• `/airupdate` untuk refresh")
    else:
        txt = "AI: `/ask <pertanyaan>`"
    await q.edit_message_text(txt, parse_mode="Markdown")

# ----- Harga & konversi -----
async def reply_price(update: Update, sym: str, fiat: str, amount: float=1.0):
    cid = get_coin_id(sym)
    data = fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan di provider utama.")
        return
    unit = float(data[cid][fiat])
    total = unit * float(amount)
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {float(chg):+.2f}%)" if isinstance(chg,(int,float)) else ""
    if amount and float(amount) != 1.0:
        await update.message.reply_text(
            f"🔁 {amount:g} {sym.upper()} ≈ {fmt_price(total, fiat)}\n"
            f"(1 {sym.upper()} = {fmt_price(unit, fiat)}){chg_txt}"
        )
    else:
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(unit, fiat)}{chg_txt}")

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <coin> [fiat]"); return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat, 1.0)

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amt> <coin> <fiat>"); return
    try:
        amt = float(str(ctx.args[0]).replace(",",""))
    except:
        await update.message.reply_text("❌ Format jumlah salah."); return
    sym, fiat = ctx.args[1], ctx.args[2].lower()
    await reply_price(update, sym, fiat, amt)

# ----- Airdrop -----
async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).strip().lower()
    items = scrape_all(force=False)
    if not q:
        lines = [f"• {a.name} — {a.status or a.source}" for a in items[:15]]
        await update.message.reply_text(
            "Airdrop terdeteksi (Top 15):\n" + "\n".join(lines) + "\n\nGunakan `/airdrops <keyword>` untuk detail.",
            parse_mode="Markdown", disable_web_page_preview=True
        )
        return
    a = fuzzy_find(items, q)
    if not a:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{q}'."); return
    txt = (f"🎁 *{a.name}*\n"
           f"🌐 Chain: {a.chain or '-'}\n"
           f"💰 Reward: {a.reward or '-'}\n"
           f"📊 Status: {a.status or '-'}\n"
           f"🔗 Sumber: {a.source}\n\n")
    if a.tasks:
        txt += "*Task:*\n" + "\n".join([f"• {html.escape(t)}" for t in a.tasks[:10]])
    else:
        txt += "_Task belum terdeteksi otomatis; buka link untuk detail._"
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("Buka halaman", url=a.link)]]
    ), parse_mode="Markdown", disable_web_page_preview=False)

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Memperbarui daftar airdrop…")
    items = scrape_all(force=True)
    await update.message.reply_text(f"✅ Selesai. Total item: {len(items)}")

# ----- AI -----
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>"); return
    if not client:
        await update.message.reply_text("❌ OPENAI_API_KEY belum diatur."); return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user", "content": prompt}],
            max_tokens=350, temperature=0.45
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

# ----- Router teks bebas -----
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()

    m = RX_AMOUNT_PAIR.match(t)      # "0.1 btc idr"
    if m:
        amt = float(m.group(1).replace(",",""))
        sym = m.group(2)
        fiat = m.group(3).lower()
        await reply_price(update, sym, fiat, amt); return

    m = RX_PAIR_ONLY.match(t)        # "btc/usdt" atau "btc usd"
    if m:
        sym, fiat = m.groups()
        await reply_price(update, sym, fiat.lower(), 1.0); return

    m = RX_PRICE_WORD.match(t)       # "harga btc usdt"
    if m:
        sym = m.group(1)
        fiat = (m.group(2) or FIAT_DEFAULT).lower()
        await reply_price(update, sym, fiat, 1.0); return

    # keyword airdrop di teks bebas
    if "airdrop" in t.lower():
        items = scrape_all(False)
        key = t.lower().replace("airdrop","").strip()
        if key:
            a = fuzzy_find(items, key)
            if a:
                txt = (f"🎁 {a.name}\nChain: {a.chain or '-'} | Reward: {a.reward or '-'}\n"
                       f"Status: {a.status or a.source}\nLink: {a.link}")
                await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Buka halaman", url=a.link)]]
                ))
                return
        await update.message.reply_text("Gunakan `/airdrops <keyword>` untuk detail.", parse_mode="Markdown"); return

    # fallback ke AI
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": t}],
                max_tokens=220, temperature=0.6
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer); return
        except Exception as e:
            log.warning("AI fallback error: %s", e)

    await update.message.reply_text("Tidak paham. Coba: `btc usd`, `0.1 eth idr`, atau `/airdrops`.", parse_mode="Markdown")

# =====================
# Main
# =====================
def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN belum diisi di .env")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    # Menu
    app.add_handler(CallbackQueryHandler(menu_cb))
    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling berjalan…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

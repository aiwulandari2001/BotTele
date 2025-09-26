# AirdropCore Super Bot — env-based config
import os, re, json, time, math, logging, html
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ==== ENV ====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

# ==== OpenAI (opsional) ====
client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        logging.getLogger("airdropcore.bot").info("OpenAI client aktif")
except Exception:
    client = None

# ==== Logging ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("airdropcore.bot")

# -----------------------------------------------------------------------------
# KRIPTO
# -----------------------------------------------------------------------------
CG_BASE = "https://api.coingecko.com/api/v3"
SYMBOL_MAP = {
    "btc":"bitcoin","eth":"ethereum","bnb":"binancecoin","sol":"solana",
    "ada":"cardano","xrp":"ripple","dot":"polkadot","doge":"dogecoin",
    "trx":"tron","matic":"polygon","ton":"the-open-network","arb":"arbitrum",
    "op":"optimism","avax":"avalanche-2","atom":"cosmos","near":"near",
    "sui":"sui","kas":"kaspa","link":"chainlink","uni":"uniswap","inj":"injective-protocol",
    "usdt":"tether","usdc":"usd-coin","dai":"dai","busd":"binance-usd",
}

def norm_symbol(sym: str) -> str:
    return SYMBOL_MAP.get((sym or "").lower(), (sym or "").lower())

def fetch_price(ids: List[str], fiat: str = "usd") -> Dict:
    if not ids:
        return {}
    try:
        r = requests.get(
            f"{CG_BASE}/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": fiat, "include_24hr_change": "true"},
            timeout=20,
            headers={"User-Agent":"Mozilla/5.0 AirdropCoreBot"}
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("fetch_price error")
        return {}

def fmt_price(val: float, fiat: str) -> str:
    if val is None: return f"n/a {fiat.upper()}"
    try:
        v = float(val)
    except Exception:
        return f"{val} {fiat.upper()}"
    if v == 0:
        return f"0 {fiat.upper()}"
    if v < 1:
        # dinamis untuk koin mikro
        digits = max(2, min(8, -int(math.floor(math.log10(v)))+2))
        return f"{v:.{digits}f} {fiat.upper()}"
    return f"{v:,.2f} {fiat.upper()}"

# Pola teks bebas
RX_AMOUNT_PAIR = re.compile(r"^\s*([\d\.,]+)\s+([a-z0-9\-]+)\s+([a-z]{2,6})\s*$", re.I)
RX_PAIR_ONLY   = re.compile(r"^\s*([a-z0-9\-]+)[/\s]+([a-z]{2,6})\s*$", re.I)
RX_PRICE_WORD  = re.compile(r"^(?:harga|price)\s+([a-z0-9,/\s]+?)(?:\s+([a-z]{2,6}))?$", re.I)
RX_TICKER_ONLY = re.compile(r"^[a-z0-9\-]{2,10}$", re.I)

# -----------------------------------------------------------------------------
# AIRDROP SCRAPER (airdrops.io + cryptorank)
# -----------------------------------------------------------------------------
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
UA_HDR     = {"User-Agent": "Mozilla/5.0 (AirdropCoreBot)"}

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
            tasks.extend(lis)
            break
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
            r = requests.get(url, headers=UA_HDR, timeout=25); r.raise_for_status()
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
                    rr = requests.get(href, headers=UA_HDR, timeout=25)
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
        r = requests.get(url, headers=UA_HDR, timeout=25); r.raise_for_status()
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

# -----------------------------------------------------------------------------
# TELEGRAM HANDLERS
# -----------------------------------------------------------------------------
def kb_link(url: str, text: str="Buka halaman"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=url)]])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔁 Convert", callback_data="menu_convert")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di *AirdropCore Bot*!\n"
        "• Ketik: `btc usd`, `0.1 eth idr`\n"
        "• /price <coin> [fiat], /convert <amt> <coin> <fiat>\n"
        "• /airdrops [keyword], /airupdate (refresh)\n"
        "• /ask <pertanyaan> (AI)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah:\n"
        "• /price <coin> [fiat]\n"
        "• /convert <amt> <coin> <fiat>\n"
        "• /airdrops [keyword]\n"
        "• /airupdate  (paksa refresh scraper)\n"
        "• /ask <pertanyaan>\n"
        "Contoh tanpa slash juga bisa: `0.25 btc idr`, `eth usd`.",
        disable_web_page_preview=True
    )

# ---- Harga / Convert ----
async def reply_price(update: Update, sym: str, fiat: str):
    cid = norm_symbol(sym)
    data = fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan."); return
    price = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {float(chg):+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price, fiat)}{chg_txt}")

async def reply_convert(update: Update, amount: float, sym: str, fiat: str):
    cid = norm_symbol(sym)
    data = fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan."); return
    price = float(data[cid][fiat])
    total = amount * price
    await update.message.reply_text(f"🔁 {amount:g} {sym.upper()} ≈ {fmt_price(total, fiat)}\n(1 {sym.upper()} = {fmt_price(price, fiat)})")

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]"); return
    sym = ctx.args[0]; fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat)

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args)<3:
        await update.message.reply_text("Format: /convert <amt> <coin> <fiat>"); return
    try:
        amt = float(str(ctx.args[0]).replace(",",""))
    except: 
        await update.message.reply_text("Format angka salah."); return
    sym, fiat = ctx.args[1], ctx.args[2].lower()
    await reply_convert(update, amt, sym, fiat)

# ---- Airdrop ----
async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).strip().lower()
    items = scrape_all(force=False)
    if not q:
        lines = [f"• {a.name} — {a.status or a.source}" for a in items[:15]]
        await update.message.reply_text("Airdrop terdeteksi (Top 15):\n" + "\n".join(lines) + "\n\nGunakan `/airdrops <keyword>` untuk detail.", parse_mode="Markdown")
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
    await update.message.reply_text(txt, reply_markup=kb_link(a.link, "Buka halaman"), parse_mode="Markdown")

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Memperbarui daftar airdrop…")
    items = scrape_all(force=True)
    await update.message.reply_text(f"✅ Selesai. Total item: {len(items)}")

# ---- AI ----
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>"); return
    if not client:
        await update.message.reply_text("❌ OPENAI_API_KEY belum diatur / modul tidak tersedia."); return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=350, temperature=0.45
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("AI error"); await update.message.reply_text(f"❌ Error AI: {e}")

# ---- MENU CALLBACK ----
async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = ("Contoh:\n• `/price btc usdt`\n• Ketik bebas: `btc usd`\n• `0.25 eth idr`")
    elif data == "menu_convert":
        txt = ("Contoh:\n• `/convert 1.2 sol usdt`\n• `0.5 btc idr`")
    elif data == "menu_air":
        txt = ("Airdrop:\n• `/airdrops`\n• `/airdrops monad`\n• `/airupdate` untuk refresh")
    else:
        txt = "AI: `/ask <pertanyaan>`"
    await q.edit_message_text(txt, parse_mode="Markdown")

# ---- ROUTER TEKS TANPA SLASH ----
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()

    m = RX_AMOUNT_PAIR.match(t)       # "0.1 btc idr"
    if m:
        amt = float(m.group(1).replace(",","")); sym = m.group(2); fiat = m.group(3).lower()
        await reply_convert(update, amt, sym, fiat); return

    m = RX_PRICE_WORD.match(t)        # "harga btc usdt" / "price btc,eth idr"
    if m:
        syms_part, fiat_opt = m.groups()
        fiat = (fiat_opt or FIAT_DEFAULT).lower()
        syms_part = syms_part.replace("/", " ").replace("  "," ")
        syms = []
        if "," in syms_part:
            syms = [s.strip() for s in syms_part.split(",") if s.strip()]
        else:
            parts = syms_part.split()
            if parts: syms = [parts[-1]]
        if len(syms) > 1:
            ids = [norm_symbol(s) for s in syms]; data = fetch_price(ids, fiat)
            if not data: await update.message.reply_text("❌ Tidak ada data."); return
            lines=[]
            for s,cid in zip(syms, ids):
                if cid in data and fiat in data[cid]:
                    p = data[cid][fiat]; chg = data[cid].get(f"{fiat}_24h_change")
                    chg_txt = f" ({float(chg):+.2f}%/24h)" if isinstance(chg,(int,float)) else ""
                    lines.append(f"• {s.upper():<6} {fmt_price(p,fiat)}{chg_txt}")
                else:
                    lines.append(f"• {s.upper():<6} n/a")
            await update.message.reply_text("📊 Harga:\n" + "\n".join(lines)); return
        else:
            await reply_price(update, syms[0], fiat); return

    m = RX_PAIR_ONLY.match(t)         # "btc/usdt" atau "btc usdt"
    if m:
        sym, fiat = m.groups(); await reply_price(update, sym, fiat.lower()); return

    if RX_TICKER_ONLY.match(t):       # "btc"
        await reply_price(update, t, FIAT_DEFAULT); return

    if "airdrop" in t.lower():        # minta airdrop
        items = scrape_all(False)
        key = t.lower().replace("airdrop","").strip()
        if key:
            a = fuzzy_find(items, key)
            if a:
                txt = (f"🎁 {a.name}\nChain: {a.chain or '-'} | Reward: {a.reward or '-'}\n"
                       f"Status: {a.status or a.source}\nLink: {a.link}")
                await update.message.reply_text(txt, reply_markup=kb_link(a.link, "Buka halaman")); return
        await update.message.reply_text("Gunakan `/airdrops <keyword>` untuk detail.", parse_mode="Markdown"); return

    if client:                        # fallback AI
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

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
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

    # Menu & text
    app.add_handler(CallbackQueryHandler(menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling berjalan…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

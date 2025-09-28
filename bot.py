# bot.py
import os, re, time, math, logging, json
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
import asyncio
import httpx
import requests
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

# ============== Konfigurasi dasar ==============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# ============== OpenAI (opsional) ==============
_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client aktif")
    except Exception as e:
        log.warning("OpenAI init gagal: %s", e)

# ============== Cache & konstanta ==============
UA = {"User-Agent": "Mozilla/5.0 (AirdropCoreBot/1.0)"}
FIAT_DEFAULT = "usd"
SUPPORTED_FIATS: set[str] = set()
COIN_ID_BY_SYMBOL: Dict[str, str] = {}   # e.g. "btc"->"bitcoin"
LAST_SCRAPE_TS: float = 0.0

# Airdrop storage (in-memory)
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None

AIRDROPS: List[Airdrop] = []
SCRAPE_ERRORS: List[str] = []   # ring buffer kecil (max 20)

def _push_err(msg: str) -> None:
    global SCRAPE_ERRORS
    ts = time.strftime("%H:%M:%S")
    SCRAPE_ERRORS.append(f"[{ts}] {msg}")
    SCRAPE_ERRORS = SCRAPE_ERRORS[-20:]

# ============== Utilitas harga (CoinGecko) ==============
def _init_markets_cache():
    """Ambil daftar fiat & coin dari CoinGecko; abaikan error."""
    global SUPPORTED_FIATS, COIN_ID_BY_SYMBOL
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/supported_vs_currencies",
            timeout=20, headers=UA
        )
        if r.ok:
            SUPPORTED_FIATS = set(r.json())
    except Exception as e:
        log.warning("Gagal ambil supported_vs_currencies: %s", e)

    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=25, headers=UA)
        if r.ok:
            data = r.json()
            # build symbol map (first come wins)
            for it in data:
                sym = (it.get("symbol") or "").lower()
                cid = (it.get("id") or "").lower()
                if sym and cid and sym not in COIN_ID_BY_SYMBOL:
                    COIN_ID_BY_SYMBOL[sym] = cid
    except Exception as e:
        log.warning("Gagal ambil coins/list: %s", e)

_init_markets_cache()

def _norm_symbol(sym: str) -> Optional[str]:
    if not sym:
        return None
    s = sym.lower()
    return COIN_ID_BY_SYMBOL.get(s)

def _is_fiat_ok(fiat: str) -> bool:
    return fiat.lower() in SUPPORTED_FIATS if SUPPORTED_FIATS else fiat.lower() in {"usd","idr","eur","usdt","btc","eth"}

def _fmt_price(val: float, fiat: str) -> str:
    if fiat.lower() in {"idr"}:
        return f"Rp {val:,.0f}"
    return f"{val:,.4f} {fiat.upper()}"

def fetch_price(ids: List[str], fiat: str) -> Dict:
    if not ids:
        return {}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(ids),
                "vs_currencies": fiat,
                "include_24hr_change": "true"
            },
            timeout=20, headers=UA
        )
        if r.ok:
            return r.json()
    except Exception as e:
        log.warning("fetch_price error: %s", e)
    return {}

# ============== Parser teks kripto (bebas) ==============
PAIR_RX = re.compile(r"^\s*([a-z0-9]{2,12})(?:[ /:_-]([a-z0-9]{2,12}))?\s*$", re.I)
AMOUNT_RX = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s+([a-z0-9]{2,12})\s+([a-z0-9]{2,12})\s*$", re.I)

def parse_pair_or_amount(text: str) -> Tuple[Optional[Tuple[str,str]], Optional[Tuple[float,str,str]]]:
    """Kembalikan (pair, convert) — salah satu bisa None.
       pair=(coin,fiat), convert=(amount, coin, fiat)
    """
    if not text: return (None, None)
    m2 = AMOUNT_RX.match(text)
    if m2:
        amt = float(m2.group(1))
        coin = m2.group(2).lower()
        fiat = m2.group(3).lower()
        return (None, (amt, coin, fiat))
    m1 = PAIR_RX.match(text)
    if m1:
        coin = m1.group(1).lower()
        fiat = (m1.group(2) or "").lower()
        if fiat:
            return ((coin, fiat), None)
    return (None, None)

# ============== Scrapers Airdrop ==============
def _clean_text(s: Optional[str]) -> Optional[str]:
    if not s: return None
    return " ".join(s.split())

def scrape_airdrops_io(pages:int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    base = "https://airdrops.io/latest/page/{}/"
    for p in range(1, max(1,pages)+1):
        url = base.format(p)
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".airdrops-list article, .airdrops-list .item"):
            title_el = card.select_one(".title, h3, h2, a")
            name = _clean_text(title_el.get_text() if title_el else None)
            if not name: continue
            href = title_el.get("href") if title_el and title_el.has_attr("href") else url
            reward = _clean_text((card.select_one(".reward, .prize, .subtitle") or {}).get_text() if card.select_one(".reward, .prize, .subtitle") else None)
            chain = _clean_text((card.select_one(".chain, .platform") or {}).get_text() if card.select_one(".chain, .platform") else None)
            slug = name.lower().strip().replace(" ", "-")
            out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward, url=href, source="airdrops.io"))
    return out

def scrape_airdropalert(pages:int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    base = "https://airdropalert.com/new-airdrops?page={}"
    for p in range(1, max(1,pages)+1):
        url = base.format(p)
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 403:
            _push_err("airdropalert 403 (Forbidden) – mungkin butuh cookies/anti-bot.")
            break
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("article, .airdrop-card, .card, .list-item"):
            t = row.select_one("h2, h3, .title, a")
            name = _clean_text(t.get_text() if t else None)
            if not name: continue
            href = t.get("href") if t and t.has_attr("href") else url
            reward = _clean_text((row.select_one(".reward, .rewards, .badge") or {}).get_text() if row.select_one(".reward, .rewards, .badge") else None)
            chain  = _clean_text((row.select_one(".chain, .network") or {}).get_text() if row.select_one(".chain, .network") else None)
            slug = name.lower().strip().replace(" ", "-")
            out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward, url=href, source="airdropalert"))
    return out

def scrape_airdrops_fun(pages:int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    base = "https://airdrops.fun/?_page={}"
    for p in range(1, max(1,pages)+1):
        url = base.format(p)
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("article, .post, .card"):
            t = card.select_one("h2, h3, .title, a")
            name = _clean_text(t.get_text() if t else None)
            if not name: continue
            href = t.get("href") if t and t.has_attr("href") else url
            reward = _clean_text((card.select_one(".reward, .prize, .subtitle") or {}).get_text() if card.select_one(".reward, .prize, .subtitle") else None)
            chain  = _clean_text((card.select_one(".chain, .platform") or {}).get_text() if card.select_one(".chain, .platform") else None)
            slug = name.lower().strip().replace(" ", "-")
            out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward, url=href, source="airdrops.fun"))
    return out

def scrape_all(pages:int=1) -> Tuple[List[Airdrop], Dict[str,int]]:
    results: List[Airdrop] = []
    per_source: Dict[str,int] = {"airdrops.io":0,"airdropalert":0,"airdrops.fun":0}
    for name, fn in [
        ("airdrops.io", scrape_airdrops_io),
        ("airdropalert", scrape_airdropalert),
        ("airdrops.fun", scrape_airdrops_fun),
    ]:
        try:
            chunk = fn(pages)
            results.extend(chunk)
            per_source[name] = len(chunk)
        except requests.exceptions.RequestException as e:
            msg = f"scrape {name} gagal: {e.__class__.__name__}: {e}"
            log.warning(msg)
            _push_err(msg)
        except Exception as e:
            msg = f"scrape {name} runtime error: {e}"
            log.warning(msg)
            _push_err(msg)

    # dedupe by slug
    uniq: Dict[str,Airdrop] = {}
    for a in results:
        if a.slug not in uniq or (a.reward and not uniq[a.slug].reward):
            uniq[a.slug] = a
    return list(uniq.values()), per_source

# ============== Helpers Telegram ==============
def _air_kb(page:int, pages:int, kw:str="") -> InlineKeyboardMarkup:
    prev_cb = f"air:prev:{page}:{kw}"
    next_cb = f"air:next:{page}:{kw}"
    ref_cb  = f"air:refresh:{page}:{kw}"
    btns = [
        [InlineKeyboardButton("« Prev", callback_data=prev_cb),
         InlineKeyboardButton(f"{page}/{pages}", callback_data="air:nop"),
         InlineKeyboardButton("Next »", callback_data=next_cb)],
        [InlineKeyboardButton("🔄 Refresh", callback_data=ref_cb)]
    ]
    return InlineKeyboardMarkup(btns)

def _render_airdrops(items: List[Airdrop], page:int, page_size:int=6) -> Tuple[str,int]:
    total = len(items)
    pages = max(1, math.ceil(total/page_size))
    page = max(1, min(page, pages))
    i0 = (page-1)*page_size
    view = items[i0:i0+page_size]
    lines = [f"Airdrop terdeteksi: {total} item (hal {page}/{pages})\n"]
    for a in view:
        parts = [f"• <b>{a.name}</b>"]
        if a.reward: parts.append(f"— {a.reward}")
        if a.chain:  parts.append(f"({a.chain})")
        if a.url:    parts.append(f"\n  <a href=\"{a.url}\">{a.source or 'link'}</a>")
        lines.append(" ".join(parts))
    if not view:
        lines.append("Tidak ada data.")
    return "\n".join(lines), pages

# ============== Handlers ==============
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Selamat datang di <b>Airdrop CORE (AI)</b>.\n"
        "Perintah utama:\n"
        "• <code>/price &lt;coin&gt; [fiat]</code>\n"
        "• <code>/prices btc,eth idr</code>\n"
        "• <code>/convert 0.25 btc idr</code>\n"
        "• <code>/setfiat idr|usd|usdt|eur</code>\n"
        "• <code>/airupdate [pages] [force]</code>\n"
        "• <code>/airdrops [keyword]</code>, <code>/airnews</code>, <code>/airstatus</code>\n\n"
        "(AI juga bisa tanpa /ask — cukup ketik pertanyaan)."
    )
    await update.message.reply_html(txt)

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(f"FIAT saat ini: {FIAT_DEFAULT.upper()}\nFormat: /setfiat idr|usd|usdt|eur")
        return
    fiat = ctx.args[0].lower()
    if not _is_fiat_ok(fiat):
        await update.message.reply_text("❌ Fiat tidak valid.")
        return
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default diset ke {fiat.upper()}")

# ---- Harga & konversi
async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <coin> [fiat]")
        return
    coin = ctx.args[0].lower()
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()

    cid = _norm_symbol(coin)
    if not cid or not _is_fiat_ok(fiat):
        return  # silent ignore (hemat VPS)
    data = fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        return
    val = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_text(f"💰 {coin.upper()} = {_fmt_price(val, fiat)}{chg_txt}")

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth [fiat]")
        return
    coins = [s.strip().lower() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    ids = [c for c in ( _norm_symbol(c) for c in coins ) if c]
    if not ids or not _is_fiat_ok(fiat):
        return
    data = fetch_price(ids, fiat)
    if not data: return
    lines = []
    for c in coins:
        cid = _norm_symbol(c)
        if not cid or cid not in data or fiat not in data[cid]: 
            continue
        val = data[cid][fiat]
        lines.append(f"• {c.upper()} = {_fmt_price(val, fiat)}")
    if lines:
        await update.message.reply_text("\n".join(lines))

async def cmd_convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amount> <coin> <fiat>")
        return
    try:
        amt = float(ctx.args[0])
    except:
        return
    coin = ctx.args[1].lower()
    fiat = ctx.args[2].lower()
    cid = _norm_symbol(coin)
    if not cid or not _is_fiat_ok(fiat): 
        return
    data = fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        return
    val = float(data[cid][fiat]) * amt
    await update.message.reply_text(f"≈ {_fmt_price(val, fiat)}")

# ---- Scrape & daftar airdrop
async def cmd_airupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pages = 1
    force = False
    if ctx.args:
        try:
            pages = max(1, int(ctx.args[0]))
        except: pass
        if len(ctx.args) > 1 and ctx.args[1].lower() in {"1","true","force"}:
            force = True

    global AIRDROPS, LAST_SCRAPE_TS
    if (time.time() - LAST_SCRAPE_TS < 300) and not force:
        await update.message.reply_text("⏳ Terlalu sering. Coba beberapa menit lagi atau pakai /airupdate 1 force")
        return

    msg = await update.message.reply_text(f"🔄 Update airdrops (pages={pages}, force={force})…")
    items, per_source = await asyncio.get_event_loop().run_in_executor(None, scrape_all, pages)
    AIRDROPS = items
    LAST_SCRAPE_TS = time.time()

    lines = [f"✅ Selesai. Terkumpul {len(AIRDROPS)} airdrop.\nPer sumber:"]
    for k,v in per_source.items():
        lines.append(f"• {k}: {v}")
    if SCRAPE_ERRORS:
        lines.append("\n⚠️ Error terakhir:")
        lines.extend(" - "+e for e in SCRAPE_ERRORS[-3:])
    await msg.edit_text("\n".join(lines))

def _filter_airdrops(keyword: str) -> List[Airdrop]:
    if not keyword: return AIRDROPS
    kw = keyword.lower()
    res = []
    for a in AIRDROPS:
        blob = " ".join([
            (a.name or ""), (a.slug or ""), (a.chain or ""),
            (a.reward or ""), (a.source or "")
        ]).lower()
        if kw in blob:
            res.append(a)
    return res

async def cmd_airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kw = " ".join(ctx.args).strip() if ctx.args else ""
    page = 1
    items = _filter_airdrops(kw)
    text, pages = _render_airdrops(items, page)
    await update.message.reply_html(
        text, reply_markup=_air_kb(page, pages, kw), disable_web_page_preview=True
    )

async def air_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("air:"): return
    parts = data.split(":", 3)  # air:next:page:kw
    action = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    kw = parts[3] if len(parts) > 3 else ""
    items = _filter_airdrops(kw)
    _, pages = _render_airdrops(items, page)
    if action == "next": page = min(page+1, pages)
    elif action == "prev": page = max(page-1, 1)
    elif action == "refresh": pass
    text, pages = _render_airdrops(items, page)
    await q.edit_message_text(text, reply_markup=_air_kb(page, pages, kw), parse_mode="HTML", disable_web_page_preview=True)

async def cmd_airnews(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = AIRDROPS[:5]
    if not items:
        await update.message.reply_text("Belum ada data. Jalankan /airupdate dulu.")
        return
    lines = ["🆕 Airdrop terbaru:"]
    for a in items:
        lines.append(f"• {a.name} — {a.reward or '-'}\n  {a.url or ''}")
    await update.message.reply_text("\n".join(lines))

async def cmd_airstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    per: Dict[str,int] = {}
    for a in AIRDROPS:
        key = a.source or "other"
        per[key] = per.get(key, 0) + 1
    last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(LAST_SCRAPE_TS)) if LAST_SCRAPE_TS else "-"
    lines = [f"📈 Status Airdrop\nTotal: {len(AIRDROPS)}\nLast update: {last}\n"]
    for k,v in sorted(per.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"• {k}: {v}")
    await update.message.reply_text("\n".join(lines))

async def cmd_airdebug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not SCRAPE_ERRORS:
        await update.message.reply_text("Tidak ada error terbaru.")
        return
    lines = ["🛠 Debug (last):"]
    lines.extend(SCRAPE_ERRORS[-10:])
    await update.message.reply_text("\n".join(lines))

# ---- Router teks bebas: kripto / AI
AI_SYSTEM = (
    "You are AirdropCore Assistant. Answer concisely but with depth. "
    "When users ask for numbers or prices, never hallucinate; if uncertain, say you can’t fetch live price. "
    "Use well-structured bullets and short paragraphs, avoid oversimplified tone."
)

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    txt = update.message.text.strip()

    # 1) cek pola konversi "0.25 btc idr"
    pair, conv = parse_pair_or_amount(txt)
    if conv:
        amt, coin, fiat = conv
        cid = _norm_symbol(coin)
        if not cid or not _is_fiat_ok(fiat):
            return
        data = fetch_price([cid], fiat)
        if not data or cid not in data or fiat not in data[cid]:
            return
        val = float(data[cid][fiat]) * amt
        await update.message.reply_text(f"≈ {_fmt_price(val, fiat)}")
        return

    # 2) cek pola pair "btc usd"
    if pair:
        coin, fiat = pair
        fiat = fiat or FIAT_DEFAULT
        cid = _norm_symbol(coin)
        if not cid or not _is_fiat_ok(fiat):
            return
        data = fetch_price([cid], fiat)
        if not data or cid not in data or fiat not in data[cid]:
            return
        val = float(data[cid][fiat])
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        await update.message.reply_text(f"💰 {coin.upper()} = {_fmt_price(val, fiat)}{chg_txt}")
        return

    # 3) Fallback ke AI (jika ada API key)
    if _client:
        try:
            resp = _client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role":"system","content": AI_SYSTEM},
                    {"role":"user",  "content": txt}
                ],
                temperature=0.4,
                max_tokens=500,
            )
            ans = resp.choices[0].message.content.strip()
            if ans:
                await update.message.reply_text(ans)
        except Exception as e:
            log.warning("AI error: %s", e)

# ============== Main ==============
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN belum diisi (ENV)")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("convert", cmd_convert))

    app.add_handler(CommandHandler("airupdate", cmd_airupdate))
    app.add_handler(CommandHandler("airdrops", cmd_airdrops))
    app.add_handler(CallbackQueryHandler(air_cb, pattern=r"^air:"))
    app.add_handler(CommandHandler("airnews", cmd_airnews))
    app.add_handler(CommandHandler("airstatus", cmd_airstatus))
    app.add_handler(CommandHandler("airdebug", cmd_airdebug))

    # router teks bebas
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling start…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

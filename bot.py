#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, math, json, asyncio, logging, html
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("ultra_pro.bot")

# ========== OpenAI (opsional) ==========
_openai = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _openai = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI aktif")
    except Exception as e:
        log.warning("OpenAI init gagal: %s", e)

# ========== HTTP SESSION ==========
HTTP: ClientSession | None = None
UA = {
    "User-Agent": "Mozilla/5.0 (AirdropCoreUltraPro/1.0; +https://t.me/) ",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.7",
    "Connection": "keep-alive",
    "Referer": "https://www.google.com/",
}

async def http_get(url: str, **kw) -> aiohttp.ClientResponse:
    assert HTTP is not None
    headers = kw.pop("headers", {})
    resp = await HTTP.get(url, headers={**UA, **headers}, **kw)
    return resp

# ========== KRIPTO: simbol & harga ==========
SUPPORTED_FIATS: set[str] = set()
SYMBOL_MAP: Dict[str, str] = {}   # "btc" -> "bitcoin"
SYMBOL_LAST = 0.0
SYM_TTL = 6*3600  # 6 jam

PAIR_RX = re.compile(r"^\s*([a-z0-9]{2,12})(?:[ /:_-]([a-z0-9]{2,12}))?\s*$", re.I)
AMT_RX  = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s+([a-z0-9]{2,12})\s+([a-z0-9]{2,12})\s*$", re.I)

async def refresh_supported_markets(force: bool=False):
    global SUPPORTED_FIATS, SYMBOL_MAP, SYMBOL_LAST
    if not force and (time.time()-SYMBOL_LAST) < SYM_TTL and SYMBOL_MAP:
        return
    try:
        # vs_currencies
        r = await http_get("https://api.coingecko.com/api/v3/simple/supported_vs_currencies", timeout=20)
        if r.status == 200:
            SUPPORTED_FIATS = set(await r.json())
        else:
            SUPPORTED_FIATS = {"usd","idr","eur","usdt"}
        # coins/list
        r = await http_get("https://api.coingecko.com/api/v3/coins/list?include_platform=false", timeout=40)
        if r.status == 200:
            arr = await r.json()
            m: Dict[str,str] = {}
            for it in arr:
                sym = (it.get("symbol") or "").lower()
                cid = (it.get("id") or "").lower()
                name= (it.get("name") or "").lower()
                if sym and cid:
                    m.setdefault(sym, cid)
                    m.setdefault(name, cid)
                    m.setdefault(cid, cid)
            if m:
                SYMBOL_MAP = m
                SYMBOL_LAST = time.time()
                log.info("Loaded symbols: %d entries", len(SYMBOL_MAP))
    except Exception as e:
        log.warning("refresh_supported_markets error: %s", e)

def fiat_ok(f: str) -> bool:
    return f.lower() in SUPPORTED_FIATS if SUPPORTED_FIATS else f.lower() in {"usd","idr","eur","usdt"}

def coin_id(sym: str) -> Optional[str]:
    return SYMBOL_MAP.get(sym.lower().strip())

def fmt_price(val: float, fiat: str) -> str:
    if fiat.lower()=="idr":
        return f"Rp {val:,.0f}"
    return f"{val:,.4f} {fiat.upper()}"

async def fetch_price(ids: List[str], fiat: str) -> Dict:
    if not ids: return {}
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        q = {
            "ids": ",".join(ids),
            "vs_currencies": fiat,
            "include_24hr_change": "true"
        }
        r = await http_get(url, params=q, timeout=25)
        if r.status == 200:
            return await r.json()
    except Exception as e:
        log.warning("fetch_price error: %s", e)
    return {}

def parse_text_price(text: str) -> Tuple[Optional[Tuple[str,str]], Optional[Tuple[float,str,str]]]:
    m = AMT_RX.match(text or "")
    if m:
        return (None, (float(m.group(1)), m.group(2).lower(), m.group(3).lower()))
    m = PAIR_RX.match(text or "")
    if m and m.group(2):
        return ((m.group(1).lower(), m.group(2).lower()), None)
    return (None, None)

# ========== AIRDROP ==========
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None

AIRDROPS: List[Airdrop] = []
AIR_LAST = 0.0
SCRAPE_ERR_RING: List[str] = []
PER_PAGE = 6

def push_err(msg: str):
    ts = time.strftime("%H:%M:%S")
    SCRAPE_ERR_RING.append(f"[{ts}] {msg}")
    del SCRAPE_ERR_RING[:-20]

def clean(s: Optional[str]) -> Optional[str]:
    if not s: return None
    return re.sub(r"\s+", " ", s).strip() or None

async def scrape_airdrops_io(pages:int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    for p in range(1, max(1,pages)+1):
        url = f"https://airdrops.io/latest/page/{p}/"
        r = await http_get(url, timeout=30)
        if r.status != 200:
            push_err(f"airdrops.io HTTP {r.status} (page {p})"); break
        soup = BeautifulSoup(await r.text(), "html.parser")
        for card in soup.select(".airdrops-list article, .airdrops-list .item"):
            t = card.select_one(".title, h3, h2, a")
            name = clean(t.get_text() if t else None)
            if not name: continue
            href = t.get("href") if t and t.has_attr("href") else url
            reward = clean((card.select_one(".reward, .prize, .subtitle") or {}).get_text() if card.select_one(".reward, .prize, .subtitle") else None)
            chain  = clean((card.select_one(".chain, .platform") or {}).get_text() if card.select_one(".chain, .platform") else None)
            slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
            out.append(Airdrop(slug, name, chain, reward, href, "airdrops.io"))
    return out

async def scrape_airdropalert(pages:int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    for p in range(1, max(1,pages)+1):
        url = f"https://airdropalert.com/new-airdrops?page={p}"
        r = await http_get(url, timeout=30)
        if r.status == 403:
            push_err("airdropalert 403 (Forbidden) – skip")
            break
        if r.status != 200:
            push_err(f"airdropalert HTTP {r.status} (page {p})"); break
        soup = BeautifulSoup(await r.text(), "html.parser")
        for row in soup.select("article, .airdrop-card, .card, .list-item"):
            t = row.select_one("h2, h3, .title, a")
            name = clean(t.get_text() if t else None)
            if not name: continue
            href = t.get("href") if t and t.has_attr("href") else url
            reward = clean((row.select_one(".reward, .rewards, .badge") or {}).get_text() if row.select_one(".reward, .rewards, .badge") else None)
            chain  = clean((row.select_one(".chain, .network") or {}).get_text() if row.select_one(".chain, .network") else None)
            slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
            out.append(Airdrop(slug, name, chain, reward, href, "airdropalert"))
    return out

async def scrape_all(pages:int=1) -> Tuple[List[Airdrop], Dict[str,int]]:
    results: List[Airdrop] = []
    per: Dict[str,int] = {"airdrops.io":0, "airdropalert":0}
    for name, fn in [("airdrops.io", scrape_airdrops_io), ("airdropalert", scrape_airdropalert)]:
        try:
            chunk = await fn(pages)
            results.extend(chunk)
            per[name] = len(chunk)
        except Exception as e:
            push_err(f"scrape {name} error: {e}")
    # dedupe by slug (prefer with reward)
    uniq: Dict[str,Airdrop] = {}
    for a in results:
        if a.slug not in uniq or (a.reward and not uniq[a.slug].reward):
            uniq[a.slug] = a
    out = list(uniq.values())
    return out, per

def filter_airdrops(keyword: str) -> List[Airdrop]:
    if not keyword: return AIRDROPS
    kw = keyword.lower()
    res = []
    for a in AIRDROPS:
        blob = " ".join([
            a.slug or "", a.name or "", a.chain or "",
            a.reward or "", a.url or "", a.source or ""
        ]).lower()
        if kw in blob: res.append(a)
    return res

def render_airdrops(items: List[Airdrop], page:int, size:int=PER_PAGE) -> Tuple[str,int]:
    total = len(items)
    pages = max(1, math.ceil(total/size))
    page = max(1, min(page, pages))
    i0 = (page-1)*size
    view = items[i0:i0+size]
    lines = [f"Airdrop: {total} item (hal {page}/{pages})\n"]
    for a in view:
        line = f"• <b>{html.escape(a.name)}</b>"
        if a.reward: line += f" — {html.escape(a.reward)}"
        if a.chain:  line += f" ({html.escape(a.chain)})"
        if a.url:    line += f"\n  <a href=\"{html.escape(a.url)}\">{html.escape(a.source or 'link')}</a>"
        lines.append(line)
    if not view: lines.append("Tidak ada data.")
    return "\n".join(lines), pages

def air_kb(page:int, pages:int, kw:str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Prev", callback_data=f"air:prev:{page}:{kw}"),
         InlineKeyboardButton(f"{page}/{pages}", callback_data="air:nop"),
         InlineKeyboardButton("Next »", callback_data=f"air:next:{page}:{kw}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"air:refresh:{page}:{kw}")]
    ])

# ========== RATE LIMIT (ringan) ==========
LAST_HIT: Dict[int, float] = {}
def too_fast(user_id: int, sec: float=0.8) -> bool:
    t = time.time()
    last = LAST_HIT.get(user_id, 0.0)
    if (t - last) < sec:
        return True
    LAST_HIT[user_id] = t
    return False

# ========== COMMANDS ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "👋 <b>AirdropCore Ultra Pro</b>\n"
        "• /menu — tombol cepat\n"
        "• /price <coin> [fiat]\n"
        "• /prices btc,eth [fiat]\n"
        "• /convert <amt> <coin> <fiat>\n"
        "• /airupdate [pages] [force]\n"
        "• /airdrops [keyword], /airnews, /airstatus, /airdebug\n"
        "• /about, /ping, /stats\n\n"
        "Ketik bebas: <code>btc idr</code> atau <code>0.25 sol usd</code>. "
        "Pertanyaan umum → AI (jika API Key aktif)."
    )
    await update.message.reply_html(txt)

async def menu_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Harga", callback_data="m:price"),
         InlineKeyboardButton("🔁 Convert", callback_data="m:conv")],
        [InlineKeyboardButton("🎁 Airdrops", callback_data="m:air"),
         InlineKeyboardButton("🤖 AI", callback_data="m:ai")],
        [InlineKeyboardButton("ℹ️ About", callback_data="m:about")]
    ])
    await update.message.reply_text("Pilih menu:", reply_markup=kb)

async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data or ""
    if data == "m:price":
        txt = "Contoh:\n• /price btc usdt\n• /prices btc,eth idr\n• Teks bebas: <code>btc idr</code>"
    elif data == "m:conv":
        txt = "Konversi:\n• /convert 0.25 btc idr\n• Teks bebas: <code>0.25 sol usd</code>"
    elif data == "m:air":
        txt = "Airdrops:\n• /airupdate • /airdrops [kw]\n• /airnews • /airstatus • /airdebug"
    elif data == "m:ai":
        txt = "Ketik pertanyaan apa saja (AI)."
    else:
        txt = "AirdropCore Ultra Pro — by AirdropCore.com"
    await q.edit_message_text(txt, parse_mode="HTML")

async def about_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>AirdropCore Ultra Pro</b>\n"
        "Fokus: intel airdrop + utilitas kripto + AI analitis.\n"
        "Optimized async I/O, cache simbol CoinGecko, dan pagination yang halus."
    )

async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Airdrops: {len(AIRDROPS)} | Symbols: {len(SYMBOL_MAP)} | Fiats: {len(SUPPORTED_FIATS)}"
    )

# ---- Harga & Konversi
async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <coin> [fiat]"); return
    coin = ctx.args[0].lower()
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    cid = coin_id(coin)
    if not cid or not fiat_ok(fiat): return
    data = await fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]: return
    val = float(data[cid][fiat]); chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_text(f"💰 {coin.upper()} = {fmt_price(val, fiat)}{chg_txt}")

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth [fiat]"); return
    coins = [s.strip().lower() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    ids = [c for c in (coin_id(c) for c in coins) if c]
    if not ids or not fiat_ok(fiat): return
    data = await fetch_price(ids, fiat)
    if not data: return
    lines=[]
    for c in coins:
        cid = coin_id(c)
        if cid and cid in data and fiat in data[cid]:
            lines.append(f"• {c.upper()} = {fmt_price(float(data[cid][fiat]), fiat)}")
    if lines: await update.message.reply_text("\n".join(lines))

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amt> <coin> <fiat>"); return
    try:
        amt = float(ctx.args[0])
    except:
        return
    coin = ctx.args[1].lower(); fiat = ctx.args[2].lower()
    cid = coin_id(coin)
    if not cid or not fiat_ok(fiat): return
    data = await fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]: return
    out = float(data[cid][fiat]) * amt
    await update.message.reply_text(f"≈ {fmt_price(out, fiat)}")

# ---- Airdrops
async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pages = 1; force = False
    if ctx.args:
        try: pages = max(1, int(ctx.args[0]))
        except: pass
        if len(ctx.args)>1 and ctx.args[1].lower() in {"1","true","force"}: force=True

    global AIRDROPS, AIR_LAST
    if (time.time()-AIR_LAST < 180) and not force:
        await update.message.reply_text("⏳ Terlalu sering. Tambahkan argumen 'force' untuk paksa.")
        return
    msg = await update.message.reply_text(f"🔄 Update airdrops (pages={pages}, force={force})…")
    new, per = await scrape_all(pages)
    AIRDROPS = new
    AIR_LAST = time.time()
    lines = [f"✅ Selesai. Terkumpul {len(AIRDROPS)} airdrop.", "Per sumber:"]
    for k,v in per.items(): lines.append(f"• {k}: {v}")
    if SCRAPE_ERR_RING:
        lines.append("\n⚠️ Error:")
        lines.extend(" - "+e for e in SCRAPE_ERR_RING[-4:])
    await msg.edit_text("\n".join(lines))

def air_kb_wrap(page:int, pages:int, kw:str) -> InlineKeyboardMarkup:
    return air_kb(page, pages, kw)

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kw = " ".join(ctx.args) if ctx.args else ""
    items = filter_airdrops(kw)
    text, pages = render_airdrops(items, 1)
    await update.message.reply_html(text, reply_markup=air_kb_wrap(1, pages, kw), disable_web_page_preview=True)

async def air_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data or ""
    if not data.startswith("air:"): return
    _, action, p, *rest = data.split(":", 3)
    page = int(p) if p.isdigit() else 1
    kw = rest[0] if rest else ""
    items = filter_airdrops(kw)
    _, pages = render_airdrops(items, page)
    if action=="next": page = min(page+1, pages)
    elif action=="prev": page = max(page-1, 1)
    # refresh → tetap page yang sama
    text, pages = render_airdrops(items, page)
    await q.edit_message_text(text, reply_markup=air_kb_wrap(page, pages, kw), parse_mode="HTML", disable_web_page_preview=True)

async def airnews_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = AIRDROPS[:5]
    if not items:
        await update.message.reply_text("Belum ada data. Jalankan /airupdate."); return
    lines = ["🆕 Airdrop terbaru:"]
    for a in items:
        lines.append(f"• {a.name} — {a.reward or '-'}\n  {a.url or ''}")
    await update.message.reply_text("\n".join(lines))

async def airstatus_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    per: Dict[str,int] = {}
    for a in AIRDROPS: per[a.source or "other"] = per.get(a.source or "other",0)+1
    last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(AIR_LAST)) if AIR_LAST else "-"
    lines = [f"📈 Total: {len(AIRDROPS)} | Last: {last}"]
    for k,v in sorted(per.items(), key=lambda x:(-x[1], x[0])):
        lines.append(f"• {k}: {v}")
    await update.message.reply_text("\n".join(lines))

async def airdebug_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not SCRAPE_ERR_RING:
        await update.message.reply_text("Tidak ada error terbaru."); return
    await update.message.reply_text("🛠 Debug:\n" + "\n".join(SCRAPE_ERR_RING[-12:]))

# ========== AI fallback (gaya “ultra pro”) ==========
AI_SYSTEM = (
    "You are AirdropCore Ultra Pro — a crisp, modern crypto & airdrop analyst. "
    "Write in Indonesian. Be structured, insightful, and pragmatic. "
    "Use bullets and short paragraphs. Avoid fluff. "
    "Never invent live prices; if unavailable, be transparent."
)

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if too_fast(user_id):
        return  # throttled

    txt = update.message.text.strip()

    # 1) harga konversi gaya bebas: "0.25 btc usd"
    pair, conv = parse_text_price(txt)
    if conv:
        amt, coin, fiat = conv
        cid = coin_id(coin)
        if not cid: return
        if not fiat_ok(fiat): return
        data = await fetch_price([cid], fiat)
        if not data or cid not in data or fiat not in data[cid]: return
        out = float(data[cid][fiat]) * amt
        await update.message.reply_text(f"≈ {fmt_price(out, fiat)}")
        return

    # 2) pair sederhana: "btc idr"
    if pair:
        coin, fiat = pair
        fiat = fiat or FIAT_DEFAULT
        cid = coin_id(coin)
        if not cid or not fiat_ok(fiat): return
        data = await fetch_price([cid], fiat)
        if not data or cid not in data or fiat not in data[cid]: return
        val = float(data[cid][fiat])
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        await update.message.reply_text(f"💰 {coin.upper()} = {fmt_price(val, fiat)}{chg_txt}")
        return

    # 3) fallback AI (opsional)
    if _openai:
        try:
            resp = _openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":AI_SYSTEM},{"role":"user","content":txt}],
                temperature=0.45, max_tokens=650
            )
            ans = resp.choices[0].message.content.strip()
            if ans:
                await update.message.reply_text(ans)
        except Exception as e:
            log.warning("AI error: %s", e)

# ========== MAIN ==========
async def _app_main():
    global HTTP
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN belum diisi (ENV)")

    # HTTP session
    HTTP = aiohttp.ClientSession(
        timeout=ClientTimeout(total=45),
        connector=TCPConnector(limit=30, ttl_dns_cache=300, ssl=False)
    )
    await refresh_supported_markets(force=True)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Core
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^m:"))

    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    # Crypto
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))

    # Airdrops
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CallbackQueryHandler(air_cb, pattern=r"^air:"))
    app.add_handler(CommandHandler("airnews", airnews_cmd))
    app.add_handler(CommandHandler("airstatus", airstatus_cmd))
    app.add_handler(CommandHandler("airdebug", airdebug_cmd))

    # Free text router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)

async def _cleanup():
    global HTTP
    if HTTP:
        await HTTP.close()
        HTTP = None

def main():
    try:
        asyncio.run(_app_main())
    finally:
        try:
            asyncio.run(_cleanup())
        except Exception:
            pass

if __name__ == "__main__":
    main()

# bot.py
import os, re, html, json, logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ================== ENV & LOGGING ==================
load_dotenv(override=True)

BOT_TOKEN       = (os.getenv("BOT_TOKEN") or "").strip()
OPENAI_API_KEY  = (os.getenv("OPENAI_API_KEY") or "").strip()
FIAT_DEFAULT    = (os.getenv("FIAT_DEFAULT") or "usd").lower()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# ================ (Opsional) OpenAI =================
client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI aktif")
    except Exception as e:
        log.warning("OpenAI init gagal: %s", e)

# ================== HTTP CONST =====================
UA = {
    "User-Agent":
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
TIMEOUT = 30

# ================== MARKET HELPERS =================
COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_TREND  = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_MARKET = "https://api.coingecko.com/api/v3/coins/markets"

SYMBOL_MAP = {
    "btc":"bitcoin","xbt":"bitcoin","eth":"ethereum","bnb":"binancecoin",
    "usdt":"tether","usdc":"usd-coin","sol":"solana","ada":"cardano",
    "xrp":"ripple","dot":"polkadot","doge":"dogecoin","trx":"tron",
    "matic":"polygon","ton":"the-open-network","arb":"arbitrum",
    "op":"optimism","bch":"bitcoin-cash","ltc":"litecoin","avax":"avalanche-2",
}

FREEFORM_CONV = re.compile(r"^\s*(\d*\.?\d+)\s+([a-z0-9]+)\s+([a-z0-9]+)\s*$", re.I)
PAIR_PATTERN   = re.compile(r"^([a-z0-9]{2,10})(?:[ /]([a-z0-9]{2,10}))?$", re.I)
PRICE_WORD     = re.compile(r"^[a-z0-9]{2,10}([/ ]+[a-z0-9]{2,10})?$", re.I)

def norm_symbol(sym: str) -> str:
    s = (sym or "").lower()
    return SYMBOL_MAP.get(s, s)

def fetch_price(symbol_ids: List[str], fiat: str="usd") -> Dict:
    try:
        r = requests.get(
            COINGECKO_SIMPLE,
            params={
                "ids": ",".join(symbol_ids),
                "vs_currencies": fiat,
                "include_24hr_change": "true",
            },
            headers=UA, timeout=TIMEOUT
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.exception("fetch_price error: %s", e)
        return {}

def fmt_price(val, fiat: str) -> str:
    try:
        v = float(val)
    except (TypeError, ValueError):
        raise RuntimeError("Harga tidak valid")
    no_decimal = {"idr","vnd","krw","jpy"}
    return (f"{v:,.0f}" if fiat in no_decimal else f"{v:,.4f}") + f" {fiat.upper()}"

def fetch_top(n: int=10, fiat: str="usd") -> List[Dict]:
    try:
        r = requests.get(COINGECKO_MARKET,
                         params={"vs_currency": fiat, "order": "market_cap_desc",
                                 "per_page": n, "page": 1, "sparkline": "false"},
                         headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("fetch_top gagal: %s", e)
        return []

def fetch_trending() -> List[Dict]:
    try:
        r = requests.get(COINGECKO_TREND, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        return [c.get("item", {}) for c in js.get("coins", [])]
    except Exception as e:
        log.warning("fetch_trending gagal: %s", e)
        return []

# ================== AIRDROP MODEL & STORE ==========
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None

AIRDROPS: List[Airdrop] = []
LAST_SLUGS: set = set()
NEW_SINCE_UPDATE: List[str] = []  # nama baru sejak update

JSON_FILE = "airdrops.json"

def load_airdrops_json() -> List[Airdrop]:
    if not os.path.exists(JSON_FILE):
        return []
    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Airdrop(**a) for a in data]
    except Exception as e:
        log.warning("Gagal load JSON: %s", e)
        return []

def save_airdrops_json(items: List[Airdrop]):
    try:
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump([a.__dict__ for a in items], f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Gagal simpan JSON: %s", e)

# ================== SCRAPER UTILS ================
def _clean(x: Optional[str]) -> Optional[str]:
    if not x: return None
    t = " ".join(x.split())
    return t or None

def _abs(base: str, href: Optional[str]) -> str:
    return urljoin(base, (href or "").strip())

def _render_node_with_links(base_url: str, node) -> str:
    if isinstance(node, NavigableString):
        return html.escape(str(node))
    if isinstance(node, Tag):
        if node.name == "a":
            txt = html.escape(node.get_text(strip=True) or node.get("href") or "link")
            href = _abs(base_url, node.get("href"))
            return f'<a href="{html.escape(href)}">{txt}</a>'
        return "".join(_render_node_with_links(base_url, ch) for ch in node.children)
    return ""

# -------- airdrops.io (latest) ----------
def scrape_airdrops_io(pages: int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    url = "https://airdrops.io/latest/"
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".airdrops-list .item"):
            title_el = card.select_one(".title, h2, h3, a")
            name = _clean(title_el.get_text() if title_el else None)
            if not name: continue
            href_el = card.select_one("a")
            href = _abs(url, href_el.get("href")) if href_el else url
            reward = _clean((card.select_one(".reward, .subtitle") or {}).get_text() if card.select_one(".reward, .subtitle") else None)
            chain  = _clean((card.select_one(".chain, .platform") or {}).get_text() if card.select_one(".chain, .platform") else None)
            slug = name.lower().replace(" ", "-")
            out.append(Airdrop(slug, name, chain, reward, href, "airdrops.io"))
    except Exception as e:
        log.warning("airdrops.io gagal: %s", e)
    return out

# -------- airdropalert.com ----------
def scrape_airdropalert(pages: int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    base = "https://airdropalert.com"
    url  = f"{base}/latest-airdrops"
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for it in soup.select("article, .card, .post, .list-item"):
            title_el = it.select_one("a, h2, h3")
            name = _clean(title_el.get_text() if title_el else None)
            if not name: continue
            href = _abs(base, title_el.get("href") if title_el else None)
            slug = name.lower().replace(" ", "-")
            out.append(Airdrop(slug, name, None, None, href, "airdropalert.com"))
    except Exception as e:
        log.warning("airdropalert gagal: %s", e)
    return out

# -------- airdropbob.com ----------
def scrape_airdropbob(pages: int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    base = "https://www.airdropbob.com"
    url  = f"{base}/latest"
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("article, .airdrop, .teaser, .card"):
            title_el = card.select_one("a, h2, h3")
            name = _clean(title_el.get_text() if title_el else None)
            if not name: continue
            href = _abs(base, title_el.get("href") if title_el else None)
            reward = _clean((card.select_one(".reward, .prize") or {}).get_text() if card.select_one(".reward, .prize") else None)
            chain  = _clean((card.select_one(".chain, .network") or {}).get_text() if card.select_one(".chain, .network") else None)
            slug = name.lower().replace(" ", "-")
            out.append(Airdrop(slug, name, chain, reward, href, "airdropbob.com"))
    except Exception as e:
        log.warning("airdropbob gagal: %s", e)
    return out

# -------- coincodex.com/airdrop ----------
def scrape_coincodex(pages: int=1) -> List[Airdrop]:
    out: List[Airdrop] = []
    base = "https://coincodex.com"
    url  = f"{base}/airdrop/"
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("table tr"):
            a = row.select_one("a[href*='/airdrop/']")
            name = _clean(a.get_text() if a else None)
            if not name: continue
            href = _abs(base, a.get("href"))
            slug = name.lower().replace(" ", "-")
            out.append(Airdrop(slug, name, None, None, href, "coincodex.com"))
    except Exception as e:
        log.warning("coincodex gagal: %s", e)
    return out

def scrape_airdrops_sync(pages:int=1) -> Tuple[List[Airdrop], Dict[str,int]]:
    results: List[Airdrop] = []
    per: Dict[str,int] = {}

    def add(src: str, items: List[Airdrop]):
        per[src] = per.get(src,0) + len(items)
        results.extend(items)

    add("airdrops.io",   scrape_airdrops_io(pages))
    add("airdropalert",  scrape_airdropalert(pages))
    add("airdropbob",    scrape_airdropbob(pages))
    add("coincodex",     scrape_coincodex(pages))

    # unik + prioritas yg punya reward
    mp: Dict[str, Airdrop] = {}
    for a in results:
        if (a.slug not in mp) or (a.reward and not mp[a.slug].reward):
            mp[a.slug] = a
    merged = list(mp.values())
    return merged, per

def scrape_airdrop_detail(url: str) -> Dict[str, List[str]]:
    r = requests.get(url, headers=UA, timeout=TIMEOUT); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    steps = []
    for li in soup.select(
        "article ol li, article ul li, "
        ".entry-content ol li, .entry-content ul li, "
        ".content ol li, .content ul li"
    ):
        s = _render_node_with_links(url, li).strip()
        s = s.lstrip("•-—0123456789. ").strip()
        if s and s not in steps:
            steps.append(s)
        if len(steps) >= 20: break

    links = []
    for a in soup.select("a[href]"):
        t = (a.get_text(strip=True) or "").lower()
        if any(k in t for k in ["join","claim","website","app","discord","telegram","x.com","twitter"]):
            href = _abs(url, a.get("href"))
            label = html.escape(a.get_text(strip=True) or href)
            links.append(f'• <a href="{html.escape(href)}">{label}</a>')
        if len(links) >= 8: break

    return {"steps": steps, "links": links}

def find_airdrop(slug: str) -> Optional[Airdrop]:
    s = slug.lower().strip()
    for a in AIRDROPS:
        if a.slug == s or a.slug in s or a.name.lower() == s:
            return a
    for a in AIRDROPS:
        if s in a.name.lower():
            return a
    return None

# ================== UI HELPERS =====================
AIR_PAGE_SIZE = 7

def _air_slice(page: int) -> List[Airdrop]:
    start = page * AIR_PAGE_SIZE
    return AIRDROPS[start:start+AIR_PAGE_SIZE]

def _air_keyboard(page: int, total: int):
    max_page = max(0, (total - 1) // AIR_PAGE_SIZE)
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"airlist:{page-1}"))
    if page < max_page:
        btns.append(InlineKeyboardButton("Next ➡️", callback_data=f"airlist:{page+1}"))
    return InlineKeyboardMarkup([btns]) if btns else None

# ================== HANDLERS =======================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>🤖 AirdropCore (AI)</b>\n"
        "• <b>/airupdate</b> (update daftar)\n"
        "• <b>/airdrops</b> (daftar & paging)\n"
        "• <b>/air</b> &lt;keyword&gt; (detail)\n"
        "• <b>/tugas</b> &lt;keyword&gt; (steps + link)\n\n"
        "💱 <b>Harga & Market</b>\n"
        "• <code>/price btc idr</code>\n"
        "• <code>/prices btc,eth usdt</code>\n"
        "• <code>/top 10 idr</code>, <code>/trend</code>\n"
        "• bebas: <code>0.25 btc idr</code>, <code>eth/idr</code>\n"
        "AI juga bisa chat bebas (tanpa /ask)."
    )
    await update.message.reply_html(txt, disable_web_page_preview=True)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: {FIAT_DEFAULT.upper()}\nFormat: /setfiat idr|usd|usdt|eur"
        )
        return
    fiat = ctx.args[0].lower()
    if fiat not in {"idr","usd","usdt","eur"}:
        await update.message.reply_text("❌ Fiat tidak valid."); return
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default diset ke {fiat.upper()}")

# ---- Harga ----
async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\ncontoh: /price btc usdt"); return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await _reply_price(update, sym, fiat)

async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth [fiat]"); return
    parts = [p.strip() for p in ctx.args[0].split(",") if p.strip()]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    ids = [norm_symbol(p) for p in parts]
    data = fetch_price(ids, fiat)
    lines = ["<b>📈 Harga</b>"]
    for orig, cid in zip(parts, ids):
        p = data.get(cid, {}).get(fiat)
        if p is None:  # skip yang tak ada
            continue
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {float(chg):+,.2f}%)" if isinstance(chg, (int, float)) else ""
        lines.append(f"• <b>{html.escape(orig.upper())}</b> = {fmt_price(p, fiat)}{chg_txt}")
    if len(lines) == 1:
        await update.message.reply_text("❌ Tidak ada pair yang tersedia.")
    else:
        await update.message.reply_html("\n".join(lines))

async def _reply_price(update: Update, sym: str, fiat: str):
    try:
        cid = norm_symbol(sym)
        data = fetch_price([cid], fiat)
        price_val = data.get(cid, {}).get(fiat)
        if price_val is None:
            await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan.")
            return
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {float(chg):+,.2f}%)" if isinstance(chg, (int, float)) else ""
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price_val, fiat)}{chg_txt}")
    except Exception as e:
        log.exception("price error"); await update.message.reply_text(f"❌ Error harga: {e}")

# ---- Market extra ----
async def top_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = 10
    fiat = FIAT_DEFAULT
    if ctx.args:
        if ctx.args[0].isdigit(): n = max(1, min(50, int(ctx.args[0])))
        if len(ctx.args) > 1: fiat = ctx.args[1].lower()
    data = fetch_top(n, fiat)
    if not data:
        await update.message.reply_text("❌ Gagal ambil data top."); return
    lines = [f"<b>🏆 Top {n} ({fiat.upper()})</b>"]
    for i, c in enumerate(data, 1):
        price = c.get("current_price")
        mc    = c.get("market_cap")
        mc_txt = f"{mc:,.0f}" if isinstance(mc, (int, float)) else "N/A"
        lines.append(f"{i}. <b>{html.escape(c.get('symbol','').upper())}</b> = {fmt_price(price, fiat)}  | MC: {mc_txt}")
    await update.message.reply_html("\n".join(lines))

async def trend_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fiat = (ctx.args[0].lower() if ctx.args else FIAT_DEFAULT)
    t = fetch_trending()
    if not t:
        await update.message.reply_text("❌ Gagal ambil trending."); return
    ids = [c.get("id") for c in t if c.get("id")]
    prices = fetch_price(ids, fiat) if ids else {}
    lines = [f"<b>🔥 Trending ({fiat.upper()})</b>"]
    for c in t:
        sym = c.get("symbol","").upper()
        cid = c.get("id","")
        p   = prices.get(cid,{}).get(fiat)
        ptxt= fmt_price(p, fiat) if p is not None else "N/A"
        score = c.get("score")
        lines.append(f"• <b>{html.escape(sym)}</b> = {ptxt} (score {score})")
    await update.message.reply_html("\n".join(lines))

# ---- Airdrop ----
async def airupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pages = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 1
    await update.message.reply_text(f"🔄 Update airdrops (pages={pages})…")
    try:
        items, per = scrape_airdrops_sync(pages)
        global AIRDROPS, LAST_SLUGS, NEW_SINCE_UPDATE
        prev = LAST_SLUGS
        AIRDROPS = items
        LAST_SLUGS = {a.slug for a in AIRDROPS}
        NEW_SINCE_UPDATE = [a.name for a in AIRDROPS if a.slug not in prev]
        save_airdrops_json(AIRDROPS)
        lines = [f"✅ Selesai. Terkumpul {len(AIRDROPS)} airdrop.", "Per sumber:"]
        for k,v in per.items(): lines.append(f"• {k}: {v}")
        if NEW_SINCE_UPDATE:
            lines.append("\n🆕 Baru:")
            lines += [f"• {html.escape(n)}" for n in NEW_SINCE_UPDATE[:10]]
        await update.message.reply_html("\n".join(lines), disable_web_page_preview=True)
    except Exception as e:
        log.exception("airupdate error")
        await update.message.reply_text(f"❌ Gagal update: {e}")

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total = len(AIRDROPS)
    if total == 0:
        await update.message.reply_text("Belum ada data. Jalankan /airupdate dulu."); return
    page = 0
    chunk = _air_slice(page)
    lines = [f"<b>🎁 Airdrop (hal {page+1}/{max(1,(total-1)//AIR_PAGE_SIZE+1)})</b>"]
    for a in chunk:
        src = f' (<a href="{html.escape(a.url or "")}">{html.escape(a.source or "link")}</a>)' if a.url else ""
        lines.append(
            f"• <b>{html.escape(a.name)}</b>{src}\n"
            f"  <i>Reward:</i> {html.escape(a.reward or '-')}\n"
            f"  <i>Chain:</i> {html.escape(a.chain or '-')}"
        )
    kb = _air_keyboard(page, total)
    await update.message.reply_html("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)

async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data.startswith("airlist:"):
        page = int(data.split(":")[1])
        total = len(AIRDROPS)
        chunk = _air_slice(page)
        lines = [f"<b>🎁 Airdrop (hal {page+1}/{max(1,(total-1)//AIR_PAGE_SIZE+1)})</b>"]
        for a in chunk:
            src = f' (<a href="{html.escape(a.url or "")}">{html.escape(a.source or "link")}</a>)' if a.url else ""
            lines.append(
                f"• <b>{html.escape(a.name)}</b>{src}\n"
                f"  <i>Reward:</i> {html.escape(a.reward or '-')}\n"
                f"  <i>Chain:</i> {html.escape(a.chain or '-')}"
            )
        kb = _air_keyboard(page, total)
        await q.edit_message_text("\n".join(lines), parse_mode="HTML",
                                  reply_markup=kb, disable_web_page_preview=True)

async def air_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /air <keyword>"); return
    ad = find_airdrop(" ".join(ctx.args))
    if not ad:
        await update.message.reply_text("❌ Tidak ditemukan."); return
    await update.message.reply_html(
        f"<b>{html.escape(ad.name)}</b>\n"
        f"🎯 <i>Reward:</i> {html.escape(ad.reward or '-')}\n"
        f"⛓️ <i>Chain:</i> {html.escape(ad.chain or '-')}\n\n"
        f'➡️ <a href="{html.escape(ad.url or "")}">Buka halaman airdrop</a>',
        disable_web_page_preview=True
    )

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>"); return
    ad = find_airdrop(" ".join(ctx.args))
    if not ad:
        await update.message.reply_text("❌ Tidak ditemukan."); return
    try:
        d = scrape_airdrop_detail(ad.url or "")
        parts = [f"<b>{html.escape(ad.name)}</b> – <i>Tugas:</i>"]
        if d["steps"]:
            parts += [f"{i}. {s}" for i, s in enumerate(d["steps"], 1)]
        else:
            parts.append("• (Belum ada langkah spesifik)")
        if d["links"]:
            parts.append("\n🔗 <b>Link penting:</b>")
            parts += d["links"]
        parts.append(f'\n➡️ <a href="{html.escape(ad.url or "")}">Buka halaman airdrop</a>')
        await update.message.reply_html("\n".join(parts), disable_web_page_preview=True)
    except Exception as e:
        log.exception("tugas error"); await update.message.reply_text(f"❌ Gagal ambil detail: {e}")

# ---- Bebas: 0.25 btc idr / btc usdt / eth/idr / AI fallback ----
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    m = FREEFORM_CONV.match(text)
    if m:
        qty, sym, fiat = m.groups()
        cid = norm_symbol(sym); fiat = fiat.lower()
        data = fetch_price([cid], fiat)
        price_val = data.get(cid, {}).get(fiat)
        if price_val is None: return  # diam: hemat VPS
        total = float(qty) * float(price_val)
        await update.message.reply_text(f"≈ {fmt_price(total, fiat)}")
        return

    m = PAIR_PATTERN.match(text) if PRICE_WORD.match(text) else None
    if m:
        sym, fiat = m.groups()
        fiat = (fiat or FIAT_DEFAULT).lower()
        await _reply_price(update, sym, fiat)
        return

    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": text}],
                max_tokens=400, temperature=0.6
            )
            answer = resp.choices[0].message.content.strip()
            if answer:
                await update.message.reply_text(answer)
        except Exception as e:
            log.warning("AI fallback error: %s", e)
    # jika tak ada AI atau gagal → diam

# ================== APP ============================
def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN belum di isi di .env")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))

    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trend", trend_cmd))

    app.add_handler(CommandHandler("airupdate", airupdate))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("air", air_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))

    app.add_handler(CallbackQueryHandler(on_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app

def main():
    # load JSON di startup
    global AIRDROPS, LAST_SLUGS
    AIRDROPS = load_airdrops_json()
    LAST_SLUGS = {a.slug for a in AIRDROPS}
    log.info("Loaded %d airdrops dari JSON", len(AIRDROPS))

    app = build_app()
    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

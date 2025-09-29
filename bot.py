#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, math, time, logging, asyncio
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    constants as TG
)
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes,
    filters
)

# ===================== ENV & LOG =====================

load_dotenv(override=True)

BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_CHAT_ID   = os.getenv("ADMIN_CHAT_ID", "").strip()  # optional, kirim ringkasan auto-update
FIAT_DEFAULT    = os.getenv("FIAT_DEFAULT", "usd").lower()

if not BOT_TOKEN:
    raise SystemExit("ENV BOT_TOKEN belum diisi")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# ===================== MODEL & UTIL =====================

STORE_FILE = "airdrops.json"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AirdropCoreBot/1.0",
    "Accept-Language": "en-US,en;q=0.8"
}

@dataclass
class Airdrop:
    slug: str
    name: str
    url: str
    source: str
    reward: str = "-"
    chain: str = "-"

def _clean(s: Optional[str]) -> str:
    if not s: return ""
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def _maybe_lxml() -> str:
    # gunakan lxml kalau tersedia
    try:
        import lxml  # noqa
        return "lxml"
    except Exception:
        return "html.parser"

def _abs_url(href: str, base: str) -> str:
    return urljoin(base, href or "")

# ===================== STORAGE =====================

def load_store() -> Dict:
    if not os.path.exists(STORE_FILE):
        return {"items": [], "updated_at": 0, "seen_slugs": [], "per_source": {}}
    with open(STORE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_store(items: List[Airdrop], per_source: Dict[str, int]) -> None:
    store = load_store()
    payload = {
        "items": [asdict(x) for x in items],
        "updated_at": int(time.time()),
        "seen_slugs": store.get("seen_slugs", []),
        "per_source": per_source
    }
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def get_items() -> List[Airdrop]:
    store = load_store()
    return [Airdrop(**x) for x in store.get("items", [])]

# ===================== SCRAPERS =====================

def _uniq_by_slug(items: List[Airdrop]) -> List[Airdrop]:
    mp: Dict[str, Airdrop] = {}
    for a in items:
        k = a.slug.lower()
        old = mp.get(k)
        if (old is None or
            (a.reward != "-" and (old.reward == "-" or len(a.reward) < len(a.reward))) or
            (a.chain != "-" and old.chain == "-")):
            mp[k] = a
    return list(mp.values())

def scrape_airdrops_io(pages: int = 1) -> List[Airdrop]:
    out: List[Airdrop] = []
    parser = _maybe_lxml()
    for p in range(1, pages + 1):
        url = "https://airdrops.io/latest/" if p == 1 else f"https://airdrops.io/page/{p}/"
        r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
        soup = BeautifulSoup(r.text, parser)
        cards = soup.select(".airdrops-list .item, article, .post, .card")
        for c in cards:
            a = c.select_one("h2 a, h3 a, .title a, a")
            name = _clean(a.get_text() if a else "")
            if not name: continue
            href = _abs_url(a.get("href") if a else "", url)
            reward = _clean((c.select_one(".reward, .prize, .subtitle, .excerpt") or {}).get_text()
                            if c.select_one(".reward, .prize, .subtitle, .excerpt") else "")
            chain  = _clean((c.select_one(".chain, .network, .category") or {}).get_text()
                            if c.select_one(".chain, .network, .category") else "")
            out.append(Airdrop(_slugify(name), name, href, "airdrops.io", reward or "-", chain or "-"))
    return out

def scrape_airdropalert() -> List[Airdrop]:
    out: List[Airdrop] = []
    parser = _maybe_lxml()
    base = "https://airdropalert.com/latest-airdrops"
    r = requests.get(base, headers=UA, timeout=25)
    if r.status_code != 200: return out
    soup = BeautifulSoup(r.text, parser)
    cards = soup.select("article, .airdrop-list .airdrop, .post, .card")
    for c in cards:
        t = c.select_one("h2, h3, .title, a")
        name = _clean(t.get_text() if t else "")
        if not name: continue
        a = c.select_one("a")
        href = _abs_url(a.get("href") if a else "", base)
        reward = _clean((c.select_one(".reward, .excerpt, .subtitle") or {}).get_text()
                        if c.select_one(".reward, .excerpt, .subtitle") else "")
        out.append(Airdrop(_slugify(name), name, href, "airdropalert.com", reward or "-", "-"))
    return out

def scrape_coincodex() -> List[Airdrop]:
    out: List[Airdrop] = []
    parser = _maybe_lxml()
    url = "https://coincodex.com/airdrop/"
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code != 200: return out
    soup = BeautifulSoup(r.text, parser)
    for tr in soup.select("table tr"):
        a = tr.select_one("a")
        if not a: continue
        name = _clean(a.get_text());  href = _abs_url(a.get("href"), url)
        if not name: continue
        reward = _clean((tr.select_one(".cc-table__td--right, td:nth-last-child(1)") or {}).get_text()
                        if tr else "")
        out.append(Airdrop(_slugify(name), name, href, "coincodex.com", reward or "-", "-"))
    return out

def scrape_cryptorank() -> List[Airdrop]:
    out: List[Airdrop] = []
    parser = _maybe_lxml()
    tried = ["https://cryptorank.io/airdrops", "https://cryptorank.com/airdrops"]
    for base in tried:
        try:
            r = requests.get(base, headers=UA, timeout=25)
            if r.status_code != 200: continue
            soup = BeautifulSoup(r.text, parser)
            cards = soup.select("article, .airdrop-card, .card, .list-item, tr")
            for c in cards:
                a = c.select_one("a[href]")
                if not a: continue
                name = _clean((c.select_one("h3, h2, .title") or a).get_text())
                if not name: continue
                href = _abs_url(a.get("href"), base)
                reward = _clean((c.select_one(".reward, .subtitle, .desc, .right") or {}).get_text()
                                if c.select_one(".reward, .subtitle, .desc, .right") else "")
                chain  = _clean((c.select_one(".chain, .network, .tags, .left") or {}).get_text()
                                if c.select_one(".chain, .network, .tags, .left") else "")
                out.append(Airdrop(_slugify(name), name, href, "cryptorank", reward or "-", chain or "-"))
            if out: break
        except Exception:
            continue
    return out

def scrape_coinmarketcap() -> List[Airdrop]:
    out: List[Airdrop] = []
    parser = _maybe_lxml()
    base = "https://coinmarketcap.com/airdrop/"
    r = requests.get(base, headers=UA, timeout=25)
    if r.status_code != 200: return out
    soup = BeautifulSoup(r.text, parser)
    cards = soup.select("a[href*='/airdrop/']")
    seen = set()
    for a in cards:
        href = _abs_url(a.get("href"), base)
        name = _clean((a.get("title") or a.get_text()))
        if not name or (href in seen): continue
        seen.add(href)
        parent = a.find_parent()
        reward = "-"
        if parent:
            near = parent.select_one(".sc-*, .reward, .subtitle")
            if near: reward = _clean(near.get_text())
        out.append(Airdrop(_slugify(name), name, href, "coinmarketcap", reward, "-"))
    return out

def scrape_airdropbob() -> List[Airdrop]:
    out: List[Airdrop] = []
    parser = _maybe_lxml()
    base = "https://www.airdropbob.com/airdrops"
    r = requests.get(base, headers=UA, timeout=25)
    if r.status_code != 200: return out
    soup = BeautifulSoup(r.text, parser)
    cards = soup.select("article, .airdrop, .card, .post, .teaser")
    for c in cards:
        t = c.select_one("h2 a, h3 a, a[href*='/airdrop/']")
        if not t: continue
        name = _clean(t.get_text())
        href = _abs_url(t.get("href"), base)
        reward = _clean((c.select_one(".reward, .subtitle, .excerpt") or {}).get_text()
                        if c.select_one(".reward, .subtitle, .excerpt") else "")
        out.append(Airdrop(_slugify(name), name, href, "airdropbob", reward or "-", "-"))
    return out

def scrape_airdrops_fun() -> List[Airdrop]:
    out: List[Airdrop] = []
    parser = _maybe_lxml()
    base = "https://airdrops.fun/"
    try:
        r = requests.get(base, headers=UA, timeout=25)
        if r.status_code != 200: return out
        soup = BeautifulSoup(r.text, parser)
        cards = soup.select("article, .card, a[href*='/airdrop/']")
        for c in cards:
            a = c if c.name == "a" else c.select_one("a")
            if not a: continue
            name = _clean((c.select_one("h2, h3, .title") or a).get_text())
            if not name: continue
            href = _abs_url(a.get("href"), base)
            reward = _clean((c.select_one(".reward, .subtitle") or {}).get_text()
                            if c.select_one(".reward, .subtitle") else "")
            out.append(Airdrop(_slugify(name), name, href, "airdrops.fun", reward or "-", "-"))
    except Exception:
        return out
    return out

def scrape_all_sources(pages: int = 1) -> Tuple[List[Airdrop], Dict[str, int]]:
    got: List[Airdrop] = []
    per_src: Dict[str, int] = {}
    SOURCES = [
        ("airdrops.io",   lambda: scrape_airdrops_io(pages)),
        ("airdropalert",  scrape_airdropalert),
        ("airdropbob",    scrape_airdropbob),
        ("cryptorank",    scrape_cryptorank),
        ("coincodex",     scrape_coincodex),
        ("airdrops.fun",  scrape_airdrops_fun),
        ("coinmarketcap", scrape_coinmarketcap),
    ]
    for name, fn in SOURCES:
        try:
            items = fn() if callable(fn) else []
            got.extend(items); per_src[name] = len(items)
        except Exception as e:
            log.warning("scrape %s gagal: %s", name, e)
            per_src[name] = 0
    return _uniq_by_slug(got), per_src

# ===================== HARGA COIN =====================

CG_MAP = {}  # cache symbol→id
def coingecko_ids() -> Dict[str, str]:
    global CG_MAP
    if CG_MAP: return CG_MAP
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/list?include_platform=false", timeout=30)
        if r.status_code == 200:
            data = r.json()
            CG_MAP = { (d["symbol"].lower()): d["id"] for d in data }
    except Exception:
        pass
    return CG_MAP

def fetch_price(ids: List[str], fiat: str) -> Dict:
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        resp = requests.get(url, params={
            "ids": ",".join(ids),
            "vs_currencies": fiat,
            "include_24hr_change": "true"
        }, timeout=20)
        return resp.json()
    except Exception:
        return {}

def norm_symbol(sym: str) -> Optional[str]:
    sym = (sym or "").lower()
    mp = {
        "btc":"bitcoin", "eth":"ethereum", "bnb":"binancecoin", "usdt":"tether",
        "usdc":"usd-coin", "sol":"solana", "ada":"cardano", "xrp":"ripple",
        "dot":"polkadot", "doge":"dogecoin", "trx":"tron", "matic":"polygon",
        "pi":"pi-network"  # jika sudah tersedia di CG
    }
    if sym in mp: return mp[sym]
    ids = coingecko_ids()
    return ids.get(sym)  # bisa None jika tak ada

def fmt_price(val, fiat): return f"{val:,.4f} {fiat.upper()}"

PAIR_FREE = re.compile(r"^\s*(\d+(\.\d+)?)?\s*([a-zA-Z0-9]{2,10})\s+([a-zA-Z]{2,10})\s*$")

async def handle_free_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap input bebas '0.25 btc idr' atau 'btc idr'. Diam jika pair invalid."""
    text = (update.message.text or "").strip()
    m = PAIR_FREE.match(text)
    if not m: return False
    qty = float(m.group(1) or 1.0)
    base = m.group(3); fiat = m.group(4).lower()
    cid = norm_symbol(base)
    if not cid:  # tidak dikenal → diam
        return True
    data = fetch_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        return True  # pair tak ada → diam
    price = data[cid][fiat] * qty
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
    await update.message.reply_html(f"💰 <b>{base.upper()}</b> × {qty:g} = <b>{fmt_price(price, fiat)}</b>{chg_txt}")
    return True

# ===================== RENDERING =====================

def chunk(lst: List, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def airdrop_line(a: Airdrop) -> str:
    name = a.name.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return f"• <b>{name}</b> (<a href=\"{a.url}\">{a.source}</a>)\n  Reward: {a.reward}\n  Chain: {a.chain}"

# ===================== COMMANDS =====================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🔁 AirUpdate", callback_data="airupdate"),
         InlineKeyboardButton("🎁 Airdrop", callback_data="airlist:1")],
        [InlineKeyboardButton("🧠 AI", callback_data="aihint"),
         InlineKeyboardButton("💱 Harga", callback_data="pricehint")],
    ]
    text = ("<b>AirdropCore (AI)</b>\n\n"
            "• /airupdate (update daftar)\n"
            "• /airdrops (daftar & paging)\n"
            "• /air &lt;keyword&gt; (detail)\n"
            "• /tugas &lt;keyword&gt; (steps + link)\n\n"
            "💲 <b>Harga &amp; Market</b>\n"
            "• /price btc idr\n"
            "• /prices btc,eth usdt\n"
            "• /top 10 idr,/trend\n"
            "• bebas: <i>0.25 btc idr,eth/idr</i>\n"
            "AI juga bisa chat bebas.")
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True)

async def airstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = load_store()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("updated_at", 0)))
    counts = st.get("per_source", {})
    detail = "\n".join([f"• {k}: {v}" for k, v in counts.items()]) or "-"
    await update.message.reply_html(
        f"🩺 <b>Status</b>\n"
        f"Total tersimpan: <b>{len(st.get('items', []))}</b>\n"
        f"Updated: <code>{ts}</code>\nPer sumber:\n{detail}"
    )

async def airclear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = {"items": [], "updated_at": 0, "seen_slugs": [], "per_source": {}}
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    await update.message.reply_text("🧹 Cache airdrop dibersihkan.")

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pages = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 1
    await update.message.reply_text(f"🔄 Update airdrops (pages={pages})…")
    items, per_src = scrape_all_sources(pages)
    before = {a.slug for a in get_items()}
    save_store(items, per_src)
    after = {a.slug for a in items}
    added = len(after - before)
    per_detail = "\n".join([f"• {k}: {v}" for k, v in per_src.items()])
    await update.message.reply_html(
        f"✅ <b>Selesai</b>. Terkumpul <b>{len(items)}</b> airdrop.\n"
        f"Baru sejak terakhir: <b>{added}</b>\n<i>Per sumber:</i>\n{per_detail}",
        disable_web_page_preview=True
    )

async def airnews_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = load_store()
    current = [Airdrop(**x) for x in st.get("items", [])]
    seen = set(st.get("seen_slugs", []))
    news = [a for a in current if a.slug not in seen]
    if not news:
        await update.message.reply_text("ℹ️ Belum ada airdrop baru sejak terakhir dicek.")
        return
    st["seen_slugs"] = list(seen | {a.slug for a in news})
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    lines = [airdrop_line(a) for a in news[:15]]
    await update.message.reply_html("🆕 <b>Airdrop Baru</b>:\n" + "\n".join(lines), disable_web_page_preview=True)

PAGE_SIZE = 7

def render_page(page: int) -> Tuple[str, InlineKeyboardMarkup]:
    items = get_items()
    total = len(items)
    max_page = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), max_page)
    start = (page-1)*PAGE_SIZE
    chunk_items = items[start:start+PAGE_SIZE]
    body = "\n".join(airdrop_line(a) for a in chunk_items) or "Belum ada data."
    nav = [
        InlineKeyboardButton("⬅️ Prev", callback_data=f"airlist:{page-1}" if page>1 else "airlist:1"),
        InlineKeyboardButton(f"Hal {page}/{max_page}", callback_data="noop"),
        InlineKeyboardButton("Next ➡️", callback_data=f"airlist:{page+1}" if page<max_page else f"airlist:{max_page}")
    ]
    return f"🎁 <b>Airdrop</b> (hal {page}/{max_page})\n{body}", InlineKeyboardMarkup([nav])

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = render_page(1)
    await update.message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)

async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    if data.startswith("airlist:"):
        page = int(data.split(":")[1])
        text, kb = render_page(page)
        await q.edit_message_text(text=text, reply_markup=kb, parse_mode=TG.ParseMode.HTML, disable_web_page_preview=True)
        await q.answer()
    elif data == "airupdate":
        await q.answer("Mulai update…")
        items, per_src = scrape_all_sources(1)
        save_store(items, per_src)
        text, kb = render_page(1)
        await q.edit_message_text(text=text, reply_markup=kb, parse_mode=TG.ParseMode.HTML, disable_web_page_preview=True)
    else:
        await q.answer()

def find_airdrop(keyword: str) -> Optional[Airdrop]:
    kw = keyword.lower().strip()
    items = get_items()
    for a in items:
        if kw==a.slug or kw==a.name.lower() or kw in a.name.lower() or kw in a.slug:
            return a
    return None

async def air_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /air <keyword>")
        return
    kw = " ".join(ctx.args)
    a = find_airdrop(kw)
    if not a:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{kw}'.")
        return
    await update.message.reply_html(
        f"🎯 <b>{a.name}</b>\n"
        f"Sumber: <a href=\"{a.url}\">{a.source}</a>\n"
        f"Reward: {a.reward}\n"
        f"Chain: {a.chain}",
        disable_web_page_preview=False
    )

def extract_steps_from_page(html: str, base: str) -> List[str]:
    soup = BeautifulSoup(html, _maybe_lxml())
    # prioritaskan daftar berurutan
    ol = soup.select_one("ol")
    if ol:
        steps = [ _clean(li.get_text(" ", strip=True)) for li in ol.select("li") ][:15]
        return [f"{i}. {s}" for i,s in enumerate(steps,1) if s]
    # fallback: cari bullet list
    ul = soup.select_one("ul")
    if ul:
        steps = [ _clean(li.get_text(" ", strip=True)) for li in ul.select("li") ][:15]
        return [f"- {s}" for s in steps if s]
    # terakhir: paragraf pendek yang terlihat seperti instruksi
    paras = [p for p in soup.select("p") if len(p.get_text(strip=True))<220]
    steps = [ _clean(p.get_text(" ", strip=True)) for p in paras[:10] ]
    return [f"- {s}" for s in steps if s]

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>")
        return
    kw = " ".join(ctx.args)
    a = find_airdrop(kw)
    if not a:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{kw}'.")
        return
    try:
        r = requests.get(a.url, headers=UA, timeout=25)
        r.raise_for_status()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Gagal membuka halaman sumber: {e}")
        return
    steps = extract_steps_from_page(r.text, a.url)
    if not steps:
        await update.message.reply_html(
            f"ℹ️ <b>{a.name}</b> – tidak menemukan daftar langkah yang jelas.\n"
            f"Lihat sumber: <a href=\"{a.url}\">{a.source}</a>"
        )
        return
    body = "\n".join(steps)
    await update.message.reply_html(
        f"<b>{a.name}</b> – <u>Tugas</u>:\n{body}\n\nSumber: <a href=\"{a.url}\">{a.source}</a>",
        disable_web_page_preview=False
    )

# ---------- Harga Commands ----------

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\ncontoh: /price btc usdt")
        return
    sym = ctx.args[0]; fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    cid = norm_symbol(sym)
    if not cid:
        return  # hemat: diam
    data = fetch_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        return  # diam
    price = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int,float)) else ""
    await update.message.reply_html(f"💰 <b>{sym.upper()}</b> = <b>{fmt_price(price, fiat)}</b>{chg_txt}")

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args)<2:
        await update.message.reply_text("Format: /prices btc,eth idr")
        return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = ctx.args[1].lower()
    ids = [norm_symbol(s) for s in syms]
    ids = [i for i in ids if i]
    if not ids:
        return
    data = fetch_price(ids, fiat)
    lines=[]
    for s,i in zip(syms, ids):
        try:
            val = data[i][fiat]
            lines.append(f"• <b>{s.upper()}</b> = {fmt_price(val,fiat)}")
        except Exception:
            pass
    if not lines: return
    await update.message.reply_html("📈 <b>Harga</b>\n"+"\n".join(lines))

# ---------- Free text router ----------

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 1) numeric pair bebas → harga
    if await handle_free_price(update, ctx):
        return
    # 2) fallback AI (opsional)
    if OPENAI_API_KEY:
        # Hemat: matikan default AI kalau tidak diminta (bisa diaktifkan lagi kalau kamu mau)
        return

# ===================== JOBS =====================

async def auto_update_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        items, per_src = scrape_all_sources(1)
        before = {a.slug for a in get_items()}
        save_store(items, per_src)
        added = len({a.slug for a in items} - before)
        if added and ADMIN_CHAT_ID:
            per_detail = "\n".join([f"• {k}: {v}" for k, v in per_src.items()])
            await ctx.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=f"🕒 Auto-update selesai. Baru: {added}\nTotal: {len(items)}\nPer sumber:\n{per_detail}"
            )
    except Exception as e:
        log.warning("auto_update_job error: %s", e)

# ===================== BUILD & RUN =====================

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("airstatus", airstatus))
    app.add_handler(CommandHandler("airclear", airclear))
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airnews", airnews_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("air", air_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))

    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))

    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Auto update tiap 4 jam (mulai 2 menit setelah start)
    app.job_queue.run_repeating(auto_update_job, interval=4*60*60, first=120)

    return app

def main():
    app = build_app()
    log.info("Bot start…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

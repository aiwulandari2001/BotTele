#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, math, time, logging
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, constants as TG
)
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ============ ENV & LOG ============
load_dotenv(override=True)

BOT_TOKEN      = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
FIAT_DEFAULT   = os.getenv("FIAT_DEFAULT", "usd").lower()

if not BOT_TOKEN:
    raise SystemExit("ENV BOT_TOKEN belum diisi")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("airdropcore.bot")

# ============ OPENAI CLIENT ============
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

SYSTEM_PROMPT = (
    "Kamu adalah asisten investasi kripto bernama AirdropCore AI. "
    "Jawab ringkas, terstruktur (gunakan poin/nomor bila perlu), dan to-the-point. "
    "Sertakan konteks & peringatan risiko seperlunya. "
    "Jika tidak yakin, katakan apa yang diperlukan untuk memastikan."
)

async def ai_reply(update: Update, text: str, max_tokens=450, temperature=0.5):
    if not client:
        return await update.message.reply_text("❌ API Key OpenAI belum diatur di .env")
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content": SYSTEM_PROMPT},
                {"role":"user","content": text}
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        answer = (resp.choices[0].message.content or "").strip()
        # kirim sebagai HTML aman (OpenAI bisa ada tag/code block)
        return await update.message.reply_html(answer)
    except Exception as e:
        log.exception("AI error")
        return await update.message.reply_text(f"❌ Error AI: {e}")

# ============ MODEL ============

STORE_FILE = "airdrops.json"
CACHE_DIR  = ".aircache"

@dataclass
class Airdrop:
    slug: str
    name: str
    url: str
    source: str
    reward: str = "-"
    chain: str = "-"

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AirdropCoreBot/3.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

def _parser() -> str:
    try:
        import lxml  # noqa
        return "lxml"
    except Exception:
        return "html.parser"

def _clean(s: Optional[str]) -> str:
    if not s: return ""
    return re.sub(r"\s+", " ", s).strip()

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def _u(href: str, base: str) -> str:
    return urljoin(base, href or "")

# ============ STORE ============
def load_store() -> Dict:
    if not os.path.exists(STORE_FILE):
        return {"items": [], "updated_at": 0, "seen_slugs": [], "per_source": {}}
    with open(STORE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_store(items: List[Airdrop], per_source: Dict[str, int]) -> None:
    data = {
        "items": [asdict(x) for x in items],
        "updated_at": int(time.time()),
        "seen_slugs": load_store().get("seen_slugs", []),
        "per_source": per_source
    }
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_items() -> List[Airdrop]:
    st = load_store()
    return [Airdrop(**x) for x in st.get("items", [])]

# ============ SIMPLE DISK CACHE ============
def cache_path(url: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = re.sub(r"[^a-zA-Z0-9]+", "_", url)[:160]
    return os.path.join(CACHE_DIR, key + ".html")

def cache_get(url: str, ttl_sec=6*3600) -> Optional[str]:
    p = cache_path(url)
    if os.path.exists(p) and (time.time() - os.path.getmtime(p) < ttl_sec):
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            return None
    return None

def cache_set(url: str, html_text: str) -> None:
    try:
        with open(cache_path(url), "w", encoding="utf-8") as f:
            f.write(html_text or "")
    except Exception:
        pass

# ============ SCRAPERS ============
def _uniq(items: List[Airdrop]) -> List[Airdrop]:
    out: Dict[str, Airdrop] = {}
    for a in items:
        k = a.slug
        if k not in out: out[k] = a
        else:
            if out[k].reward == "-" and a.reward != "-": out[k].reward = a.reward
            if out[k].chain  == "-" and a.chain  != "-": out[k].chain  = a.chain
    return list(out.values())

def scrape_airdrops_io(pages: int = 1) -> List[Airdrop]:
    res: List[Airdrop] = []
    parser = _parser()
    for p in range(1, pages+1):
        url = "https://airdrops.io/latest/" if p == 1 else f"https://airdrops.io/page/{p}/"
        r = requests.get(url, headers=UA, timeout=25); r.raise_for_status()
        soup = BeautifulSoup(r.text, parser)
        for c in soup.select(".airdrops-list .item, article, .post, .card"):
            a = c.select_one("h2 a, h3 a, .title a, a")
            name = _clean(a.get_text() if a else "")
            if not name: continue
            href = _u(a.get("href") if a else "", url)
            reward = _clean((c.select_one(".reward, .prize, .subtitle, .excerpt") or {}).get_text()
                            if c.select_one(".reward, .prize, .subtitle, .excerpt") else "")
            chain = _clean((c.select_one(".chain, .network, .category") or {}).get_text()
                           if c.select_one(".chain, .network, .category") else "")
            res.append(Airdrop(_slugify(name), name, href, "airdrops.io", reward or "-", chain or "-"))
    return res

def scrape_airdropalert() -> List[Airdrop]:
    base = "https://airdropalert.com/latest-airdrops"
    r = requests.get(base, headers=UA, timeout=25)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, _parser())
    out: List[Airdrop] = []
    for c in soup.select("article, .airdrop, .post, .card"):
        t = c.select_one("h2 a, h3 a, a")
        if not t: continue
        name = _clean(t.get_text())
        if not name: continue
        href = _u(t.get("href"), base)
        reward = _clean((c.select_one(".reward, .excerpt, .subtitle") or {}).get_text()
                        if c.select_one(".reward, .excerpt, .subtitle") else "")
        out.append(Airdrop(_slugify(name), name, href, "airdropalert", reward or "-", "-"))
    return out

def scrape_airdropbob() -> List[Airdrop]:
    base = "https://www.airdropbob.com/airdrops"
    r = requests.get(base, headers=UA, timeout=25)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, _parser())
    out: List[Airdrop] = []
    for c in soup.select("article, .airdrop, .card, .post, .teaser"):
        t = c.select_one("h2 a, h3 a, a[href*='/airdrop/']")
        if not t: continue
        name = _clean(t.get_text())
        if not name: continue
        href = _u(t.get("href"), base)
        reward = _clean((c.select_one(".reward, .subtitle, .excerpt") or {}).get_text()
                        if c.select_one(".reward, .subtitle, .excerpt") else "")
        out.append(Airdrop(_slugify(name), name, href, "airdropbob", reward or "-", "-"))
    return out

def scrape_cryptorank() -> List[Airdrop]:
    out: List[Airdrop] = []
    for base in ("https://cryptorank.io/airdrops", "https://cryptorank.com/airdrops"):
        try:
            r = requests.get(base, headers=UA, timeout=25)
            if r.status_code != 200: continue
            soup = BeautifulSoup(r.text, _parser())
            for c in soup.select("article, .card, .list-item, tr"):
                a = c.select_one("a[href]")
                if not a: continue
                name = _clean((c.select_one("h3, h2, .title") or a).get_text())
                if not name: continue
                href = _u(a.get("href"), base)
                reward = _clean((c.select_one(".reward, .subtitle, .desc, .right") or {}).get_text()
                                if c.select_one(".reward, .subtitle, .desc, .right") else "")
                chain  = _clean((c.select_one(".chain, .network, .tags, .left") or {}).get_text()
                                if c.select_one(".chain, .network, .tags, .left") else "")
                out.append(Airdrop(_slugify(name), name, href, "cryptorank", reward or "-", chain or "-"))
            if out: break
        except Exception:
            continue
    return out

def scrape_coincodex() -> List[Airdrop]:
    url = "https://coincodex.com/airdrop/"
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, _parser())
    out: List[Airdrop] = []
    for tr in soup.select("table tr"):
        a = tr.select_one("a")
        if not a: continue
        name = _clean(a.get_text())
        if not name: continue
        href = _u(a.get("href"), url)
        reward = _clean((tr.select_one(".cc-table__td--right, td:nth-last-child(1)") or {}).get_text()
                        if tr else "")
        out.append(Airdrop(_slugify(name), name, href, "coincodex", reward or "-", "-"))
    return out

def scrape_airdrops_fun() -> List[Airdrop]:
    base = "https://airdrops.fun/"
    try:
        r = requests.get(base, headers=UA, timeout=25)
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.text, _parser())
        out: List[Airdrop] = []
        for c in soup.select("article, .card, a[href*='/airdrop/']"):
            a = c if c.name == "a" else c.select_one("a")
            if not a: continue
            name = _clean((c.select_one("h2, h3, .title") or a).get_text())
            if not name: continue
            href = _u(a.get("href"), base)
            reward = _clean((c.select_one(".reward, .subtitle") or {}).get_text()
                            if c.select_one(".reward, .subtitle") else "")
            out.append(Airdrop(_slugify(name), name, href, "airdrops.fun", reward or "-", "-"))
        return out
    except Exception:
        return []

def scrape_coinmarketcap() -> List[Airdrop]:
    base = "https://coinmarketcap.com/airdrop/"
    r = requests.get(base, headers=UA, timeout=25)
    if r.status_code != 200: return []
    soup = BeautifulSoup(r.text, _parser())
    out: List[Airdrop] = []
    seen = set()
    for a in soup.select("a[href*='/airdrop/']"):
        href = _u(a.get("href"), base)
        if href in seen: continue
        seen.add(href)
        name = _clean(a.get("title") or a.get_text())
        if not name: continue
        reward = "-"
        parent = a.find_parent()
        if parent:
            near = parent.select_one(".sc-*, .reward, .subtitle")
            if near: reward = _clean(near.get_text())
        out.append(Airdrop(_slugify(name), name, href, "coinmarketcap", reward, "-"))
    return out

def scrape_all_sources(pages: int = 1) -> Tuple[List[Airdrop], Dict[str, int]]:
    all_items: List[Airdrop] = []
    per: Dict[str, int] = {}
    sources = [
        ("airdrops.io",   lambda: scrape_airdrops_io(pages)),
        ("airdropalert",  scrape_airdropalert),
        ("airdropbob",    scrape_airdropbob),
        ("cryptorank",    scrape_cryptorank),
        ("coincodex",     scrape_coincodex),
        ("airdrops.fun",  scrape_airdrops_fun),
        ("coinmarketcap", scrape_coinmarketcap),
    ]
    for name, fn in sources:
        try:
            items = fn() if callable(fn) else []
            per[name] = len(items); all_items.extend(items)
        except Exception as e:
            log.warning("scrape %s gagal: %s", name, e)
            per[name] = 0
    return _uniq(all_items), per

# ============ HARGA COIN ============
CG_MAP = {}

def coingecko_ids() -> Dict[str, str]:
    global CG_MAP
    if CG_MAP: return CG_MAP
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/list?include_platform=false", timeout=30)
        if r.status_code == 200:
            CG_MAP = {d["symbol"].lower(): d["id"] for d in r.json()}
    except Exception:
        pass
    return CG_MAP

def norm_symbol(sym: str) -> Optional[str]:
    sym = (sym or "").lower()
    presets = {
        "btc":"bitcoin","eth":"ethereum","bnb":"binancecoin","usdt":"tether",
        "usdc":"usd-coin","sol":"solana","ada":"cardano","xrp":"ripple",
        "dot":"polkadot","doge":"dogecoin","trx":"tron","matic":"polygon",
        "pi":"pi-network"  # jika aktif di CG
    }
    if sym in presets: return presets[sym]
    return coingecko_ids().get(sym)

def fetch_price(ids: List[str], fiat: str) -> Dict:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={
            "ids": ",".join(ids), "vs_currencies": fiat,
            "include_24hr_change": "true"
        }, timeout=20)
        return r.json()
    except Exception:
        return {}

def fmt_price(val, fiat): return f"{val:,.4f} {fiat.upper()}"

PAIR_FREE = re.compile(r"^\s*(\d+(?:\.\d+)?)?\s*([a-zA-Z0-9]{2,10})\s+([a-zA-Z]{2,10})\s*$")

async def handle_free_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    text = (update.message.text or "").strip()
    m = PAIR_FREE.match(text)
    if not m: return False
    qty  = float(m.group(1) or 1.0)
    base = m.group(2); fiat = m.group(3).lower()
    cid = norm_symbol(base)
    if not cid: return True   # diam: tak dikenal
    data = fetch_price([cid], fiat)
    if cid not in data or fiat not in data[cid]: return True  # diam
    price = data[cid][fiat] * qty
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_html(f"💰 <b>{base.upper()}</b> × {qty:g} = <b>{fmt_price(price, fiat)}</b>{chg_txt}")
    return True

# ============ RENDERING ============
def airdrop_line(a: Airdrop) -> str:
    name = (a.name or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return f"• <b>{name}</b> (<a href=\"{a.url}\">{a.source}</a>)\n  Reward: {a.reward}\n  Chain: {a.chain}"

def render_page(page: int, size: int = 7) -> Tuple[str, InlineKeyboardMarkup]:
    items = get_items()
    total = len(items)
    maxp = max(1, math.ceil(total/size))
    page = min(max(page,1), maxp)
    start = (page-1)*size
    body = "\n".join(airdrop_line(a) for a in items[start:start+size]) or "Belum ada data."
    nav = [
        InlineKeyboardButton("⬅️ Prev", callback_data=f"airlist:{page-1 if page>1 else 1}"),
        InlineKeyboardButton(f"Hal {page}/{maxp}", callback_data="noop"),
        InlineKeyboardButton("Next ➡️", callback_data=f"airlist:{page+1 if page<maxp else maxp}")
    ]
    return f"🎁 <b>Airdrop</b> (hal {page}/{maxp})\n{body}", InlineKeyboardMarkup([nav])

# ============ COMMANDS ============
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🔁 AirUpdate", callback_data="airupdate"),
         InlineKeyboardButton("🎁 Airdrop", callback_data="airlist:1")],
        [InlineKeyboardButton("🧠 AI /ask", callback_data="aihint"),
         InlineKeyboardButton("💱 Harga Tips", callback_data="pricehint")]
    ]
    txt = (
        "<b>AirdropCore Bot</b>\n\n"
        "🎁 Airdrop:\n"
        "• /airupdate (update multi-sumber)\n"
        "• /airdrops (daftar & paging)\n"
        "• /air <keyword> (detail)\n"
        "• /tugas <keyword> (steps + link)\n"
        "• /airstatus, /airnews, /airclear\n\n"
        "💲 Harga:\n"
        "• /price btc idr, /prices btc,eth usdt\n"
        "• bebas: <i>0.25 btc idr</i>\n\n"
        "🧠 AI:\n"
        "• /ask <pertanyaan> atau chat biasa tanpa slash."
    )
    await update.message.reply_html(txt, reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True)

async def airstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = load_store()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("updated_at",0)))
    src = "\n".join(f"• {k}: {v}" for k,v in st.get("per_source",{}).items()) or "-"
    await update.message.reply_html(
        f"🩺 <b>Status</b>\nTotal: <b>{len(st.get('items',[]))}</b>\nUpdated: <code>{ts}</code>\nPer sumber:\n{src}"
    )

async def airclear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with open(STORE_FILE,"w",encoding="utf-8") as f:
        json.dump({"items":[], "updated_at":0, "seen_slugs":[], "per_source":{}}, f, ensure_ascii=False, indent=2)
    await update.message.reply_text("🧹 Cache airdrop dibersihkan.")

async def airexport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(STORE_FILE):
        await update.message.reply_text("Belum ada data.")
        return
    await update.message.reply_document(document=open(STORE_FILE,"rb"), filename="airdrops.json", caption="📦 Export airdrops.json")

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pages = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 1
    await update.message.reply_text(f"🔄 Update airdrops (pages={pages})…")
    items, per_src = scrape_all_sources(pages)
    before = {a.slug for a in get_items()}
    save_store(items, per_src)
    added = len({a.slug for a in items} - before)
    per_detail = "\n".join([f"• {k}: {v}" for k, v in per_src.items()])
    await update.message.reply_html(
        f"✅ <b>Selesai</b>. Terkumpul <b>{len(items)}</b> airdrop.\n"
        f"Baru sejak terakhir: <b>{added}</b>\n<i>Per sumber:</i>\n{per_detail}"
    )

async def airnews_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st = load_store()
    now_items = [Airdrop(**x) for x in st.get("items",[])]
    seen = set(st.get("seen_slugs", []))
    baru = [a for a in now_items if a.slug not in seen]
    if not baru:
        await update.message.reply_text("ℹ️ Belum ada airdrop baru.")
        return
    st["seen_slugs"] = list(seen | {a.slug for a in baru})
    with open(STORE_FILE,"w",encoding="utf-8") as f:
        json.dump(st,f,ensure_ascii=False,indent=2)
    await update.message.reply_html("🆕 <b>Airdrop Baru</b>:\n" + "\n".join(airdrop_line(a) for a in baru[:15]))

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = render_page(1)
    await update.message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)

async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""
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

def find_airdrop(kw: str) -> Optional[Airdrop]:
    key = kw.lower().strip()
    for a in get_items():
        if key==a.slug or key==a.name.lower() or key in a.slug or key in a.name.lower():
            return a
    return None

async def air_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /air <keyword>")
        return
    kw = " ".join(ctx.args)
    a = find_airdrop(kw)
    if not a:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{kw}'."); return
    await update.message.reply_html(
        f"🎯 <b>{a.name}</b>\nSumber: <a href=\"{a.url}\">{a.source}</a>\nReward: {a.reward}\nChain: {a.chain}",
        disable_web_page_preview=False
    )

def extract_steps(html_text: str, base: str) -> Tuple[List[str], Optional[str]]:
    soup = BeautifulSoup(html_text, _parser())
    join_link = None
    for a in soup.select("a[href]"):
        txt = _clean(a.get_text()).lower()
        if any(k in txt for k in ["join", "airdrop", "claim", "start", "app", "bot"]):
            join_link = _u(a.get("href"), base); break
    ol = soup.select_one("ol")
    if ol:
        steps = [_clean(li.get_text(" ", strip=True)) for li in ol.select("li")][:15]
        steps = [f"{i}. {s}" for i,s in enumerate(steps,1) if s]
        return steps, join_link
    ul = soup.select_one("ul")
    if ul:
        steps = [_clean(li.get_text(" ", strip=True)) for li in ul.select("li")][:15]
        steps = [f"- {s}" for s in steps if s]
        return steps, join_link
    paras = [p for p in soup.select("p") if 15 < len(p.get_text(strip=True)) < 220]
    steps = [f"- {_clean(p.get_text(' ', strip=True))}" for p in paras[:10]]
    return steps, join_link

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>"); return
    kw = " ".join(ctx.args)
    a = find_airdrop(kw)
    if not a:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{kw}'."); return
    html_text = cache_get(a.url)
    if html_text is None:
        try:
            r = requests.get(a.url, headers=UA, timeout=25); r.raise_for_status()
            html_text = r.text; cache_set(a.url, html_text)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Gagal membuka sumber: {e}"); return
    steps, join_url = extract_steps(html_text, a.url)
    if not steps:
        await update.message.reply_html(
            f"ℹ️ <b>{a.name}</b> – belum menemukan daftar langkah yang rapi.\n"
            f"Lihat sumber: <a href=\"{a.url}\">{a.source}</a>"
        ); return
    body = "\n".join(steps)
    extra = f"\n🔗 <a href=\"{join_url}\">Link Airdrop</a>" if join_url else ""
    await update.message.reply_html(
        f"<b>{a.name}</b> – <u>Tugas</u>:\n{body}{extra}\n\nSumber: <a href=\"{a.url}\">{a.source}</a>",
        disable_web_page_preview=False
    )

# ----- Harga -----
async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\ncontoh: /price btc usdt"); return
    sym = ctx.args[0]; fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    cid = norm_symbol(sym)
    if not cid: return
    data = fetch_price([cid], fiat)
    if cid not in data or fiat not in data[cid]: return
    price = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_html(f"💰 <b>{sym.upper()}</b> = <b>{fmt_price(price, fiat)}</b>{chg_txt}")

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args)<2:
        await update.message.reply_text("Format: /prices btc,eth idr"); return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = ctx.args[1].lower()
    ids = [norm_symbol(s) for s in syms]; ids = [i for i in ids if i]
    if not ids: return
    data = fetch_price(ids, fiat)
    lines=[]
    for s,i in zip(syms, ids):
        try: lines.append(f"• <b>{s.upper()}</b> = {fmt_price(data[i][fiat],fiat)}")
        except Exception: pass
    if not lines: return
    await update.message.reply_html("📈 <b>Harga</b>\n"+"\n".join(lines))

# ----- AI -----
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /ask <pertanyaan>"); return
    prompt = " ".join(ctx.args)
    await ai_reply(update, prompt, max_tokens=600, temperature=0.6)

# ----- Free text router -----
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 1) tangkap format harga bebas → sudah dibalas; kalau invalid → diam
    if await handle_free_price(update, ctx):
        return
    # 2) selain itu: AI fallback
    text = (update.message.text or "").strip()
    if not text:
        return
    await ai_reply(update, text, max_tokens=450, temperature=0.6)

# ============ JOBS ============
async def auto_update_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        items, per = scrape_all_sources(1)
        before = {a.slug for a in get_items()}
        save_store(items, per)
        added = len({a.slug for a in items} - before)
        if added and ADMIN_CHAT_ID:
            detail = "\n".join(f"• {k}: {v}" for k,v in per.items())
            await ctx.bot.send_message(ADMIN_CHAT_ID, f"🕒 Auto-update OK. Baru: {added}\nTotal: {len(items)}\n{detail}")
    except Exception as e:
        log.warning("auto_update_job error: %s", e)

# ============ BUILD & RUN ============
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("airstatus", airstatus))
    app.add_handler(CommandHandler("airclear", airclear))
    app.add_handler(CommandHandler("airexport", airexport))

    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airnews", airnews_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("air", air_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))

    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))

    app.add_handler(CommandHandler("ask", ask_cmd))  # AI command

    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.job_queue.run_repeating(auto_update_job, interval=4*60*60, first=120)
    return app

def main():
    app = build_app()
    log.info("Bot start…")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

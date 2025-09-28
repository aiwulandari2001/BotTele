#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, logging, html
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter, Retry
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ==== OpenAI optional ====
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # biarkan opsional

# ---------------- Config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_TOKEN_KAMU")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "ISI_API_KEY_KAMU")
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

# batas pagination airdrops
PER_PAGE = 5

# logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# OpenAI client (opsional)
client = None
if OpenAI and OPENAI_API_KEY and not OPENAI_API_KEY.startswith("ISI_"):
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client aktif")
    except Exception as e:
        log.warning("Gagal init OpenAI: %s", e)

# -------------- HTTP helpers --------------
def rand_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.7",
        "Connection": "keep-alive",
        "Referer": "https://www.google.com/",
    }

def http_get(url: str, timeout: int = 30) -> requests.Response:
    sess = requests.Session()
    retry = Retry(
        total=3, backoff_factor=0.6,
        status_forcelist=[429,500,502,503,504],
        allowed_methods=["GET","HEAD","OPTIONS"]
    )
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    sess.mount("http://", HTTPAdapter(max_retries=retry))
    return sess.get(url, headers=rand_headers(), timeout=timeout)

def _dns_ok(host: str) -> bool:
    try:
        requests.get(f"https://{host}", timeout=3)
        return True
    except Exception:
        return False

# -------------- CoinGecko symbol map --------------
_SYMBOLS: Dict[str, str] = {}   # "btc" -> "bitcoin"
_SYM_LAST = 0                   # epoch last refresh

def _refresh_symbols(force: bool=False) -> None:
    global _SYMBOLS, _SYM_LAST
    if not force and (time.time() - _SYM_LAST) < 6*3600 and _SYMBOLS:
        return
    try:
        r = http_get("https://api.coingecko.com/api/v3/coins/list?include_platform=false", timeout=40)
        r.raise_for_status()
        arr = r.json()
        m: Dict[str,str] = {}
        for it in arr:
            sym = (it.get("symbol") or "").lower()
            cid = (it.get("id") or "").lower()
            name = (it.get("name") or "").lower()
            if not sym or not cid:
                continue
            # prefer id with same symbol; store multiple keys
            m.setdefault(sym, cid)
            m.setdefault(name, cid)
            m.setdefault(cid, cid)
        if m:
            _SYMBOLS = m
            _SYM_LAST = time.time()
            log.info("Symbol map refreshed: %d entries", len(_SYMBOLS))
    except Exception as e:
        log.warning("Refresh symbols error: %s", e)

def norm_symbol(sym: str) -> Optional[str]:
    if not sym: return None
    _refresh_symbols(False)
    s = sym.lower().strip()
    return _SYMBOLS.get(s)

# -------------- Price utils --------------
def fetch_price(ids: List[str], fiat: str="usd") -> Dict[str, Dict[str,float]]:
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        r = http_get(url + f"?ids={','.join(ids)}&vs_currencies={fiat}&include_24hr_change=true", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("fetch_price error: %s", e)
        return {}

def fmt_price(val: float, fiat: str) -> str:
    if val >= 1:
        return f"{val:,.2f} {fiat.upper()}"
    return f"{val:,.6f} {fiat.upper()}"

PAIR_PATTERN = re.compile(r"^([0-9]*\.?[0-9]+)?\s*([a-zA-Z0-9]+)[/ ]([a-zA-Z0-9]+)$")
SIMPLE_PAIR = re.compile(r"^([a-zA-Z0-9]{2,10})(?:[/ ]([a-zA-Z0-9]{2,10}))?$")

# -------------- Airdrop data --------------
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None
    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())

AIRDROPS: List[Airdrop] = []
AIR_LAST: float = 0.0

def _clean_text(s: Optional[str]) -> Optional[str]:
    if not s: return None
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None

def _paged(items: List[Airdrop], page: int, per: int) -> List[Airdrop]:
    start = (page-1)*per
    return items[start:start+per]

def _air_list_text(items: List[Airdrop]) -> str:
    if not items: return "Tidak ada data."
    out = []
    for i,a in enumerate(items,1):
        line = f"<b>{i}. {html.escape(a.name)}</b>"
        if a.chain: line += f" • {html.escape(a.chain)}"
        if a.reward: line += f"\n  🎁 {html.escape(a.reward)}"
        if a.url: line += f"\n  🔗 {html.escape(a.url)}"
        if a.source: line += f"\n  📰 {html.escape(a.source)}"
        out.append(line)
    return "\n\n".join(out)

def _air_kb(page: int, total: int, per: int) -> InlineKeyboardMarkup:
    pages = max(1, (total + per - 1)//per)
    prev_p = max(1, page-1)
    next_p = min(pages, page+1)
    kb = [
        [
            InlineKeyboardButton("⬅️ Prev", callback_data=f"air_prev:{prev_p}"),
            InlineKeyboardButton(f"📄 {page}/{pages}", callback_data=f"air_refresh:{page}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"air_next:{next_p}"),
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data="air_refresh:1")]
    ]
    return InlineKeyboardMarkup(kb)

# --------- Scrapers (6 sumber, retry + header) ---------
def scrape_airdrops_io() -> List[Airdrop]:
    host = "airdrops.io"
    if not _dns_ok(host):
        raise RuntimeError("DNS airdrops.io tidak resolve")
    url = "https://airdrops.io/latest/"
    r = http_get(url, timeout=30); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for card in soup.select(".airdrops-list .item, .airdrops-list article"):
        title_el = card.select_one(".title, h3, h2, a")
        name = _clean_text(title_el.get_text() if title_el else None)
        if not name: continue
        href = card.select_one("a")
        reward = _clean_text((card.select_one(".reward, .prize, .subtitle") or {}).get_text() if card.select_one(".reward, .prize, .subtitle") else None)
        chain = _clean_text((card.select_one(".chain, .platform") or {}).get_text() if card.select_one(".chain, .platform") else None)
        url_item = href["href"] if href and href.has_attr("href") else url
        slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
        out.append(Airdrop(slug, name, chain, reward, url_item, "airdrops.io"))
    return out

def scrape_airdropking() -> List[Airdrop]:
    host = "airdropking.io"
    if not _dns_ok(host):
        raise RuntimeError("DNS airdropking.io tidak resolve")
    url = "https://airdropking.io/airdrops/"
    r = http_get(url, timeout=30); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for row in soup.select("article, .airdrop-card, .card"):
        t = row.select_one("h2, h3, .title, a")
        name = _clean_text(t.get_text() if t else None)
        if not name: continue
        href = row.select_one("a")
        reward = _clean_text((row.select_one(".reward, .rewards, .badge") or {}).get_text() if row.select_one(".reward, .rewards, .badge") else None)
        chain = _clean_text((row.select_one(".chain, .network") or {}).get_text() if row.select_one(".chain, .network") else None)
        url_item = href["href"] if href and href.has_attr("href") else url
        slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
        out.append(Airdrop(slug, name, chain, reward, url_item, "airdropking.io"))
    return out

def scrape_cryptorank() -> List[Airdrop]:
    host = "cryptorank.io"
    if not _dns_ok(host):
        raise RuntimeError("DNS cryptorank.io tidak resolve")
    url = "https://cryptorank.io/airdrops"
    r = http_get(url, timeout=30); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for row in soup.select("a.AirdropsListItem, .airdrop-card, article"):
        t = row.select_one("h3, .title, .AirdropsListItem__title")
        name = _clean_text(t.get_text() if t else None)
        if not name: continue
        href = row if row.has_attr("href") else row.select_one("a")
        url_item = ("https://cryptorank.io" + href["href"]) if href and href.has_attr("href") and href["href"].startswith("/") else (href["href"] if href and href.has_attr("href") else url)
        reward = _clean_text((row.select_one(".AirdropsListItem__reward, .reward") or {}).get_text() if row.select_one(".AirdropsListItem__reward, .reward") else None)
        chain = _clean_text((row.select_one(".AirdropsListItem__network, .network") or {}).get_text() if row.select_one(".AirdropsListItem__network, .network") else None)
        slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
        out.append(Airdrop(slug, name, chain, reward, url_item, "cryptorank.io"))
    return out

def scrape_coingecko_airdrops() -> List[Airdrop]:
    host = "www.coingecko.com"
    if not _dns_ok(host):
        raise RuntimeError("DNS coingecko.com tidak resolve")
    url = "https://www.coingecko.com/airdrops?locale=en"
    r = http_get(url, timeout=30); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for row in soup.select("a.tw-block, a.card, article a"):
        name = _clean_text(row.get_text())
        if not name: continue
        href = row["href"] if row.has_attr("href") else url
        url_item = ("https://www.coingecko.com" + href) if isinstance(href,str) and href.startswith("/") else href
        slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
        out.append(Airdrop(slug, name, None, None, url_item, "coingecko.com"))
    return out

def scrape_airdrops_fun() -> List[Airdrop]:
    host = "airdrops.fun"
    if not _dns_ok(host):
        raise RuntimeError("DNS airdrops.fun tidak resolve")
    url = "https://airdrops.fun/latest"
    r = http_get(url, timeout=30); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for row in soup.select("article, .card, .post"):
        t = row.select_one("h2, h3, .title, a")
        name = _clean_text(t.get_text() if t else None)
        if not name: continue
        href = row.select_one("a")
        url_item = href["href"] if href and href.has_attr("href") else url
        reward = _clean_text((row.select_one(".reward, .prize") or {}).get_text() if row.select_one(".reward, .prize") else None)
        chain = _clean_text((row.select_one(".network, .chain") or {}).get_text() if row.select_one(".network, .chain") else None)
        slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
        out.append(Airdrop(slug, name, chain, reward, url_item, "airdrops.fun"))
    return out

def scrape_airdropalert() -> List[Airdrop]:
    host = "airdropalert.com"
    if not _dns_ok(host):
        raise RuntimeError("DNS airdropalert.com tidak resolve")
    url = "https://airdropalert.com/latest-airdrops"
    r = http_get(url, timeout=30); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for row in soup.select("article, .post, .card"):
        t = row.select_one("h2, h3, .title, a")
        name = _clean_text(t.get_text() if t else None)
        if not name: continue
        href = row.select_one("a")
        url_item = href["href"] if href and href.has_attr("href") else url
        reward = _clean_text((row.select_one(".reward, .prize") or {}).get_text() if row.select_one(".reward, .prize") else None)
        chain = _clean_text((row.select_one(".network, .chain") or {}).get_text() if row.select_one(".network, .chain") else None)
        slug = re.sub(r"[^a-z0-9\-]+","-", name.lower()).strip("-")
        out.append(Airdrop(slug, name, chain, reward, url_item, "airdropalert.com"))
    return out

def scrape_airdrops_sync() -> List[Airdrop]:
    results: List[Airdrop] = []
    def _safe(run, label):
        try:
            got = run()
            results.extend(got)
            return len(got), None
        except Exception as e:
            log.warning("scrape %s gagal: %s", label, e)
            return 0, e

    report = {}
    n, e = _safe(scrape_airdrops_io, "airdrops.io");    report["airdrops.io"] = (n, e)
    n, e = _safe(scrape_airdropking, "airdropking.io"); report["airdropking.io"] = (n, e)
    n, e = _safe(scrape_cryptorank, "cryptorank.io");   report["cryptorank"] = (n, e)
    n, e = _safe(scrape_coingecko_airdrops, "coingecko"); report["coingecko"] = (n, e)
    n, e = _safe(scrape_airdrops_fun, "airdrops.fun");  report["airdrops.fun"] = (n, e)
    n, e = _safe(scrape_airdropalert, "airdropalert");  report["airdropalert"] = (n, e)

    # unique by slug, prefer yang punya reward
    mp: Dict[str, Airdrop] = {}
    for a in results:
        if (a.slug not in mp) or (a.reward and not mp[a.slug].reward):
            mp[a.slug] = a
    out = list(mp.values())
    out.sort(key=lambda x:(x.created_at, x.updated_at), reverse=True)
    # simpan laporan (optional)
    try:
        with open("air_report.json","w") as f:
            json.dump({k:v[0] for k,v in report.items()}, f)
    except Exception:
        pass
    return out

def find_airdrop(slug: str) -> Optional[Airdrop]:
    s = slug.lower()
    for a in AIRDROPS:
        if a.slug == s or a.slug in s or a.name.lower() == s:
            return a
    return None

# -------------- Commands --------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "👋 Selamat datang di AirdropCore (AI)\n\n"
        "• AI juga tanpa /ask\n"
        "• /price <coin> fiat\n"
        "• /prices btc,eth idr\n"
        "• /convert 123 sol usd\n"
        "• /setfiat idr|usd|usdt|eur\n"
        "• /airupdate [pages] [force]\n"
        "• /airdrops, /tugas <keyword>\n"
        "• /airnews, /airstatus, /airclear\n"
    )
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔄 Convert", callback_data="menu_conv")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

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
    if not client:
        await update.message.reply_text("❌ OpenAI tidak aktif."); return
    prompt = " ".join(ctx.args) or ""
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>"); return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=400, temperature=0.5
        )
        ans = resp.choices[0].message.content.strip()
        await update.message.reply_text(ans)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

# ---- Price family
async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\ncontoh: /price btc usdt")
        return
    sym = ctx.args[0]; fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await _reply_price(update, sym, fiat, silent=False)

async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth idr"); return
    parts = ctx.args[0].split(",")
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    ids = []
    for p in parts:
        cid = norm_symbol(p)
        if cid: ids.append(cid)
    if not ids:
        await update.message.reply_text("❌ Tidak ada simbol yang dikenali."); return
    data = fetch_price(ids, fiat)
    lines = []
    for p in parts:
        cid = norm_symbol(p)
        if not cid or cid not in data or fiat not in data[cid]:
            continue
        val = data[cid][fiat]
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        lines.append(f"• {p.upper()} = {fmt_price(val, fiat)}{chg_txt}")
    await update.message.reply_text("\n".join(lines) if lines else "❌ Tidak ada data.")

async def convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Format: /convert <jumlah> <coin> [fiat]\ncontoh: /convert 0.1 btc idr")
        return
    try:
        qty = float(ctx.args[0])
    except Exception:
        await update.message.reply_text("❌ Jumlah tidak valid."); return
    sym = ctx.args[1]
    fiat = (ctx.args[2] if len(ctx.args)>2 else FIAT_DEFAULT).lower()
    cid = norm_symbol(sym)
    if not cid:
        await update.message.reply_text("❌ Koin tidak dikenali."); return
    data = fetch_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        await update.message.reply_text("❌ Pair tidak tersedia."); return
    px = data[cid][fiat]
    await update.message.reply_text(f"≈ {fmt_price(qty*px, fiat)}")

# ---- Airdrop commands
async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔁 Update airdrops…")
    new = scrape_airdrops_sync()
    global AIRDROPS, AIR_LAST
    if new:
        AIRDROPS = new
        AIR_LAST = time.time()
    # laporan per sumber
    per = {}
    try:
        with open("air_report.json") as f:
            per = json.load(f)
    except Exception:
        pass
    lines = ["✅ Selesai. Terkumpul %d airdrop." % len(AIRDROPS), "Per sumber:"]
    for k,v in per.items():
        lines.append(f"• {k}: {v}")
    await msg.edit_text("\n".join(lines) + "\n\n/airdrops untuk daftar")

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not AIRDROPS:
        await update.message.reply_text("⚠️ Belum ada data. Jalankan /airupdate dulu."); return
    page = 1
    chunk = _paged(AIRDROPS, page, PER_PAGE)
    await update.message.reply_text(
        _air_list_text(chunk),
        reply_markup=_air_kb(page, len(AIRDROPS), PER_PAGE),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>"); return
    key = " ".join(ctx.args).lower()
    a = find_airdrop(key)
    if not a:
        await update.message.reply_text("❌ Tidak ketemu."); return
    txt = f"<b>{html.escape(a.name)}</b>\n"
    if a.reward: txt += f"🎁 {html.escape(a.reward)}\n"
    if a.chain:  txt += f"⛓️ {html.escape(a.chain)}\n"
    if a.url:    txt += f"🔗 {html.escape(a.url)}\n"
    txt += f"📰 {html.escape(a.source or '-')}"
    await update.message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)

# ---- Pagination callback
async def air_pager_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    m = re.match(r"^air_(prev|next|refresh):(\d+)$", data)
    if not m:
        return await q.answer()
    action, page_str = m.groups()
    page = max(1, int(page_str))
    if action == "refresh": page = 1
    total = len(AIRDROPS)
    if total == 0:
        return await q.edit_message_text("⚠️ Belum ada data.")
    chunk = _paged(AIRDROPS, page, PER_PAGE)
    await q.edit_message_text(
        _air_list_text(chunk),
        reply_markup=_air_kb(page, total, PER_PAGE),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

# ---- Menu callback (ringkas)
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = ("Contoh:\n"
               "• /price btc usdt\n"
               "• /prices btc,eth idr\n"
               "• /convert 0.25 btc idr\n"
               "Ketik bebas: 0.1 btc idr")
    elif data == "menu_conv":
        txt = "Format: /convert <jumlah> <coin> [fiat]"
    elif data == "menu_air":
        txt = "• /airupdate • /airdrops • /tugas <keyword>"
    else:
        txt = "Tulis pertanyaan apa saja (AI)."
    await q.edit_message_text(txt)

# ---- Free text router (hemat VPS: diam jika pair tak valid)
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    # 1) angka + pair, contoh: "0.1 btc idr"
    m = PAIR_PATTERN.match(text)
    if m:
        qty_s, base, quote = m.groups()
        cid = norm_symbol(base)
        if not cid:
            return  # diam
        fiat = (quote or FIAT_DEFAULT).lower()
        data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]:
            return  # diam (hemat)
        px = data[cid][fiat]
        qty = float(qty_s) if qty_s else 1.0
        reply = f"💰 {qty:g} {base.upper()} ≈ {fmt_price(px*qty, fiat)}"
        return await update.message.reply_text(reply)

    # 2) simple pair: "btc idr" / "btc"
    m2 = SIMPLE_PAIR.match(text)
    if m2:
        base, quote = m2.groups()
        cid = norm_symbol(base)
        if not cid:
            return  # diam
        fiat = (quote or FIAT_DEFAULT).lower()
        data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]:
            return  # diam
        val = data[cid][fiat]
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        return await update.message.reply_text(f"💰 {base.upper()} = {fmt_price(val, fiat)}{chg_txt}")

    # 3) fallback AI
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": text}],
                max_tokens=240, temperature=0.6
            )
            ans = resp.choices[0].message.content.strip()
            await update.message.reply_text(ans)
        except Exception as e:
            log.warning("AI fallback error: %s", e)

# -------------- Runner --------------
def main():
    _refresh_symbols(force=True)  # warm cache
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("setfiat", setfiat))

    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("convert", convert))

    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))

    app.add_handler(CallbackQueryHandler(air_pager_cb, pattern=r"^air_(prev|next|refresh):\d+$"))
    app.add_handler(CallbackQueryHandler(on_menu_cb))  # menu_*

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

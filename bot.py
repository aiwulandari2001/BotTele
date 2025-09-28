# -*- coding: utf-8 -*-
"""
AirdropCore (Ultra Pro) — Telegram Bot
Fiturnya:
- Harga/konversi ribuan kripto (CoinGecko) + TTL cache
- AI bebas tanpa /ask (fallback), sistem prompt “pro”
- Scraper airdrop multi-sumber + daftar paging + detail tugas
- Hemat VPS: jika pair tak valid → tidak balas
Python 3.8+ compatible.
"""

import os, re, json, time, asyncio, logging, math, html
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, asdict

import httpx
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ========== Konfigurasi dasar ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()  # opsi

BRAND = "AirdropCore"
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

DATA_DIR = os.getenv("DATA_DIR", "data")
COINMAP_FILE = os.path.join(DATA_DIR, "coins.json")
AIRDROPS_FILE = os.path.join(DATA_DIR, "airdrops.json")

COINGECKO = "https://api.coingecko.com/api/v3"
HTTP_TIMEOUT = 20
PRICE_TTL = 60          # detik
COINMAP_TTL = 6 * 3600  # 6 jam

# Sumber scraper yang aman (hindari domain yg sering 403/DNS)
SCRAPE_SOURCES = ("airdrops.io", "airdropalert.com", "airdrops.fun")

# ========== Logging ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# ========== Klien HTTP Async global ==========
HTTP: Optional[httpx.AsyncClient] = None

# ========== OpenAI (opsional) ==========
try:
    from openai import OpenAI
    OAI: Optional[OpenAI] = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    if OAI:
        log.info("OpenAI aktif")
except Exception as e:
    OAI = None
    log.warning("OpenAI tidak tersedia: %s", e)

AI_SYSTEM = (
    f"Kamu adalah asisten kripto & airdrop super profesional untuk {BRAND}. "
    "Jawablah singkat-padat, gunakan poin/emoji seperlunya, sebutkan sumber jika relevan. "
    "Jika ditanya harga/konversi, cukup beri angka & ringkas. Jika diminta rangkum, ringkas elegan."
)

# ========== Util json & storage ==========
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

ensure_dir(DATA_DIR)

def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ========== Dataclass Airdrop ==========
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None
    tasks: Optional[List[str]] = None

# Memori runtime
COINMAP: Dict[str, Dict[str, Any]] = {
    "_ts": 0,
    "by_symbol": {},   # "btc" -> {"id":"bitcoin","name":"Bitcoin","symbol":"btc"}
}
PRICE_CACHE: Dict[str, Dict[str, Any]] = {}  # key=f"{ids}|{fiat}" -> {"_ts":..., "data": {...}}
AIRDROPS: Dict[str, Any] = load_json(AIRDROPS_FILE, {"_ts":0, "items":[]})

# ========== Coin map (CoinGecko) ==========
async def refresh_coinmap_if_needed() -> None:
    # pakai TTL dan file cache
    now = time.time()
    if (now - COINMAP["_ts"]) < COINMAP_TTL and COINMAP["by_symbol"]:
        return
    # coba muat dari file dulu
    filedata = load_json(COINMAP_FILE, {})
    if filedata and (now - filedata.get("_ts", 0)) < COINMAP_TTL and filedata.get("by_symbol"):
        COINMAP.update(filedata)
        return
    # fetch dari API
    try:
        assert HTTP is not None
        url = f"{COINGECKO}/coins/list?include_platform=false"
        r = await HTTP.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        items = r.json()
        by_symbol = {}
        for it in items:
            sym = (it.get("symbol") or "").lower()
            if not sym:
                continue
            # kalau simbol bentrok, prioritaskan yg market cap besar? Tidak ada data—ambil pertama
            if sym not in by_symbol:
                by_symbol[sym] = {"id": it["id"], "name": it.get("name",""), "symbol": sym}
        COINMAP["_ts"] = now
        COINMAP["by_symbol"] = by_symbol
        save_json(COINMAP_FILE, COINMAP)
        log.info("Coin map terbarui (%d simbol)", len(by_symbol))
    except Exception as e:
        log.warning("Gagal refresh coin map: %s", e)

def norm_symbol_to_id(sym: str) -> Optional[str]:
    sym = (sym or "").lower().strip()
    if not sym:
        return None
    d = COINMAP.get("by_symbol", {})
    return d.get(sym, {}).get("id")

# ========== Harga / Konversi ==========
ALLOWED_FIAT = {"usd","usdt","idr","eur","jpy","gbp","inr"}

async def get_simple_price(ids: List[str], fiat: str) -> Optional[Dict[str, Any]]:
    if not ids:
        return None
    fiat = fiat.lower()
    cache_key = f"{','.join(ids)}|{fiat}"
    now = time.time()
    c = PRICE_CACHE.get(cache_key)
    if c and now - c.get("_ts", 0) < PRICE_TTL:
        return c["data"]
    try:
        assert HTTP is not None
        params = {
            "ids": ",".join(ids),
            "vs_currencies": fiat,
            "include_24hr_change": "true"
        }
        r = await HTTP.get(f"{COINGECKO}/simple/price", params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        PRICE_CACHE[cache_key] = {"_ts": now, "data": data}
        return data
    except Exception as e:
        log.warning("get_simple_price gagal: %s", e)
        return None

def fmt_price(x: float, fiat: str) -> str:
    if x >= 1:
        s = f"{x:,.2f}"
    elif x >= 0.01:
        s = f"{x:,.4f}"
    else:
        s = f"{x:.8f}".rstrip("0")
    return f"{s} {fiat.upper()}"

# ========== Parsing teks bebas untuk price/convert ==========
RE_AMOUNT_PAIR = re.compile(r"^\s*(?P<amt>\d+(?:[.,]\d+)?)\s+(?P<base>[A-Za-z0-9]+)\s+(?P<fiat>[A-Za-z]{2,6})\s*$")
RE_PAIR = re.compile(r"^\s*(?P<base>[A-Za-z0-9]{2,15})\s+(?P<fiat>[A-Za-z]{2,6})\s*$")
RE_CSV = re.compile(r"^\s*(?P<bases>[A-Za-z0-9, ]+)\s+(?P<fiat>[A-Za-z]{2,6})\s*$")

# ========== Scraper helper ==========
UA_HEADERS = {
    "User-Agent": f"{BRAND}-Bot/1.0 (+https://t.me/) Python-httpx"
}

def _clean_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = " ".join(s.split())
    return s.strip() or None

async def scrape_airdrops_io() -> List[Airdrop]:
    url = "https://airdrops.io/latest/"
    try:
        assert HTTP is not None
        r = await HTTP.get(url, headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        out: List[Airdrop] = []
        for it in soup.select(".airdrops-list .item"):
            ttl = _clean_text(it.select_one(".title, h3, h2").get_text() if it.select_one(".title, h3, h2") else None)
            if not ttl: 
                continue
            href = it.select_one("a")
            reward = _clean_text((it.select_one(".reward, .prize, .subtitle") or {}).get_text() if it.select_one(".reward, .prize, .subtitle") else None)
            chain  = _clean_text((it.select_one(".chain, .platform") or {}).get_text() if it.select_one(".chain, .platform") else None)
            url_item = href.get("href") if href and href.has_attr("href") else url
            slug = ttl.lower().replace(" ","-")
            out.append(Airdrop(slug=slug, name=ttl, chain=chain, reward=reward, url=url_item, source="airdrops.io"))
        return out
    except Exception as e:
        log.warning("scrape airdrops.io gagal: %s", e)
        return []

async def scrape_airdropalert() -> List[Airdrop]:
    url = "https://airdropalert.com/latest-airdrops"
    try:
        assert HTTP is not None
        r = await HTTP.get(url, headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        out: List[Airdrop] = []
        cards = soup.select("article, .airdrop, .card, .blog-post")
        if not cards:
            cards = soup.select("a[href*='/airdrop/']")
        for it in cards:
            # title
            ttl_el = it.select_one("h2, h3, .title, .entry-title") or it
            ttl = _clean_text(ttl_el.get_text())
            if not ttl:
                continue
            href = it.select_one("a")
            url_item = href.get("href") if (href and href.has_attr("href")) else url
            reward = _clean_text((it.select_one(".reward, .prize, .subtitle, .post-description") or {}).get_text() if it.select_one(".reward, .prize, .subtitle, .post-description") else None)
            chain = _clean_text((it.select_one(".chain, .platform, .network") or {}).get_text() if it.select_one(".chain, .platform, .network") else None)
            slug = ttl.lower().replace(" ","-")
            out.append(Airdrop(slug=slug, name=ttl, chain=chain, reward=reward, url=url_item, source="airdropalert.com"))
        return out
    except Exception as e:
        log.warning("scrape airdropalert gagal: %s", e)
        return []

async def scrape_airdrops_fun() -> List[Airdrop]:
    url = "https://airdrops.fun/"
    try:
        assert HTTP is not None
        r = await HTTP.get(url, headers=UA_HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        out: List[Airdrop] = []
        for it in soup.select("article, .card, .post, .airdrops-item"):
            ttl = _clean_text((it.select_one("h2, h3, .title") or it).get_text())
            if not ttl: continue
            href = it.select_one("a")
            url_item = href.get("href") if (href and href.has_attr("href")) else url
            reward = _clean_text((it.select_one(".reward, .subtitle, .entry-summary") or {}).get_text() if it.select_one(".reward, .subtitle, .entry-summary") else None)
            chain = _clean_text((it.select_one(".chain, .platform, .network") or {}).get_text() if it.select_one(".chain, .platform, .network") else None)
            slug = ttl.lower().replace(" ","-")
            out.append(Airdrop(slug=slug, name=ttl, chain=chain, reward=reward, url=url_item, source="airdrops.fun"))
        return out
    except Exception as e:
        log.warning("scrape airdrops.fun gagal: %s", e)
        return []

async def scrape_all() -> Tuple[List[Airdrop], Dict[str,int]]:
    results: List[Airdrop] = []
    per_src: Dict[str,int] = {}
    for fn, name in ((scrape_airdrops_io, "airdrops.io"),
                     (scrape_airdropalert, "airdropalert.com"),
                     (scrape_airdrops_fun, "airdrops.fun")):
        arr = await fn()
        results.extend(arr)
        per_src[name] = len(arr)
    # dedupe by slug; pilih yang punya reward dulu
    mp: Dict[str, Airdrop] = {}
    for a in results:
        k = a.slug
        if k not in mp:
            mp[k] = a
        else:
            if (a.reward and not mp[k].reward):
                mp[k] = a
    out = list(mp.values())
    # sort by name
    out.sort(key=lambda x: x.name.lower())
    return out, per_src

# ========== Telegram Helpers ==========
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔄 Convert", callback_data="menu_convert")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ])

def paginate(items: List[Airdrop], page: int, size: int = 10) -> Tuple[List[Airdrop], int]:
    n = len(items)
    pages = max(1, math.ceil(n/size))
    page = max(1, min(page, pages))
    start = (page-1)*size
    return items[start:start+size], pages

def airdrop_list_text(items: List[Airdrop], page: int, pages: int) -> str:
    if not items:
        return "Belum ada data airdrop. Jalankan /airupdate."
    lines = [f"Daftar Airdrop (page {page}/{pages}):"]
    for i, a in enumerate(items, 1):
        nm = html.escape(a.name)
        src = a.source or "-"
        rw = a.reward or "-"
        lines.append(f"{i}. {nm} • {src} • {rw}")
    lines.append("\nGunakan /tugas <keyword> untuk detail.")
    return "\n".join(lines)

# ========== Command Handlers ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        f"Selamat datang di {BRAND} (AI ultra-pro).\n\n"
        "Perintah ringkas:\n"
        "• /price <coin> <fiat>\n"
        "• /prices btc,eth idr\n"
        "• /convert 0.25 btc usd\n"
        "• /setfiat idr|usd|usdt|eur\n"
        "• /airupdate [pages] [force]\n"
        "• /airdrops  — daftar + tombol Next/Prev\n"
        "• /tugas <keyword> — detail airdrop\n\n"
        "Ketik bebas (tanpa /ask) untuk tanya AI."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=kb_main())

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bantuan cepat:\n"
        "/price btc usdt — harga 1 koin\n"
        "/prices btc,eth idr — beberapa koin\n"
        "/convert 12.5 sol usd — konversi\n"
        "/setfiat usd — ganti default fiat\n"
        "/airupdate 1 true — update airdrop (paksa)\n"
        "/airdrops — lihat daftar (Next/Prev)\n"
        "/tugas <keyword> — detail airdrop\n"
        "Teks bebas → AI."
    )

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(f"FIAT saat ini: {FIAT_DEFAULT.upper()}\nFormat: /setfiat idr|usd|usdt|eur")
        return
    fiat = ctx.args[0].lower()
    if fiat not in ALLOWED_FIAT:
        await update.message.reply_text("❌ Fiat tidak didukung.")
        return
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default di-set ke {fiat.upper()}")

# ---- Harga satu koin
async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 1:
        await update.message.reply_text("Format: /price <coin> [fiat]\ncontoh: /price btc usdt")
        return
    base = ctx.args[0].lower()
    fiat = (ctx.args[1].lower() if len(ctx.args) > 1 else FIAT_DEFAULT)
    await reply_price(update, base, fiat)

# ---- Harga banyak koin: /prices btc,eth idr
async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth,sol [fiat]")
        return
    parts = " ".join(ctx.args).split()
    bases = [b.strip().lower() for b in parts[0].split(",") if b.strip()]
    fiat = (parts[1].lower() if len(parts) > 1 else FIAT_DEFAULT)
    # validasi pair; kalau kosong, diam (hemat VPS)
    ids = [norm_symbol_to_id(b) for b in bases]
    ids = [i for i in ids if i]
    if not ids or fiat not in ALLOWED_FIAT:
        return
    data = await get_simple_price(ids, fiat)
    if not data:
        return
    lines = []
    rev = {v["id"]: k for k, v in COINMAP["by_symbol"].items()}
    for i in ids:
        sym = rev.get(i, "").upper()
        p = data.get(i, {}).get(fiat)
        if p is None:
            continue
        chg = data.get(i, {}).get(f"{fiat}_24h_change")
        chg_txt = f" (24h {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        lines.append(f"• {sym} = {fmt_price(p, fiat)}{chg_txt}")
    if lines:
        await update.message.reply_text("\n".join(lines))

# ---- Konversi /convert 0.25 btc usd
async def convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <jumlah> <coin> <fiat>\ncontoh: /convert 0.25 btc usd")
        return
    amt = ctx.args[0].replace(",", ".")
    try:
        amount = float(amt)
    except Exception:
        return
    base = ctx.args[1].lower()
    fiat = ctx.args[2].lower()
    await reply_convert(update, amount, base, fiat)

async def reply_price(update: Update, base: str, fiat: str):
    await refresh_coinmap_if_needed()
    coin_id = norm_symbol_to_id(base)
    if not coin_id or fiat not in ALLOWED_FIAT:
        return  # diam
    data = await get_simple_price([coin_id], fiat)
    if not data:
        return
    price_val = data.get(coin_id, {}).get(fiat)
    if price_val is None:
        return
    chg = data.get(coin_id, {}).get(f"{fiat}_24h_change")
    chg_txt = f" (24h {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_text(f"💰 {base.upper()} = {fmt_price(price_val, fiat)}{chg_txt}")

async def reply_convert(update: Update, amount: float, base: str, fiat: str):
    await refresh_coinmap_if_needed()
    coin_id = norm_symbol_to_id(base)
    if not coin_id or fiat not in ALLOWED_FIAT:
        return
    data = await get_simple_price([coin_id], fiat)
    if not data:
        return
    p = data.get(coin_id, {}).get(fiat)
    if p is None:
        return
    val = amount * float(p)
    await update.message.reply_text(f"🔄 {amount:g} {base.upper()} ≈ {fmt_price(val, fiat)}")

# ---- Airdrop update / daftar / tugas
async def airupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # /airupdate [pages] [force]
    pages = int(ctx.args[0]) if (len(ctx.args) >= 1 and ctx.args[0].isdigit()) else 1
    force = str(ctx.args[1]).lower() in {"1","true","yes"} if len(ctx.args) >= 2 else False

    last_ts = AIRDROPS.get("_ts", 0)
    if not force and (time.time() - last_ts) < 180:  # 3 menit cooldown
        await update.message.reply_text("⏳ Terlalu sering. Coba lagi beberapa menit.")
        return

    msg = await update.message.reply_text(f"🔄 Update airdrops (pages={pages}, force={force})…")
    items_all: List[Airdrop] = []
    per_src_total: Dict[str,int] = {}
    for _ in range(pages):
        arr, per_src = await scrape_all()
        items_all.extend(arr)
        for k,v in per_src.items():
            per_src_total[k] = per_src_total.get(k,0) + v

    # dedupe & simpan
    seen = {}
    final: List[Dict[str, Any]] = []
    for a in items_all:
        if a.slug in seen:
            continue
        seen[a.slug] = 1
        final.append(asdict(a))
    AIRDROPS["_ts"] = time.time()
    AIRDROPS["items"] = final
    save_json(AIRDROPS_FILE, AIRDROPS)

    lines = [f"✅ Selesai. Terkumpul {len(final)} airdrop.", "Per sumber:"]
    for k in SCRAPE_SOURCES:
        lines.append(f"• {k}: {per_src_total.get(k,0)}")
    await msg.edit_text("\n".join(lines) + "\n\n/airdrops untuk daftar, /airnews untuk yang baru.")

# state paging per chat (in-mem)
AIRPAGE: Dict[int, int] = {}

async def airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    AIRPAGE[chat_id] = 1
    await send_airdrops_page(update, ctx, chat_id, 1)

async def send_airdrops_page(update_or_cb, ctx, chat_id: int, page: int):
    items = [Airdrop(**it) for it in AIRDROPS.get("items", [])]
    chunk, pages = paginate(items, page, 10)
    text = airdrop_list_text(chunk, page, pages)

    kb = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"airpage:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"airpage:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("🔄 Refresh", callback_data="airrefresh")])

    markup = InlineKeyboardMarkup(kb)
    if isinstance(update_or_cb, Update) and update_or_cb.message:
        await update_or_cb.message.reply_text(text, reply_markup=markup)
    else:
        q = update_or_cb.callback_query
        await q.edit_message_text(text, reply_markup=markup)

async def tugas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>")
        return
    key = " ".join(ctx.args).lower()
    items = AIRDROPS.get("items", [])
    found = None
    for it in items:
        if key in it["slug"] or key in it["name"].lower():
            found = it; break
    if not found:
        await update.message.reply_text("❌ Tidak ketemu.")
        return
    # Detail “tugas” = ringkas dari reward/chain/url
    nm = found["name"]; src = found.get("source","-")
    rw = found.get("reward") or "-"
    ch = found.get("chain") or "-"
    url = found.get("url") or "-"
    txt = (
        f"🧩 {html.escape(nm)}\n"
        f"• Sumber: {src}\n"
        f"• Chain/Platform: {ch}\n"
        f"• Reward: {rw}\n"
        f"• Link: {url}\n\n"
        "Tip: Baca halaman sumber untuk step-by-step task (follow, quest, form, testnet, dsb)."
    )
    await update.message.reply_text(txt)

async def airnews(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # tampilkan 10 terbaru
    items = [Airdrop(**it) for it in AIRDROPS.get("items", [])][:10]
    if not items:
        await update.message.reply_text("Belum ada data. /airupdate dulu.")
        return
    lines = ["✨ Airdrop terbaru:"]
    for a in items:
        nm = html.escape(a.name)
        lines.append(f"• {nm} — {a.source or '-'}")
    await update.message.reply_text("\n".join(lines))

async def airstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = len(AIRDROPS.get("items", []))
    last = AIRDROPS.get("_ts", 0)
    ago = int(time.time()-last) if last else None
    lines = [f"🩺 Airdrops: {n} item"]
    if ago is not None:
        lines.append(f"• Terakhir update: {ago}s lalu")
    await update.message.reply_text("\n".join(lines))

async def airclear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    AIRDROPS["_ts"] = 0
    AIRDROPS["items"] = []
    save_json(AIRDROPS_FILE, AIRDROPS)
    await update.message.reply_text("🧹 Cache airdrop dibersihkan.")

# ---- Callback (menu & paging)
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    chat_id = update.effective_chat.id
    if data == "menu_price":
        await q.edit_message_text("Contoh:\n• /price btc usdt\n• btc usd (ketik bebas)\n• 0.25 eth idr (konversi)")
    elif data == "menu_convert":
        await q.edit_message_text("Contoh konversi:\n/convert 1.2 sol usd\nAtau ketik: 1.2 sol usd")
    elif data == "menu_air":
        await send_airdrops_page(update, ctx, chat_id, AIRPAGE.get(chat_id,1))
    elif data == "menu_ai":
        await q.edit_message_text("Tanya apa saja, tanpa /ask. Saya jawab gaya profesional.")
    elif data.startswith("airpage:"):
        pg = int(data.split(":")[1])
        AIRPAGE[chat_id] = pg
        await send_airdrops_page(update, ctx, chat_id, pg)
    elif data == "airrefresh":
        await send_airdrops_page(update, ctx, chat_id, AIRPAGE.get(chat_id,1))

# ========== Router teks bebas ==========
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt:
        return

    # 1) amount pair: "0.5 btc idr"
    m = RE_AMOUNT_PAIR.match(txt)
    if m:
        amt = float(m.group("amt").replace(",", "."))
        base = m.group("base").lower()
        fiat = m.group("fiat").lower()
        await reply_convert(update, amt, base, fiat)
        return

    # 2) single pair "btc usd"
    m2 = RE_PAIR.match(txt)
    if m2:
        base = m2.group("base").lower()
        fiat = m2.group("fiat").lower()
        await reply_price(update, base, fiat)
        return

    # 3) comma list "btc,eth idr"
    m3 = RE_CSV.match(txt)
    if m3:
        bases = [b.strip().lower() for b in m3.group("bases").split(",") if b.strip()]
        fiat = m3.group("fiat").lower()
        ids = []
        await refresh_coinmap_if_needed()
        for b in bases:
            i = norm_symbol_to_id(b)
            if i: ids.append(i)
        if ids and fiat in ALLOWED_FIAT:
            data = await get_simple_price(ids, fiat)
            if data:
                lines = []
                rev = {v["id"]: k for k, v in COINMAP["by_symbol"].items()}
                for i in ids:
                    sym = rev.get(i, "").upper()
                    p = data.get(i, {}).get(fiat)
                    if p is None: continue
                    chg = data.get(i, {}).get(f"{fiat}_24h_change")
                    chg_txt = f" (24h {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
                    lines.append(f"• {sym} = {fmt_price(p, fiat)}{chg_txt}")
                if lines:
                    await update.message.reply_text("\n".join(lines))
        return

    # 4) AI fallback (hemat: batasi 3–300 char)
    if OAI and 3 <= len(txt) <= 300:
        try:
            await update.message.chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            resp = OAI.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=280,
                temperature=0.5,
                messages=[
                    {"role":"system","content": AI_SYSTEM},
                    {"role":"user","content": txt}
                ],
            )
            ans = (resp.choices[0].message.content or "").strip()
            if ans:
                await update.message.reply_text(ans)
        except Exception as e:
            log.warning("AI error: %s", e)
    # else: diam

# ========== Main ==========
async def on_start(app):
    global HTTP
    HTTP = httpx.AsyncClient(http2=True, timeout=HTTP_TIMEOUT)
    # warm coinmap
    try:
        await refresh_coinmap_if_needed()
    except Exception:
        pass
    log.info("Bot siap.")

async def on_stop(app):
    global HTTP
    if HTTP:
        await HTTP.aclose()
        HTTP = None
    log.info("HTTP client ditutup.")

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN belum diisi di environment.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.post_init = on_start
    app.post_shutdown = on_stop

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("convert", convert))

    app.add_handler(CommandHandler("airupdate", airupdate))
    app.add_handler(CommandHandler("airdrops", airdrops))
    app.add_handler(CommandHandler("tugas", tugas))
    app.add_handler(CommandHandler("airnews", airnews))
    app.add_handler(CommandHandler("airstatus", airstatus))
    app.add_handler(CommandHandler("airclear", airclear))

    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Polling…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

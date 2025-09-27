# bot.py — AirdropCore Super Bot (Crypto + Airdrops + AI + Pagination)
import os, re, time, math, json, logging, html
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ParseMode
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
# Executor (scrape di thread)
# =====================
executor = ThreadPoolExecutor(max_workers=4)

# =====================
# Regex input bebas (harga/convert)
# =====================
RX_AMOUNT_PAIR = re.compile(r"^\s*([\d\.,]+)\s+([a-z0-9\-]+)\s+([a-z]{2,6})\s*$", re.I)   # "0.1 btc idr"
RX_PAIR_ONLY   = re.compile(r"^\s*([a-z0-9\-]{2,15})[/\s]+([a-z]{2,6})\s*$", re.I)        # "btc/usdt" / "btc usd"
RX_PRICE_WORD  = re.compile(r"^(?:harga|price)\s+([a-z0-9\-]{2,15})(?:[/\s]+([a-z]{2,6}))?$", re.I)

# =====================
# CoinGecko resolver: symbol -> id (ribuan koin)
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
    # contoh override lain:
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
            if not sym or not cid: continue
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
        import math
        digits = max(2, min(8, -int(math.floor(math.log10(v)))+2))
        return f"{v:.{digits}f} {fiat.upper()}"
    return f"{v:,.2f} {fiat.upper()}"

# =====================
# Airdrop cache & scraper
# =====================
AIR_CACHE = Path("airdrops_cache.json")
AIRDROPS: List[dict] = []
PAGE_SIZE = 15

def load_airdrops_from_cache() -> List[dict]:
    global AIRDROPS
    if AIR_CACHE.exists():
        try:
            AIRDROPS = json.loads(AIR_CACHE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Gagal baca cache airdrops (fallback kosong)")
            AIRDROPS = []
    else:
        AIRDROPS = []
    return AIRDROPS

def save_airdrops_to_cache(items: List[dict]) -> None:
    try:
        AIR_CACHE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        log.exception("Gagal simpan cache airdrops")

HEADERS = {"User-Agent": "Mozilla/5.0 (AirdropCoreBot)"}

def _fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text

def _clean(text: str) -> str:
    return " ".join((text or "").split())

def parse_generic_listing(html_text: str, base_url: str) -> List[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    for a in soup.select("a[href]"):
        title = _clean(a.get_text(" ", strip=True))
        href  = a.get("href") or ""
        if not title or len(title) < 5: continue
        if href.startswith("/"): href = base_url.rstrip("/") + href
        low = (title + " " + href).lower()
        if any(k in low for k in ["airdrop", "quest", "reward", "galxe", "campaign"]):
            items.append({
                "name": title[:100],
                "slug": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80],
                "url": href,
                "source": base_url,
                "status": "",  # bisa ditebak kalau mau
            })
    return items

def scrape_all_sources() -> List[dict]:
    sources = [
        ("https://airdrops.io/latest", "https://airdrops.io"),
        ("https://coinmarketcap.com/airdrop/", "https://coinmarketcap.com"),
        ("https://cryptorank.io/airdrops", "https://cryptorank.io"),
        ("https://galxe.com/explore", "https://galxe.com"),
    ]
    collected: List[dict] = []
    for url, base in sources:
        try:
            html_text = _fetch(url)
            items = parse_generic_listing(html_text, base)
            collected.extend(items)
        except Exception as e:
            log.warning("Gagal scraping %s: %s", url, e)
    # dedupe by (slug,url)
    uniq: Dict[Tuple[str,str], dict] = {}
    for it in collected:
        key = (it.get("slug",""), it.get("url",""))
        if key not in uniq:
            uniq[key] = it
    items = list(uniq.values())
    items.sort(key=lambda x: x.get("name","").lower())
    return items[:300]

def scrape_tasks(url: str) -> List[str]:
    """Ambil kemungkinan daftar tugas dari halaman sumber + ringkas (jika AI aktif)."""
    try:
        r = requests.get(url, timeout=25, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        raw = [x.get_text(" ", strip=True) for x in soup.select("li, p")]
        steps = [s for s in raw if any(k in s.lower() for k in [
            "join","follow","retweet","repost","discord","galxe","zealy",
            "quest","task","mission","bridge","swap","mint","testnet","submit"
        ])]
        if client and steps:
            joined = "\n".join(steps[:20])
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": f"Ringkas jadi checklist tugas singkat dan jelas:\n{joined}"}],
                max_tokens=320, temperature=0.2
            )
            txt = resp.choices[0].message.content.strip().splitlines()
            steps = [s.strip("•- ") for s in txt if s.strip()]
        return steps or ["⚠️ Tidak ada tugas terdeteksi dari halaman sumber."]
    except Exception as e:
        log.exception("scrape_tasks error")
        return [f"❌ Error mengambil tugas: {e}"]

# =====================
# UI Helpers (Airdrops + Pagination)
# =====================
def format_airdrop_list(items: List[dict], page: int, page_size: int = PAGE_SIZE) -> str:
    total = len(items)
    if total == 0:
        return "Belum ada data airdrop."
    pages = max(1, (total + page_size - 1)//page_size)
    page = max(1, min(page, pages))
    start = (page-1)*page_size
    subset = items[start:start+page_size]
    lines = [f"Daftar Airdrop (Hal {page}/{pages}) — total {total}:", ""]
    for i, a in enumerate(subset, start=start+1):
        name = a.get("name") or a.get("slug","(no-name)")
        src  = a.get("source","-")
        st   = a.get("status","")
        tail = f" [{st}]" if st else ""
        lines.append(f"{i}. {name} — {src}{tail}")
    lines.append("\n• Cari detail: `/airdrops <keyword>` atau `/tugas <keyword>`")
    return "\n".join(lines)

def page_kb(current: int, total_items: int, page_size: int = PAGE_SIZE) -> InlineKeyboardMarkup:
    pages = max(1, (total_items + page_size - 1)//page_size)
    prev_p = max(1, current-1)
    next_p = min(pages, current+1)
    buttons = []
    if pages > 1:
        buttons.append([
            InlineKeyboardButton("⬅️ Prev", callback_data=f"airpage:{prev_p}"),
            InlineKeyboardButton(f"{current}/{pages}", callback_data="airpage:noop"),
            InlineKeyboardButton("Next ➡️", callback_data=f"airpage:{next_p}"),
        ])
    return InlineKeyboardMarkup(buttons) if buttons else InlineKeyboardMarkup([])

# =====================
# Handlers (Commands)
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
        "• /airdrops, /airdrops list|all, /tugas <keyword>, /airupdate\n"
        "• /ask <pertanyaan> (AI)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu()
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah:\n"
        "• /price <coin> [fiat]\n"
        "• /convert <amt> <coin> <fiat>\n"
        "• /airdrops  (Top 15 dgn pagination)\n"
        "• /airdrops list  (50 item) | /airdrops all (100 item)\n"
        "• /tugas <keyword>  (ringkas tugas dari halaman sumber)\n"
        "• /airupdate  (scrape + update cache + tampil Top 20)\n"
        "• /airreload  (reload cache dari file)\n"
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
        txt = "• `/airdrops` untuk daftar + tombol Next/Prev\n• `/tugas <keyword>` untuk lihat tugas"
    else:
        txt = "AI: `/ask <pertanyaan>`"
    await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)

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

# ----- Airdrops: list/search/detail -----
def airdrop_match(q: str, a: dict) -> bool:
    q = q.lower().strip()
    hay = " ".join([a.get("slug",""), a.get("name",""), a.get("status",""), a.get("source","")]).lower()
    return all(part in hay for part in q.split())

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).strip().lower()
    items = AIRDROPS or load_airdrops_from_cache()

    if q in {"list", "all"}:
        limit = 50 if q == "list" else 100
        text = format_airdrop_list(items[:limit], page=1, page_size=limit)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    if not q:
        # halaman 1 + tombol
        total = len(items)
        text = format_airdrop_list(items, page=1, page_size=PAGE_SIZE)
        kb = page_kb(1, total, PAGE_SIZE)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)
        return

    # cari
    hits = [a for a in items if airdrop_match(q, a)]
    if not hits:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{q}'."); return
    k = min(30, len(hits))
    lines = [f"Hasil untuk '{q}' ({k}/{len(hits)}):", ""]
    for i, a in enumerate(hits[:k], start=1):
        name = a.get("name") or a.get("slug","(no-name)")
        src  = a.get("source","-")
        st   = a.get("status","")
        tail = f" [{st}]" if st else ""
        lines.append(f"{i}. {name} — {src}{tail}")
    lines.append("\nTambah kata `detail` dengan /tugas untuk lihat misinya.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

# Pagination callback
async def airpage_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""
    if not data.startswith("airpage:"):
        await q.answer(); return
    _, val = data.split(":", 1)
    if val == "noop":
        await q.answer(); return
    try:
        page = int(val)
    except:
        page = 1
    items = AIRDROPS or load_airdrops_from_cache()
    text = format_airdrop_list(items, page=page, page_size=PAGE_SIZE)
    kb = page_kb(page, len(items), PAGE_SIZE)
    await q.answer()
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb, disable_web_page_preview=True)

# ----- Tugas (ambil dari URL sumber + ringkas AI) -----
async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>\ncontoh: /tugas monad")
        return
    key = " ".join(ctx.args).lower()
    items = AIRDROPS or load_airdrops_from_cache()
    hits = [a for a in items if airdrop_match(key, a)]
    if not hits:
        await update.message.reply_text(f"❌ Airdrop '{key}' tidak ditemukan. Coba `/airdrops {key}` dulu.")
        return
    a = hits[0]
    url = a.get("url")
    name = a.get("name") or a.get("slug","(no-name)")
    await update.message.reply_text(f"🔎 Mengambil tugas untuk *{name}*…", parse_mode=ParseMode.MARKDOWN)
    steps = scrape_tasks(url) if url else ["URL sumber tidak tersedia."]
    await update.message.reply_text(
        "📋 Tugas terdeteksi:\n" + "\n".join(f"• {s}" for s in steps),
        disable_web_page_preview=True
    )

# ----- Airupdate & Airreload -----
async def airreload_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    load_airdrops_from_cache()
    await update.message.reply_text(f"♻️ Cache dimuat. Total airdrop: {len(AIRDROPS)}")

def format_airdrop_preview(items: List[dict], limit: int = 20) -> str:
    if not items: return "Belum ada data airdrop."
    lines = [f"Daftar Airdrop Terbaru (Top {min(limit, len(items))}/{len(items)}):", ""]
    for i, a in enumerate(items[:limit], start=1):
        name = a.get("name") or a.get("slug","(no-name)")
        src  = a.get("source","-")
        st   = a.get("status","")
        tail = f" [{st}]" if st else ""
        lines.append(f"{i}. {name} — {src}{tail}")
    lines.append("\nCari detail: `/airdrops <keyword>` atau `/tugas <keyword>`")
    return "\n".join(lines)

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Update airdrop… mohon tunggu (±10–30s)")
    loop = ctx.application._application_loop
    try:
        new_items: List[dict] = await loop.run_in_executor(executor, scrape_all_sources)
        if not new_items:
            await msg.edit_text("⚠️ Tidak ada data baru dari sumber."); return
        save_airdrops_to_cache(new_items)
        # refresh memori
        global AIRDROPS
        AIRDROPS = new_items
        # tampilkan daftar 20 besar langsung + tombol pagination
        preview_text = format_airdrop_preview(AIRDROPS, limit=20)
        kb = page_kb(1, len(AIRDROPS), PAGE_SIZE)
        await msg.edit_text("✅ Update selesai.\n" + preview_text, parse_mode=ParseMode.MARKDOWN,
                            reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        log.exception("airupdate error")
        await msg.edit_text(f"❌ Gagal update: {e}")

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

# ----- Router teks bebas (harga/convert/airdrop/AI) -----
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
        items = AIRDROPS or load_airdrops_from_cache()
        key = t.lower().replace("airdrop","").strip()
        if key:
            hits = [a for a in items if airdrop_match(key, a)]
            if hits:
                a = hits[0]
                name = a.get("name") or a.get("slug","(no-name)")
                url = a.get("url","")
                await update.message.reply_text(
                    f"🎁 {name}\nSumber: {a.get('source','-')}\nLink: {url}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Buka halaman", url=url)]])
                ); return
        # kalau tidak ada keyword → tampilkan halaman 1 list
        text = format_airdrop_list(items, page=1, page_size=PAGE_SIZE)
        kb = page_kb(1, len(items), PAGE_SIZE)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb); return

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

    await update.message.reply_text("Tidak paham. Coba: `btc usd`, `0.1 eth idr`, atau `/airdrops`.", parse_mode=ParseMode.MARKDOWN)

    # =====================
# Main
# =====================
def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN belum diisi di .env")

    # muat cache awal (kalau ada)
    load_airdrops_from_cache()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airreload", airreload_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    # Menu & pagination
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(airpage_cb, pattern=r"^airpage:"))
    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling berjalan…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

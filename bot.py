# bot.py
# Ultra-clean Telegram bot (PTB v20+), robust & modern
import os
import re
import json
import time
import math
import html
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import requests
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ========= Logging =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("airdropcore.bot")

# ========= Env / Config =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum diisi di environment.")

# OpenAI client (opsional)
try:
    from openai import OpenAI
    ai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    if ai_client:
        log.info("OpenAI client aktif")
    else:
        log.info("OpenAI client nonaktif (API key kosong)")
except Exception as e:
    ai_client = None
    log.warning("OpenAI client nonaktif: %s", e)

# ========= Helpers =========
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AirdropCoreBot/1.0 (+https://t.me/)"
}
HTTP_TIMEOUT = 20

def fmt_price(val: float, fiat: str) -> str:
    try:
        return f"{val:,.4f} {fiat.upper()}"
    except Exception:
        return f"{val} {fiat.upper()}"

def clean_text(s: Optional[str]) -> str:
    return " ".join((s or "").split())

# Normalisasi simbol → id coingecko (subset populer + fallback)
SYMBOL_MAP = {
    "btc":"bitcoin","xbt":"bitcoin",
    "eth":"ethereum",
    "usdt":"tether",
    "usdc":"usd-coin","usd":"usd",  # fiat marker
    "bnb":"binancecoin",
    "sol":"solana",
    "ada":"cardano",
    "xrp":"ripple",
    "doge":"dogecoin",
    "trx":"tron",
    "ton":"the-open-network",
    "matic":"polygon",
    "dot":"polkadot",
    "ltc":"litecoin",
    "arb":"arbitrum",
    "op":"optimism",
    "avax":"avalanche-2",
    "sui":"sui",
    "sei":"sei-network",
    "wif":"dogwifhat",
    "pi":"pi-network",  # catatan: sering tidak tersedia di API harga publik
}

FIAT_ALLOWED = {"usd", "idr", "usdt", "eur"}

PAIR_TEXT = re.compile(r"^(\d+(\.\d+)?)?\s*([a-zA-Z0-9]{2,10})\s*[/\s]\s*([a-zA-Z0-9]{2,10})$")
SINGLE_PAIR = re.compile(r"^([a-zA-Z0-9]{2,10})\s+([a-zA-Z]{2,10})$")
AMOUNT_PAIR = re.compile(r"^(\d+(\.\d+)?)\s*([a-zA-Z0-9]{2,10})\s+([a-zA-Z]{2,10})$")

def norm_symbol(sym: str) -> str:
    s = (sym or "").lower()
    return SYMBOL_MAP.get(s, s)

def fetch_price(ids: List[str], fiat: str) -> Dict:
    """Coingecko simple/price wrapper. Auto-skip jika error."""
    if not ids:
        return {}
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        r = requests.get(url, params={
            "ids": ",".join(ids),
            "vs_currencies": fiat,
            "include_24hr_change": "true",
        }, headers=UA, timeout=HTTP_TIMEOUT)
        if r.status_code == 429:
            log.warning("Rate limited by Coingecko.")
            return {}
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("fetch_price error: %s", e)
        return {}

async def smart_reply_text(msg: Message, text: str, **kw):
    try:
        await msg.reply_text(text, **kw)
    except Exception:
        # fallback tanpa parse_mode jika html/markdown error
        kw.pop("parse_mode", None)
        try:
            await msg.reply_text(text, **kw)
        except Exception as e:
            log.warning("reply error: %s", e)

# ========= Airdrop Models =========
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None

AIR_CACHE: List[Airdrop] = []
AIR_LAST_UPDATED: float = 0.0
AIR_NEWS: List[str] = []  # ringkasan judul baru

# ========= Scrapers =========
def scrape_airdrops_io(pages: int = 1) -> List[Airdrop]:
    base = "https://airdrops.io/latest/"
    out: List[Airdrop] = []
    for p in range(1, pages + 1):
        url = base if p == 1 else f"https://airdrops.io/page/{p}/"
        try:
            r = requests.get(url, headers=UA, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for it in soup.select(".airdrops-list .item, article"):
                t = it.select_one(".title, h2, h3")
                name = clean_text(t.get_text() if t else None)
                if not name:
                    continue
                a = it.select_one("a")
                reward = clean_text((it.select_one(".reward,.prize,.subtitle") or {}).get_text()
                                    if it.select_one(".reward,.prize,.subtitle") else None)
                chain = clean_text((it.select_one(".chain,.platform") or {}).get_text()
                                   if it.select_one(".chain,.platform") else None)
                href = a["href"] if a and a.has_attr("href") else url
                slug = clean_text(name.lower().replace(" ", "-"))
                out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward,
                                   url=href, source="airdrops.io"))
        except Exception as e:
            log.warning("airdrops.io p=%s gagal: %s", p, e)
    return out

def scrape_airdropalert(pages: int = 1) -> List[Airdrop]:
    # Banyak halaman yang blok bot; kita lakukan parsing aman
    base = "https://airdropalert.com/latest-airdrops"
    out: List[Airdrop] = []
    try:
        r = requests.get(base, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("article, .airdrop, .post")
        for c in cards:
            t = c.select_one("h2, h3, .title, .entry-title")
            name = clean_text(t.get_text() if t else None)
            if not name:
                continue
            a = c.select_one("a")
            href = a["href"] if a and a.has_attr("href") else base
            reward = clean_text((c.select_one(".reward,.prize,.subtitle") or {}).get_text()
                                if c.select_one(".reward,.prize,.subtitle") else None)
            chain = clean_text((c.select_one(".chain,.network") or {}).get_text()
                               if c.select_one(".chain,.network") else None)
            slug = clean_text(name.lower().replace(" ", "-"))
            out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward,
                               url=href, source="airdropalert.com"))
    except Exception as e:
        log.warning("airdropalert gagal: %s", e)
    return out

def scrape_airdrops_fun(pages: int = 1) -> List[Airdrop]:
    base = "https://airdrops.fun/"
    out: List[Airdrop] = []
    try:
        r = requests.get(base, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("article, .card, .entry")
        for c in cards:
            t = c.select_one("h2, h3, .entry-title, .title")
            name = clean_text(t.get_text() if t else None)
            if not name:
                continue
            a = c.select_one("a")
            href = a["href"] if a and a.has_attr("href") else base
            reward = clean_text((c.select_one(".reward,.prize,.subtitle") or {}).get_text()
                                if c.select_one(".reward,.prize,.subtitle") else None)
            chain = clean_text((c.select_one(".chain,.network") or {}).get_text()
                               if c.select_one(".chain,.network") else None)
            slug = clean_text(name.lower().replace(" ", "-"))
            out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward,
                               url=href, source="airdrops.fun"))
    except Exception as e:
        log.warning("airdrops.fun gagal: %s", e)
    return out

def merge_unique(items: List[Airdrop]) -> List[Airdrop]:
    mp: Dict[str, Airdrop] = {}
    for a in items:
        key = a.slug or a.name.lower()
        if key not in mp:
            mp[key] = a
        else:
            # lebihkan info reward/chain/url jika kosong
            cur = mp[key]
            if (not cur.reward) and a.reward:
                cur.reward = a.reward
            if (not cur.chain) and a.chain:
                cur.chain = a.chain
            if (not cur.url) and a.url:
                cur.url = a.url
    return list(mp.values())

def scrape_all(pages: int = 1) -> Tuple[List[Airdrop], Dict[str, int]]:
    """Kumpulkan dari banyak sumber, per sumber hitung jumlahnya."""
    sources = [
        ("airdrops.io", scrape_airdrops_io),
        ("airdropalert.com", scrape_airdropalert),
        ("airdrops.fun", scrape_airdrops_fun),
    ]
    total: List[Airdrop] = []
    count_by_source: Dict[str, int] = {}
    for name, fn in sources:
        try:
            items = fn(pages=pages)
            count_by_source[name] = len(items)
            total.extend(items)
        except Exception as e:
            log.warning("scraper %s error: %s", name, e)
            count_by_source[name] = 0
    return merge_unique(total), count_by_source

# ========= Airdrop commands =========
PAGE_SIZE = 10

def build_airdrop_page(page: int) -> Tuple[str, InlineKeyboardMarkup]:
    n = len(AIR_CACHE)
    if n == 0:
        return "Belum ada data airdrop. Jalankan /airupdate dulu.", InlineKeyboardMarkup([])
    pages = max(1, math.ceil(n / PAGE_SIZE))
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    end = min(n, start + PAGE_SIZE)
    rows = []
    for i, a in enumerate(AIR_CACHE[start:end], start=1):
        line = f"{start+i}. <b>{html.escape(a.name)}</b>"
        if a.reward:
            line += f" — {html.escape(a.reward)}"
        if a.chain:
            line += f" ({html.escape(a.chain)})"
        if a.url:
            line += f"\n   <a href='{html.escape(a.url)}'>{html.escape(a.source or '')}</a>"
        rows.append(line)
    text = f"📜 <b>Daftar Airdrops</b> ({n} data)\nHalaman {page}/{pages}\n\n" + "\n\n".join(rows)
    btns = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"airpage:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"airpage:{page+1}"))
    if nav:
        btns.append(nav)
    return text, InlineKeyboardMarkup(btns)

async def cmd_airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = build_airdrop_page(1)
    await smart_reply_text(update.effective_message, text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

async def cb_airpage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        page = int((q.data or "airpage:1").split(":")[1])
    except Exception:
        page = 1
    text, kb = build_airdrop_page(page)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        # jika tidak bisa edit (mis. sudah lama), kirim baru
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

async def cmd_airupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    pages = 1
    force = False
    if ctx.args:
        try:
            pages = max(1, int(ctx.args[0]))
        except Exception:
            pages = 1
    if len(ctx.args) > 1:
        force = ctx.args[1].lower() in {"true", "1", "yes", "y"}
    await smart_reply_text(msg, f"🔄 Update airdrops (pages={pages}, force={force})…")

    global AIR_CACHE, AIR_LAST_UPDATED, AIR_NEWS
    new_list, count_by = scrape_all(pages=pages)

    # deteksi “baru” dibanding cache lama
    old_slugs = {a.slug for a in AIR_CACHE}
    new_slugs = {a.slug for a in new_list}
    fresh = list(new_slugs - old_slugs)
    AIR_NEWS = fresh[:10]

    if force or not AIR_CACHE:
        AIR_CACHE = new_list
    else:
        # merge update incremental
        merged = merge_unique(AIR_CACHE + new_list)
        AIR_CACHE = merged

    AIR_LAST_UPDATED = time.time()
    per_sumber = "\n".join([f"• {k}: {v}" for k, v in count_by.items()])

    await smart_reply_text(
        msg,
        "✅ Selesai. Terkumpul <b>%d</b> airdrop.\n<b>Per sumber:</b>\n%s\n\n/airdrops untuk daftar, /airnews untuk yang baru."
        % (len(AIR_CACHE), per_sumber),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def cmd_airnews(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not AIR_NEWS:
        await smart_reply_text(update.effective_message, "Belum ada airdrop baru. Jalankan /airupdate dulu.")
        return
    lines = "\n".join(f"• {html.escape(s)}" for s in AIR_NEWS)
    await smart_reply_text(update.effective_message, f"🆕 <b>Baru terdeteksi</b>:\n{lines}", parse_mode=ParseMode.HTML)

# ========= Price / Convert =========
async def cmd_setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await smart_reply_text(
            update.effective_message,
            f"FIAT saat ini: <b>{FIAT_DEFAULT.upper()}</b>\nFormat: /setfiat idr|usd|usdt|eur",
            parse_mode=ParseMode.HTML,
        )
        return
    fiat = ctx.args[0].lower()
    if fiat not in FIAT_ALLOWED:
        await smart_reply_text(update.effective_message, "❌ Fiat tidak valid. Pilih: idr, usd, usdt, eur.")
        return
    FIAT_DEFAULT = fiat
    await smart_reply_text(update.effective_message, f"✅ FIAT default diset ke {fiat.upper()}")

async def _reply_price_core(msg: Message, coin: str, fiat: str, amount: Optional[float] = None):
    coin_id = norm_symbol(coin)
    fiat_norm = fiat.lower()
    if fiat_norm not in FIAT_ALLOWED:
        return  # hemat resource

    data = fetch_price([coin_id], fiat_norm)
    if not data or coin_id not in data or fiat_norm not in data[coin_id]:
        return  # pair tak tersedia → diam

    price = float(data[coin_id][fiat_norm])
    chg = data[coin_id].get(f"{fiat_norm}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""

    if amount is not None:
        total = amount * price
        await smart_reply_text(msg, f"💱 {amount:g} {coin.upper()} ≈ {fmt_price(total, fiat_norm)}{chg_txt}")
    else:
        await smart_reply_text(msg, f"💰 {coin.upper()} = {fmt_price(price, fiat_norm)}{chg_txt}")

async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await smart_reply_text(update.effective_message, "Format: /price <symbol> [fiat]\ncontoh: /price btc usdt")
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await _reply_price_core(update.effective_message, sym, fiat)

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await smart_reply_text(update.effective_message, "Format: /prices btc,eth [fiat]")
        return
    parts = ctx.args[0].split(",")
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    ids = [norm_symbol(p.strip()) for p in parts if p.strip()]
    data = fetch_price(ids, fiat)
    if not data:
        return
    lines = []
    for sym in parts:
        cid = norm_symbol(sym.strip())
        if cid in data and fiat in data[cid]:
            price = data[cid][fiat]
            chg = data[cid].get(f"{fiat}_24h_change")
            chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
            lines.append(f"• {sym.upper()} = {fmt_price(price, fiat)}{chg_txt}")
    if lines:
        await smart_reply_text(update.effective_message, "\n".join(lines))

async def cmd_convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await smart_reply_text(update.effective_message, "Format: /convert <jumlah> <coin> [fiat]\ncontoh: /convert 0.25 btc idr")
        return
    try:
        amount = float(ctx.args[0])
    except Exception:
        await smart_reply_text(update.effective_message, "Jumlah tidak valid.")
        return
    coin = ctx.args[1]
    fiat = (ctx.args[2] if len(ctx.args) > 2 else FIAT_DEFAULT).lower()
    await _reply_price_core(update.effective_message, coin, fiat, amount)

# ========= AI =========
async def ai_answer(text: str) -> Optional[str]:
    if not ai_client:
        return None
    try:
        resp = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "Jawab ringkas, akurat, gaya profesional elegan."},
                      {"role": "user", "content": text}],
            max_tokens=350,
            temperature=0.5,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("AI error: %s", e)
        return None

async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args) if ctx.args else ""
    if not prompt:
        await smart_reply_text(update.effective_message, "Format: /ask <pertanyaan>")
        return
    await smart_reply_text(update.effective_message, "⏳ Sedang berpikir…")
    ans = await ai_answer(prompt)
    if ans:
        await smart_reply_text(update.effective_message, ans)
    else:
        await smart_reply_text(update.effective_message, "Maaf, AI sedang tidak tersedia.")

# ========= Text Router (tanpa /ask) =========
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()

    # 1) Amount convert: "0.25 btc usd"
    m = AMOUNT_PAIR.match(text)
    if m:
        amount = float(m.group(1))
        coin = m.group(3)
        fiat = m.group(4).lower()
        await _reply_price_core(update.effective_message, coin, fiat, amount)
        return

    # 2) Single pair: "btc usd"
    m = SINGLE_PAIR.match(text)
    if m:
        coin = m.group(1)
        fiat = m.group(2).lower()
        await _reply_price_core(update.effective_message, coin, fiat)
        return

    # 3) fallback AI (hemat: jika terlalu pendek, abaikan)
    if len(text) < 3:
        return
    if ai_client:
        ans = await ai_answer(text)
        if ans:
            await smart_reply_text(update.effective_message, ans)

# ========= Start / Help =========
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Harga", callback_data="menu:price"),
         InlineKeyboardButton("💱 Convert", callback_data="menu:convert")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu:air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu:ai")],
    ])
    text = (
        "<b>AirdropCore (AI)</b>\n"
        "(AI juga tanpa <i>/ask</i>)\n"
        "• <code>/price</code> &lt;coin&gt; fiat\n"
        "• <code>/prices</code> btc,eth idr\n"
        "• <code>/convert</code> 123 sol usd\n"
        "• <code>/setfiat</code> idr|usd|usdt|eur\n"
        "• <code>/airupdate</code> pages force, <code>/airdrops</code>, <code>/airnews</code>\n"
        "• Ketik bebas: <code>btc usd</code> atau <code>0.25 eth idr</code>\n"
    )
    await smart_reply_text(update.effective_message, text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    if data == "menu:price":
        txt = "Contoh:\n• /price btc usdt\n• btc usd"
    elif data == "menu:convert":
        txt = "Contoh:\n• /convert 0.25 btc idr\n• 12 sol usd"
    elif data == "menu:air":
        txt = "• /airupdate 2 true untuk update paksa\n• /airdrops untuk daftar + tombol Next/Prev\n• /airnews untuk yang baru"
    else:
        txt = "Tanya apa saja langsung tanpa /ask."
    try:
        await q.edit_message_text(txt)
    except Exception:
        await q.message.reply_text(txt)

# ========= Runner =========
def main() -> None:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setfiat", cmd_setfiat))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("convert", cmd_convert))
    app.add_handler(CommandHandler("ask", cmd_ask))

    # Airdrops
    app.add_handler(CommandHandler("airdrops", cmd_airdrops))
    app.add_handler(CommandHandler("airupdate", cmd_airupdate))
    app.add_handler(CommandHandler("airnews", cmd_airnews))
    app.add_handler(CallbackQueryHandler(cb_airpage, pattern=r"^airpage:"))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern=r"^menu:"))

    # Free text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

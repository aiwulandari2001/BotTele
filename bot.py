# bot.py
import os, re, time, html, logging, asyncio, json
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict

import requests
from bs4 import BeautifulSoup

from dotenv import load_dotenv
load_dotenv(override=True)

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, constants as tg_c
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ====== OpenAI (opsional) ======
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ====== Konfigurasi dari ENV ======
BOT_TOKEN         = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "").strip()
FIAT_DEFAULT      = os.getenv("FIAT_DEFAULT", "usd").lower()
AIRDROP_PAGE_SIZE = int(os.getenv("AIRDROP_PAGE_SIZE", "6"))
AIR_COOLDOWN_SEC  = int(os.getenv("AIRDROP_COOLDOWN_SEC", "60"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum diisi di .env")

client = None
if OPENAI_API_KEY and OpenAI:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logging.getLogger("airdropcore.bot").info("OpenAI aktif")
    except Exception as e:
        client = None
        logging.getLogger("airdropcore.bot").warning(f"OpenAI gagal init: {e}")

# ====== Logging ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# ====== Util ======
h = html.escape

SYMBOL_MAP = {
    "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin", "usdt": "tether",
    "usdc": "usd-coin", "sol": "solana", "xrp": "ripple", "ada": "cardano",
    "doge": "dogecoin", "trx": "tron", "matic": "polygon", "dot": "polkadot",
    "ton": "the-open-network", "avax": "avalanche-2", "ltc": "litecoin",
    "shib": "shiba-inu", "link": "chainlink", "op": "optimism", "arb": "arbitrum"
}

SUPPORTED_FIAT = {"usd", "usdt", "idr", "eur"}

PRICE_PAIR_RE   = re.compile(r"^\s*([a-z0-9]{2,10})[\s/]+([a-z]{2,6})\s*$", re.I)
AMOUNT_PAIR_RE  = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-z0-9]{2,10})(?:\s+([a-z]{2,6}))?\s*$", re.I)
SYMBOL_ONLY_RE  = re.compile(r"^\s*([a-z0-9]{2,10})\s*$", re.I)

USER_AGENT = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AirdropCoreBot/1.0 (+https://t.me/)"
}

def norm_symbol(sym: str) -> str:
    s = (sym or "").lower().strip()
    return SYMBOL_MAP.get(s, s)

def fetch_price(ids: List[str], fiat: str) -> Dict:
    # CoinGecko simple price
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(ids),
                "vs_currencies": fiat,
                "include_24hr_change": "true"
            },
            timeout=20
        )
        return resp.json()
    except Exception as e:
        log.warning(f"fetch_price error: {e}")
        return {}

def fmt_price(val: float, fiat: str) -> str:
    try:
        if fiat in ("usd", "usdt", "eur"):
            return f"{val:,.4f} {fiat.upper()}"
        if fiat == "idr":
            return f"{val:,.0f} {fiat.upper()}"
    except Exception:
        pass
    return f"{val} {fiat.upper()}"

# ====== Airdrop ======
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None

AIRDROPS: List[Airdrop] = []
_last_air_scrape_ts: float = 0

def scrape_cryptorank() -> List[Airdrop]:
    url = "https://cryptorank.io/drophunting"
    r = requests.get(url, headers=USER_AGENT, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    # Kartu utama
    cards = soup.select("a[href*='/airdrops/'], a[href*='/drophunting/']")
    # Fallback: coba meta list
    if not cards:
        cards = soup.select("div a")
    for a in cards:
        name = (a.get_text() or "").strip()
        if not name:
            continue
        href = a.get("href", "")
        if not href or href.startswith("#"):
            continue
        full = href if href.startswith("http") else f"https://cryptorank.io{href}"
        # Filter kasar: hanya yang mengandung airdrop/drophunting/nodes/campaign
        key = href.lower()
        if not any(k in key for k in ("/airdrops", "/drophunting", "/nodes", "/campaign")):
            continue
        slug = re.sub(r"[^a-z0-9\-]+", "-", name.lower()).strip("-")
        if not slug:
            continue
        out.append(Airdrop(
            slug=slug,
            name=name,
            chain=None,
            reward=None,
            url=full,
            source="cryptorank.io"
        ))
    # Unikkan by slug
    mp: Dict[str, Airdrop] = {}
    for a in out:
        if a.slug not in mp:
            mp[a.slug] = a
    return list(mp.values())

def air_merge(items: List[Airdrop]):
    global AIRDROPS
    mp: Dict[str, Airdrop] = {a.slug: a for a in AIRDROPS}
    for a in items:
        prev = mp.get(a.slug)
        if not prev:
            mp[a.slug] = a
        else:
            # prefer yang ada reward/chain/url
            if a.reward and not prev.reward: prev.reward = a.reward
            if a.chain and not prev.chain:   prev.chain = a.chain
            if a.url and not prev.url:       prev.url   = a.url
    AIRDROPS = sorted(mp.values(), key=lambda x: x.name.lower())

async def send_airdrop_page(query, page: int):
    per_page = max(1, AIRDROP_PAGE_SIZE)
    total = len(AIRDROPS)
    if total == 0:
        await query.edit_message_text("❌ Belum ada data Airdrop.\nKetik /airupdate untuk ambil terbaru.")
        return
    max_page = (total + per_page - 1)//per_page
    page = max(1, min(page, max_page))
    start = (page-1)*per_page
    end = min(start + per_page, total)
    items = AIRDROPS[start:end]

    lines = [f"🎁 <b>Daftar Airdrop</b> (hal {page}/{max_page})"]
    for i, a in enumerate(items, start=start+1):
        lines.append(
            f"\n<b>{i}.</b> {h(a.name)}"
            f"\n• Chain: {h(a.chain) if a.chain else '-'}"
            f"\n• Reward: {h(a.reward) if a.reward else '-'}"
            f"\n• Link: <a href='{h(a.url or '')}'>Detail</a>"
        )

    buttons: List[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"air_page:{page-1}"))
    if end < total:
        buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"air_page:{page+1}"))

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=tg_c.ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([buttons]) if buttons else None
    )

# ====== Handlers ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [
            InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
            InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
        ],
        [
            InlineKeyboardButton("🧠 AI", callback_data="menu_ai"),
        ],
    ]
    await update.message.reply_text(
        "Selamat datang di <b>AirdropCore Bot</b>!\n"
        "• Ketik <code>btc usdt</code> atau <code>0.02 eth idr</code>\n"
        "• /price, /airdrops, /airupdate, /setfiat\n"
        "• Tanyakan apa saja (tanpa /ask), AI akan jawab.\n",
        parse_mode=tg_c.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Bantuan</b>\n"
        "• <code>/setfiat usd|usdt|idr|eur</code>\n"
        "• <code>/price btc usdt</code> → harga 1 koin\n"
        "• <code>0.002 eth idr</code> → konversi otomatis\n"
        "• <code>btc usdt</code> → harga pair cepat\n"
        "• <code>/airdrops</code> → daftar airdrop (paging)\n"
        "• <code>/airupdate</code> → tarik airdrop terbaru (cryptorank)\n"
        "Cukup kirim pertanyaan tanpa /ask untuk AI.",
        parse_mode=tg_c.ParseMode.HTML
    )

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: <b>{FIAT_DEFAULT.upper()}</b>\n"
            "Format: <code>/setfiat usd|usdt|idr|eur</code>",
            parse_mode=tg_c.ParseMode.HTML
        )
        return
    fiat = (ctx.args[0] or "").lower()
    if fiat not in SUPPORTED_FIAT:
        await update.message.reply_text(
            "❌ Fiat tidak didukung. Pilih: usd, usdt, idr, eur.",
            parse_mode=tg_c.ParseMode.HTML
        )
        return
    FIAT_DEFAULT = fiat
    await update.message.reply_text(
        f"✅ FIAT default diset ke <b>{fiat.upper()}</b>",
        parse_mode=tg_c.ParseMode.HTML
    )

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Format: <code>/price btc usdt</code>",
            parse_mode=tg_c.ParseMode.HTML
        )
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat)

async def reply_price(update: Update, sym: str, fiat: str):
    # jika fiat tidak didukung → diam
    if fiat not in SUPPORTED_FIAT:
        return
    cid = norm_symbol(sym)
    data = fetch_price([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        # tidak balas agar hemat VPS
        return
    price_val = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = ""
    if isinstance(chg, (int, float)):
        chg_txt = f" · 24h: {'+' if chg>=0 else ''}{chg:.2f}%"
    await update.message.reply_text(
        f"💰 <b>{h(sym.upper())}</b> = <b>{h(fmt_price(price_val, fiat))}</b>{h(chg_txt)}",
        parse_mode=tg_c.ParseMode.HTML
    )

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _last_air_scrape_ts
    now = time.time()
    left = int(AIR_COOLDOWN_SEC - (now - _last_air_scrape_ts))
    if left > 0:
        await update.message.reply_text(
            f"⏳  Tunggu <b>{left}</b> detik untuk update lagi.",
            parse_mode=tg_c.ParseMode.HTML
        )
        return

    await update.message.reply_text("🔎 Mengambil airdrop dari CryptoRank…", parse_mode=tg_c.ParseMode.HTML)
    try:
        items = scrape_cryptorank()
        air_merge(items)
        _last_air_scrape_ts = time.time()
        await update.message.reply_text(
            f"✅  Selesai. Terkumpul <b>{len(AIRDROPS)}</b> airdrop (CryptoRank)."
            "\nGunakan /airdrops untuk melihat daftar.",
            parse_mode=tg_c.ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        log.exception("airupdate error")
        await update.message.reply_text(
            f"❌ Gagal mengambil airdrop: <code>{h(str(e))}</code>",
            parse_mode=tg_c.ParseMode.HTML
        )

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not AIRDROPS:
        await update.message.reply_text(
            "ℹ️ Data kosong. Jalankan /airupdate dulu.",
            parse_mode=tg_c.ParseMode.HTML
        )
        return
    # kirim halaman 1 via callback pipeline (agar tombol bisa Edit)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Buka daftar", callback_data="air_page:1")]])
    await update.message.reply_text(
        "🎁 Klik untuk membuka daftar airdrop:",
        reply_markup=kb
    )

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /tugas <nomor atau slug>
    Saat ini kita tampilkan detail basic + link (task detail butuh scraping halaman masing2).
    """
    if not ctx.args:
        await update.message.reply_text(
            "Format: <code>/tugas 3</code> atau <code>/tugas nama-proyek</code>",
            parse_mode=tg_c.ParseMode.HTML
        )
        return
    key = " ".join(ctx.args).lower().strip()
    item: Optional[Airdrop] = None

    # by index
    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(AIRDROPS):
            item = AIRDROPS[idx]
    # by slug/name contains
    if not item:
        for a in AIRDROPS:
            if a.slug == key or a.slug in key or a.name.lower() == key:
                item = a
                break

    if not item:
        await update.message.reply_text("❌ Airdrop tidak ditemukan.", parse_mode=tg_c.ParseMode.HTML)
        return

    await update.message.reply_text(
        "📌 <b>Detail Airdrop</b>\n"
        f"Nama: <b>{h(item.name)}</b>\n"
        f"Chain: {h(item.chain) if item.chain else '-'}\n"
        f"Reward: {h(item.reward) if item.reward else '-'}\n"
        f"Link: <a href='{h(item.url or '')}'>Buka halaman</a>\n\n"
        "• Rekomendasi: buka link untuk melihat task step-by-step (tiap project berbeda).",
        parse_mode=tg_c.ParseMode.HTML,
        disable_web_page_preview=False
    )

# ==== Callback buttons (menu & pagination) ====
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    try:
        await q.answer()
    except Exception:
        pass

    # Pagination Airdrop
    if data.startswith("air_page:"):
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 1
        try:
            await send_airdrop_page(q, page)
        except Exception as e:
            log.warning(f"cb air_page error: {e}")
        return

    # Menu teks
    if data == "menu_price":
        txt = (
            "💰 <b>Harga</b>\n"
            "Contoh:\n"
            "• <code>/price btc usdt</code>\n"
            "• <code>0.25 eth idr</code>\n"
            "• <code>btc usdt</code>\n"
        )
    elif data == "menu_air":
        txt = (
            "🎁 <b>Airdrop</b>\n"
            "• <code>/airupdate</code>  (tarik dari CryptoRank)\n"
            "• <code>/airdrops</code>   (daftar + Next/Prev)\n"
            "• <code>/tugas &lt;no/slug&gt;</code> (detail + link)\n"
        )
    else:  # menu_ai
        txt = (
            "🧠 <b>AI</b>\n"
            "Cukup ketik pertanyaan tanpa /ask. Contoh:\n"
            "• <i>Jelaskan apa itu restaking?</i>\n"
            "• <i>Buat ringkas berita crypto hari ini</i>\n"
        )

    try:
        await q.edit_message_text(txt, parse_mode=tg_c.ParseMode.HTML)
    except Exception as e:
        log.warning(f"cb edit text error: {e}")

# ====== Router teks (AI + deteksi harga) ======
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not (msg.text or "").strip():
        return
    text = msg.text.strip()

    # 1) 0.002 eth [idr/usd]
    m = AMOUNT_PAIR_RE.match(text)
    if m:
        amount = float(m.group(1))
        sym = m.group(2)
        fiat = (m.group(3) or FIAT_DEFAULT).lower()
        if fiat not in SUPPORTED_FIAT:
            return
        cid = norm_symbol(sym)
        data = fetch_price([cid], fiat)
        if not data or cid not in data or fiat not in data[cid]:
            return
        unit = data[cid][fiat]
        val = unit * amount
        await msg.reply_text(
            f"🔄 <b>{amount} {h(sym.upper())}</b> ≈ <b>{h(fmt_price(val, fiat))}</b>\n"
            f"(1 {h(sym.upper())} = {h(fmt_price(unit, fiat))})",
            parse_mode=tg_c.ParseMode.HTML
        )
        return

    # 2) btc usdt  /  eth idr
    m2 = PRICE_PAIR_RE.match(text)
    if m2:
        sym, fiat = m2.group(1), m2.group(2).lower()
        await reply_price(update, sym, fiat)
        return

    # 3) simbol saja → harga default fiat
    m3 = SYMBOL_ONLY_RE.match(text)
    if m3:
        sym = m3.group(1)
        await reply_price(update, sym, FIAT_DEFAULT)
        return

    # 4) fallback AI (kalau ada OPENAI)
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": text}],
                max_tokens=400,
                temperature=0.4,
            )
            answer = (resp.choices[0].message.content or "").strip()
            if answer:
                await msg.reply_text(answer)
        except Exception as e:
            log.warning(f"AI error: {e}")

# ====== Error handler ======
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Handler error: %s", context.error)

# ====== Main ======
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("price", price_cmd))

    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))

    app.add_handler(CallbackQueryHandler(on_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.add_error_handler(on_error)
    return app

def main():
    log.info("Bot polling start…")
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

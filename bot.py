# bot.py
import os, re, html, json, time, asyncio, logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict

import httpx
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters
)

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

FIAT_ALLOWED = {"usd", "idr", "usdt", "eur"}
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()
if FIAT_DEFAULT not in FIAT_ALLOWED:
    FIAT_DEFAULT = "usd"

AIRDROP_CACHE_FILE = os.getenv("AIRDROP_CACHE_FILE", "airdrops.json")
AIRDROP_COOLDOWN_SEC = int(os.getenv("AIRDROP_COOLDOWN_SEC", "60"))
AIRDROP_PAGE_SIZE = int(os.getenv("AIRDROP_PAGE_SIZE", "10"))

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; AirdropCoreBot/1.0; +https://t.me/)"
}

# HTTPX client
HTTP_TIMEOUT = 25.0
http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=UA)

# OpenAI (opsional)
client_openai = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        client_openai = OpenAI(api_key=OPENAI_API_KEY)
        logging.getLogger("airdropcore.bot").info("OpenAI aktif")
    except Exception as e:
        logging.getLogger("airdropcore.bot").warning(f"OpenAI init gagal: {e}")

# Logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("airdropcore.bot")

# ========= UTIL =========
def esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)

def pretty_num(n) -> str:
    try:
        f = float(n)
    except Exception:
        return str(n)
    if abs(f) >= 1:
        return f"{f:,.2f}"
    if abs(f) >= 0.01:
        return f"{f:,.4f}"
    return f"{f:.8f}"

# ========= PRICE (CoinGecko) =========
_SYMBOL_MAP = {
    "btc":"bitcoin","xbt":"bitcoin","eth":"ethereum","bnb":"binancecoin","usdt":"tether",
    "usdc":"usd-coin","sol":"solana","ada":"cardano","xrp":"ripple","dot":"polkadot",
    "doge":"dogecoin","trx":"tron","matic":"polygon","ton":"the-open-network",
    "avax":"avalanche-2","ltc":"litecoin","link":"chainlink","apt":"aptos","arb":"arbitrum",
    "op":"optimism","sui":"sui","sei":"sei-network","icp":"internet-computer","atom":"cosmos",
    "near":"near","xmr":"monero","etc":"ethereum-classic","bch":"bitcoin-cash","pepe":"pepe",
    "wif":"dogwifcoin"
}
def map_symbol(sym: str) -> Optional[str]:
    if not sym: return None
    s = sym.lower().strip()
    return _SYMBOL_MAP.get(s, s)

async def cg_price(ids: List[str], fiats: List[str]) -> Dict:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": ",".join(ids),
        "vs_currencies": ",".join(fiats),
        "include_24hr_change": "true"
    }
    r = await http.get(url, params=params)
    r.raise_for_status()
    return r.json()

# ========= AIRDROP SCRAPER (Cryptorank) =========
@dataclass
class Airdrop:
    slug: str
    name: str
    url: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    source: str = "cryptorank"
    tasks: Optional[List[str]] = None

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9\-]+", "-", (name or "").lower().strip().replace(" ", "-")).strip("-")

async def scrape_cryptorank() -> List[Airdrop]:
    """
    Scrape daftar dari https://cryptorank.io/drophunting
    Catatan: struktur halaman bisa berubah sewaktu-waktu. Selector dibuat agak longgar.
    """
    url = "https://cryptorank.io/drophunting"
    r = await http.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    items: List[Airdrop] = []
    cards = soup.select("a[href*='/drophunting/'], div[data-href*='/drophunting/']")
    # fallback: cari blok bergaya card
    if not cards:
        cards = soup.select("a, div")
        cards = [c for c in cards if c.get("href","").startswith("/drophunting/") or c.get("data-href","").startswith("/drophunting/")]

    seen = set()
    for c in cards:
        href = c.get("href") or c.get("data-href") or ""
        if not href: 
            continue
        if href.startswith("/"):
            full = "https://cryptorank.io" + href
        else:
            full = href
        # Nama biasanya dalam heading/text card
        name_el = None
        for sel in ["h3","h2",".title",".name",".airdrop-card__title",".card__title",".MuiTypography-root"]:
            name_el = c.select_one(sel) if hasattr(c, "select_one") else None
            if name_el: break
        name = (name_el.get_text(strip=True) if name_el else c.get_text(strip=True)) or ""
        name = re.sub(r"\s+", " ", name)
        if not name or len(name) < 2: 
            continue
        slug = _slugify(name)
        if slug in seen: 
            continue
        seen.add(slug)

        # chain/reward try best effort
        chain = None
        reward = None
        chip = None
        for sel in [".chip",".badge",".label",".network",".chain",".airdrop-card__chip"]:
            chip = (c.select_one(sel) if hasattr(c,"select_one") else None) or chip
        if chip:
            text = chip.get_text(" ", strip=True)
            # heuristik: kalau ada $, anggap reward; sisanya chain
            if "$" in text or "USD" in text.upper():
                reward = text
            else:
                chain = text

        items.append(Airdrop(slug=slug, name=name, url=full, chain=chain, reward=reward))
    return items

def load_airdrops() -> Tuple[List[Airdrop], float]:
    if not os.path.isfile(AIRDROP_CACHE_FILE):
        return [], 0.0
    try:
        data = json.load(open(AIRDROP_CACHE_FILE, "r", encoding="utf-8"))
        ts = float(data.get("_ts", 0))
        arr = [Airdrop(**x) for x in data.get("items", [])]
        return arr, ts
    except Exception:
        return [], 0.0

def save_airdrops(items: List[Airdrop]):
    data = {"_ts": time.time(), "items": [asdict(x) for x in items]}
    tmp = AIRDROP_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AIRDROP_CACHE_FILE)

_last_airupdate_ts = 0.0

# ========= REGEX INPUT BEBAS =========
PAIR_FREE = re.compile(r"^\s*([a-zA-Z0-9]{2,12})\s*[\/\s]\s*([a-zA-Z]{2,6})\s*$")
SYMBOL_ONLY = re.compile(r"^\s*([a-zA-Z0-9]{2,12})\s*$")
AMOUNT_PAIR = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]{2,12})\s*$")

# ========= UI =========
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🎁 Airdrops", callback_data="menu_air")],
        [InlineKeyboardButton("🤖 AI", callback_data="menu_ai"),
         InlineKeyboardButton("ℹ️ Bantuan", callback_data="menu_help")]
    ])

def kb_air_nav(page: int, total_pages: int, q: str="") -> InlineKeyboardMarkup:
    btns = []
    if page > 1:
        btns.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"air:list:{page-1}:{q}"))
    if page < total_pages:
        btns.append(InlineKeyboardButton("Next ➡️", callback_data=f"air:list:{page+1}:{q}"))
    row2 = [InlineKeyboardButton("🔄 Refresh", callback_data=f"air:refresh:{max(page,1)}:{q}")]
    return InlineKeyboardMarkup([btns] if btns else [[]], row2)

# ========= COMMANDS =========
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Selamat datang di <b>AirdropCore Bot</b> ✨\n"
        "• Harga cepat: <code>btc usdt</code> / <code>btc/usdt</code> / <code>eth</code>\n"
        "• Konversi: <code>0.002 eth</code> → USD & IDR\n"
        "• Airdrops: <code>/airupdate</code>, <code>/airdrops</code>, <code>/tugas &lt;slug/nama&gt;</code>\n"
        "• AI: <code>/ask ...</code> atau tanya bebas\n",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main()
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 <b>Perintah</b>\n"
        "• <code>btc usdt</code>, <code>btc/usdt</code>, <code>btc</code>\n"
        "• <code>0.002 eth</code> → konversi USD & IDR\n"
        "• <code>/price &lt;koin&gt; [fiat]</code>\n"
        "• <code>/airupdate</code> → scrape terbaru (cooldown)\n"
        "• <code>/airdrops [kata]</code> → list + pencarian\n"
        "• <code>/tugas &lt;slug/nama&gt;</code> → detail & link\n"
        "• <code>/ask ...</code> → AI (butuh OPENAI_API_KEY)\n",
        parse_mode=ParseMode.HTML
    )

# ==== PRICE HELPERS ====
async def reply_price_single(update: Update, sym: str, fiat: str):
    coin_id = map_symbol(sym)
    if not coin_id:  # hemat VPS
        return
    fiat = (fiat or FIAT_DEFAULT).lower()
    if fiat not in FIAT_ALLOWED:
        return
    try:
        data = await cg_price([coin_id], [fiat])
    except Exception as e:
        log.warning(f"cg_price error: {e}")
        return
    if coin_id not in data or fiat not in data[coin_id]:
        return
    price = float(data[coin_id][fiat])
    chg = data[coin_id].get(f"{fiat}_24h_change")
    chg_txt = f" <i>(24h: {float(chg):+.2f}%)</i>" if isinstance(chg, (int,float)) else ""
    await update.message.reply_text(
        f"💰 <b>{esc(sym.upper())}</b> = <b>{esc(pretty_num(price))} {esc(fiat.upper())}</b>{chg_txt}",
        parse_mode=ParseMode.HTML
    )

async def reply_amount_multi(update: Update, amount_str: str, sym: str):
    try:
        amt = float(amount_str)
        if amt <= 0: return
    except Exception:
        return
    coin_id = map_symbol(sym)
    if not coin_id: return
    want = [x for x in ("usd","idr") if x in FIAT_ALLOWED]
    if not want: return
    try:
        data = await cg_price([coin_id], want)
    except Exception:
        return
    if coin_id not in data: return
    lines = [f"🔁 <b>{esc(pretty_num(amt))} {esc(sym.upper())}</b> ≈"]
    for f in want:
        if f in data[coin_id]:
            per1 = float(data[coin_id][f])
            lines.append(f"• <b>{esc(pretty_num(amt*per1))} {esc(f.upper())}</b>  "
                         f"<i>(1 {esc(sym.upper())} = {esc(pretty_num(per1))} {esc(f.upper())})</i>")
    if len(lines) > 1:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Format: <code>/price &lt;koin&gt; [fiat]</code>\nContoh: <code>/price btc usdt</code>",
            parse_mode=ParseMode.HTML
        ); return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await reply_price_single(update, sym, fiat)

# ==== AIRDROP COMMANDS ====
async def cmd_airupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _last_airupdate_ts
    now = time.time()
    left = int(_last_airupdate_ts + AIRDROP_COOLDOWN_SEC - now)
    if left > 0:
        await update.message.reply_text(
            f"⏳ Tunggu <b>{left}</b> detik untuk update lagi.",
            parse_mode=ParseMode.HTML
        )
        return
    _last_airupdate_ts = now
    await update.message.reply_text("🔎 Scraping dari <b>cryptorank.io/drophunting</b>…", parse_mode=ParseMode.HTML)
    try:
        items = await scrape_cryptorank()
        if not items:
            await update.message.reply_text("❌ Tidak ada data baru (mungkin struktur situs berubah).", parse_mode=ParseMode.HTML)
            return
        # Unikkan by slug (ambil yang terbaru)
        mp: Dict[str, Airdrop] = {}
        for a in items:
            if a.slug not in mp:
                mp[a.slug] = a
        items = list(mp.values())
        save_airdrops(items)
        await update.message.reply_text(
            f"✅ Selesai. Disimpan <b>{len(items)}</b> airdrop.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        log.exception("airupdate error")
        await update.message.reply_text(f"❌ Gagal update: {esc(str(e))}", parse_mode=ParseMode.HTML)

def _filter_airdrops(arr: List[Airdrop], q: str) -> List[Airdrop]:
    if not q: return arr
    s = q.lower()
    out = []
    for a in arr:
        if s in a.slug.lower() or s in a.name.lower():
            out.append(a)
    return out

def _slice_page(arr: List[Airdrop], page: int, page_size: int) -> Tuple[List[Airdrop], int]:
    total = len(arr)
    if total == 0: return [], 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    start = (page-1)*page_size
    end = min(start+page_size, total)
    return arr[start:end], pages

async def cmd_airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).strip()
    arr, ts = load_airdrops()
    if not arr:
        await update.message.reply_text("ℹ️ Cache kosong. Jalankan <code>/airupdate</code> dulu.", parse_mode=ParseMode.HTML); return
    view = _filter_airdrops(arr, q)
    page = 1
    chunk, pages = _slice_page(view, page, AIRDROP_PAGE_SIZE)
    if not chunk:
        await update.message.reply_text("❌ Tidak ada yang cocok.", parse_mode=ParseMode.HTML); return

    lines = [f"🎁 <b>Airdrops</b> (page {page}/{pages})"]
    for a in chunk:
        lines.append(f"• <b>{esc(a.name)}</b> — {esc(a.chain or 'Unknown')} — {esc(a.reward or '')}\n"
                     f"  <a href=\"{esc(a.url)}\">{esc(a.url)}</a>\n"
                     f"  <i>slug:</i> <code>{esc(a.slug)}</code>")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Next ➡️", callback_data=f"air:list:2:{q}")]]) if pages>1 else None
    )

async def cmd_tugas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: <code>/tugas &lt;slug/nama&gt;</code>", parse_mode=ParseMode.HTML); return
    key = " ".join(ctx.args).strip().lower()
    arr, _ = load_airdrops()
    if not arr:
        await update.message.reply_text("Cache kosong. Jalankan <code>/airupdate</code> dulu.", parse_mode=ParseMode.HTML); return
    found = None
    for a in arr:
        if a.slug == key or key in a.slug or key == a.name.lower():
            found = a; break
    if not found:
        await update.message.reply_text("❌ Airdrop tidak ditemukan.", parse_mode=ParseMode.HTML); return

    lines = [
        f"📋 <b>{esc(found.name)}</b>",
        f"Chain: <b>{esc(found.chain or 'Unknown')}</b>",
        f"Reward: <b>{esc(found.reward or '-')}</b>",
        f"Link: <a href=\"{esc(found.url)}\">{esc(found.url)}</a>",
        "",
        "<i>Detail tugas biasanya ada di halaman resmi. Buka link di atas untuk langkah lengkap.</i>"
    ]
    # Jika suatu saat kita men-scrape halaman detail, isikan found.tasks (list)
    if found.tasks:
        lines.append("\n<b>Tasks:</b>")
        for t in found.tasks:
            lines.append(f"• {esc(t)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ==== CALLBACK (pagination/refresh) ====
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    try:
        if data.startswith("air:list:"):
            _,_,page_str,query = data.split(":",3)
            page = int(page_str)
            arr, _ = load_airdrops()
            view = _filter_airdrops(arr, query)
            chunk, pages = _slice_page(view, page, AIRDROP_PAGE_SIZE)
            if not chunk:
                await q.edit_message_text("❌ Tidak ada data.", parse_mode=ParseMode.HTML); return
            lines = [f"🎁 <b>Airdrops</b> (page {page}/{pages})"]
            for a in chunk:
                lines.append(f"• <b>{esc(a.name)}</b> — {esc(a.chain or 'Unknown')} — {esc(a.reward or '')}\n"
                             f"  <a href=\"{esc(a.url)}\">{esc(a.url)}</a>\n"
                             f"  <i>slug:</i> <code>{esc(a.slug)}</code>")
            # nav
            btns = []
            if page>1:   btns.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"air:list:{page-1}:{query}"))
            if page<pages:btns.append(InlineKeyboardButton("Next ➡️", callback_data=f"air:list:{page+1}:{query}"))
            nav = InlineKeyboardMarkup([btns] if btns else [[]],[ [InlineKeyboardButton("🔄 Refresh", callback_data=f"air:refresh:{page}:{query}")] ])
            await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=nav)

        elif data.startswith("air:refresh:"):
            _,_,page_str,query = data.split(":",3)
            # refresh = baca cache lagi (update background via /airupdate supaya hemat)
            page = int(page_str)
            arr, _ = load_airdrops()
            view = _filter_airdrops(arr, query)
            chunk, pages = _slice_page(view, page, AIRDROP_PAGE_SIZE)
            if not chunk:
                await q.edit_message_text("❌ Tidak ada data.", parse_mode=ParseMode.HTML); return
            lines = [f"🎁 <b>Airdrops</b> (page {page}/{pages})"]
            for a in chunk:
                lines.append(f"• <b>{esc(a.name)}</b> — {esc(a.chain or 'Unknown')} — {esc(a.reward or '')}\n"
                             f"  <a href=\"{esc(a.url)}\">{esc(a.url)}</a>\n"
                             f"  <i>slug:</i> <code>{esc(a.slug)}</code>")
            btns = []
            if page>1:   btns.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"air:list:{page-1}:{query}"))
            if page<pages:btns.append(InlineKeyboardButton("Next ➡️", callback_data=f"air:list:{page+1}:{query}"))
            nav = InlineKeyboardMarkup([btns] if btns else [[]],[ [InlineKeyboardButton("🔄 Refresh", callback_data=f"air:refresh:{page}:{query}")] ])
            await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=nav)

        elif data == "menu_price":
            await q.edit_message_text(
                "💰 <b>Harga Cepat</b>\n"
                "• <code>btc usdt</code>\n"
                "• <code>btc/usdt</code>\n"
                "• <code>eth</code>\n"
                "• <code>0.002 eth</code> (konversi USD & IDR)",
                parse_mode=ParseMode.HTML, reply_markup=kb_main()
            )
        elif data == "menu_air":
            await q.edit_message_text(
                "🎁 <b>Airdrops</b>\n"
                "• <code>/airupdate</code> untuk ambil data terbaru (cooldown)\n"
                "• <code>/airdrops [cari]</code> untuk daftar & navigasi\n"
                "• <code>/tugas &lt;slug/nama&gt;</code> untuk detail + link",
                parse_mode=ParseMode.HTML, reply_markup=kb_main()
            )
        elif data == "menu_ai":
            await q.edit_message_text("🤖 <b>AI</b>\nGunakan <code>/ask pertanyaan</code> atau tanya bebas.",
                                      parse_mode=ParseMode.HTML, reply_markup=kb_main())
        elif data == "menu_help":
            await q.edit_message_text("ℹ️ Lihat <code>/help</code> untuk seluruh perintah.",
                                      parse_mode=ParseMode.HTML, reply_markup=kb_main())
    except Exception as e:
        log.warning(f"cb error: {e}")

# ==== AI /ask ====
async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not client_openai:
        await update.message.reply_text("❌ AI belum aktif (OPENAI_API_KEY kosong).", parse_mode=ParseMode.HTML); return
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: <code>/ask &lt;pertanyaan&gt;</code>", parse_mode=ParseMode.HTML); return
    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except Exception:
        pass
    try:
        resp = client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=600, temperature=0.5
        )
        answer = (resp.choices[0].message.content or "").strip() or "Maaf, aku tidak menemukan jawaban."
        await update.message.reply_text(esc(answer), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {esc(str(e))}", parse_mode=ParseMode.HTML)

# ==== TEXT ROUTER (tanpa slash) ====
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # 1)  Jumlah + koin (0.002 eth)
    m = AMOUNT_PAIR.match(text)
    if m:
        amt, sym = m.groups()
        await reply_amount_multi(update, amt, sym)
        return

    # 2) Pair (btc usdt / btc/usdt)
    m = PAIR_FREE.match(text)
    if m:
        sym, fiat = m.groups()
        fiat = fiat.lower()
        if fiat in FIAT_ALLOWED:
            await reply_price_single(update, sym, fiat)
            return
        return  # fiat tak valid → diam

    # 3) Hanya simbol (btc)
    m = SYMBOL_ONLY.match(text)
    if m:
        sym = m.group(1)
        await reply_price_single(update, sym, FIAT_DEFAULT)
        return

    # 4) Fallback ke AI (kalau ada)
    if client_openai:
        try:
            await update.message.chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            resp = client_openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": text}],
                max_tokens=450, temperature=0.6
            )
            ans = (resp.choices[0].message.content or "").strip()
            if ans:
                await update.message.reply_text(esc(ans), parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"AI fallback error: {e}")

# ==== BUILD & RUN ====
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN kosong. Set env BOT_TOKEN dulu.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("airupdate", cmd_airupdate))
    app.add_handler(CommandHandler("airdrops", cmd_airdrops))
    app.add_handler(CommandHandler("tugas", cmd_tugas))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app

def main():
    app = build_app()
    log.info("Bot polling start…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        main()

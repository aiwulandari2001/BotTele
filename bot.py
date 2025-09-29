#!/usr/bin/env python3
import os, re, math, html, logging, asyncio
from typing import Dict, List, Optional, Tuple

# --- third parties
from dotenv import load_dotenv
import httpx
from bs4 import BeautifulSoup
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters,
)

# ====== ENV & LOGGING ======
load_dotenv(override=True)

BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
FIAT_DEFAULT    = os.getenv("FIAT_DEFAULT", "usd").lower().strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("airdropcore.bot")

# Optional OpenAI client (tanpa bikin crash kalau belum terpasang)
client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client aktif")
except Exception as e:
    log.warning("OpenAI client nonaktif: %s", e)
    client = None

# ====== STATE (in-memory) ======
CHAT_FIAT: Dict[int, str] = {}                   # chat_id -> fiat
AIRDROP_CACHE: List[Dict] = []                   # list item airdrop
AIRDROP_PAGE_SIZE = 7

# ====== HTTP CLIENT ======
HTTP_TIMEOUT = httpx.Timeout(15.0, read=20.0)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AirdropCoreBot/1.0; +https://t.me/)"
}

def get_client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True)

# ====== UTIL ======
PAIR_PATTERN = re.compile(r"^\s*([a-z0-9]+)(?:[ /-]([a-z0-9]+))?\s*$", re.I)
AMOUNT_PAIR_PATTERN = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s+([a-z0-9]+)(?:[ /-]([a-z0-9]+))?\s*$", re.I
)

COIN_MAP = {
    # umum
    "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin", "sol": "solana",
    "usdt": "tether", "usdc": "usd-coin", "xrp": "ripple", "ada": "cardano",
    "doge": "dogecoin", "trx": "tron", "matic": "polygon", "dot": "polkadot",
    # contoh tambahan
    "ltc": "litecoin", "link": "chainlink", "atom": "cosmos", "ton": "the-open-network",
    # pi (di coingecko resmi: "pi-network" untuk IOU; jika tak ada, akan gagal aman)
    "pi": "pi-network"
}

FIAT_ALLOWED = {"usd", "usdt", "idr", "eur"}

def norm_symbol(sym: str) -> str:
    s = sym.lower()
    return COIN_MAP.get(s, s)

def get_chat_fiat(chat_id: int) -> str:
    return CHAT_FIAT.get(chat_id, FIAT_DEFAULT)

def fmt_price(val: float, fiat: str) -> str:
    # USDT diperlakukan seperti USD untuk tampilan
    code = "USD" if fiat.lower() == "usdt" else fiat.upper()
    if fiat.lower() in {"usd", "usdt", "eur"}:
        return f"{val:,.4f} {code}"
    if fiat.lower() == "idr":
        return f"Rp {val:,.0f}"
    return f"{val:,.4f} {code}"

async def safe_reply(update: Update, text: str, **kw):
    # hindari error tag HTML
    if kw.get("parse_mode") == "HTML":
        # caller bertanggung jawab escape; kalau tidak yakin, pakai plain
        pass
    try:
        await update.message.reply_text(text, **kw)
    except Exception as e:
        log.warning("reply error: %s", e)

# ====== COINGECKO ======
def cg_simple_price(ids: List[str], fiat: str) -> Dict:
    params = {
        "ids": ",".join(ids),
        "vs_currencies": fiat.lower(),
        "include_24hr_change": "true",
    }
    with get_client() as c:
        r = c.get("https://api.coingecko.com/api/v3/simple/price", params=params)
        r.raise_for_status()
        return r.json()

# ====== AIRDROP SCRAPER (airdrops.io) ======
def scrape_airdrops_io() -> List[Dict]:
    url = "https://airdrops.io/latest/"
    out: List[Dict] = []
    with get_client() as c:
        r = c.get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

    for card in soup.select(".airdrops-list .item, article"):
        title_el = card.select_one(".title, h3, h2, a")
        name = (title_el.get_text(strip=True) if title_el else "").strip()
        if not name:
            continue
        a = card.select_one("a")
        href = a["href"] if a and a.has_attr("href") else url
        reward_el = card.select_one(".reward, .prize, .subtitle")
        reward = reward_el.get_text(strip=True) if reward_el else ""
        chain_el = card.select_one(".chain, .platform, .network")
        chain = chain_el.get_text(strip=True) if chain_el else ""
        slug = name.lower().replace(" ", "-")
        out.append({
            "slug": slug,
            "name": name,
            "url": href,
            "reward": reward,
            "chain": chain,
            "source": "airdrops.io",
        })
    return out

def merge_unique(items: List[Dict]) -> List[Dict]:
    mp: Dict[str, Dict] = {}
    for it in items:
        s = it.get("slug") or it.get("name", "").lower()
        if not s:
            continue
        if s not in mp or (it.get("reward") and not mp[s].get("reward")):
            mp[s] = it
    return list(mp.values())

# ====== COMMANDS ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>AirdropCore Bot</b>\n"
        "Perintah utama:\n"
        "• <code>/price &lt;coin&gt; [fiat]</code>\n"
        "• <code>/prices btc,eth idr</code>\n"
        "• <code>/convert 0.25 btc usd</code>\n"
        "• <code>/setfiat idr|usd|usdt|eur</code>\n"
        "• <code>/airupdate</code>, <code>/airdrops</code>, <code>/tugas &lt;keyword&gt;</code>\n"
        "\nAI: cukup ketik pertanyaan langsung (tanpa /ask)."
    )
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔄 Convert", callback_data="menu_conv")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(txt, parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(kb))

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bantuan:\n"
        "/price <coin> [fiat]\n"
        "/prices btc,eth idr\n"
        "/convert 12.3 sol usd\n"
        "/setfiat idr|usd|usdt|eur\n"
        "/airupdate, /airdrops, /tugas <keyword>\n"
        "\nAI: kirim teks bebas untuk jawaban AI."
    )

# ---- fiat per chat
async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        curr = get_chat_fiat(chat_id).upper()
        await update.message.reply_text(
            f"FIAT saat ini: {curr}\nFormat: /setfiat idr|usd|usdt|eur"
        )
        return
    fiat = ctx.args[0].lower()
    if fiat not in FIAT_ALLOWED:
        await update.message.reply_text("❌ Fiat tidak valid. Pilihan: idr, usd, usdt, eur")
        return
    CHAT_FIAT[chat_id] = fiat
    await update.message.reply_text(f"✅ FIAT diset ke {fiat.upper()}")

# ---- harga
async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Format: /price <coin> [fiat]\ncontoh: /price btc usdt")
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1].lower() if len(ctx.args) > 1 else get_chat_fiat(chat_id))
    await _reply_price(update, sym, fiat)

async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth idr")
        return
    coins = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1].lower() if len(ctx.args) > 1 else get_chat_fiat(chat_id))
    ids = [norm_symbol(c) for c in coins]
    try:
        data = cg_simple_price(ids, fiat)
        lines = []
        for c, cid in zip(coins, ids):
            val = (data.get(cid) or {}).get(fiat)
            if val is None:
                continue
            chg = (data.get(cid) or {}).get(f"{fiat}_24h_change")
            chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
            lines.append(f"• {c.upper()} = {fmt_price(val, fiat)}{chg_txt}")
        if not lines:
            return  # hemat VPS: tidak merespons bila tak ada pair valid
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.exception("prices error: %s", e)
        await update.message.reply_text("❌ Gagal mengambil harga.")

async def convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Format: /convert 0.25 btc usd")
        return
    try:
        amt = float(ctx.args[0])
    except:
        await update.message.reply_text("Nominal tidak valid.")
        return
    sym = ctx.args[1]
    fiat = (ctx.args[2].lower() if len(ctx.args) > 2 else get_chat_fiat(chat_id))
    cid = norm_symbol(sym)
    try:
        data = cg_simple_price([cid], fiat)
        val = (data.get(cid) or {}).get(fiat)
        if val is None:
            return
        total = amt * float(val)
        chg = (data.get(cid) or {}).get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
        await update.message.reply_text(
            f"{amt:g} {sym.upper()} ≈ {fmt_price(total, fiat)}{chg_txt}"
        )
    except Exception as e:
        log.exception("convert error: %s", e)

# ---- AI (tanpa /ask)
async def ai_answer(text: str) -> Optional[str]:
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": text}],
            max_tokens=300,
            temperature=0.5,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("AI error: %s", e)
        return None

# ---- Airdrop
def airdrop_pages() -> int:
    return max(1, math.ceil(len(AIRDROP_CACHE) / AIRDROP_PAGE_SIZE))

def render_airdrops_page(page: int) -> Tuple[str, InlineKeyboardMarkup]:
    n = len(AIRDROP_CACHE)
    pmax = airdrop_pages()
    page = max(1, min(page, pmax))
    s = (page - 1) * AIRDROP_PAGE_SIZE
    e = min(n, s + AIRDROP_PAGE_SIZE)
    items = AIRDROP_CACHE[s:e]

    if not items:
        return ("Belum ada data. Jalankan /airupdate dulu.", InlineKeyboardMarkup([]))

    lines = [f"<b>Airdrop (hal {page}/{pmax})</b>"]
    for it in items:
        nm = html.escape(it.get("name", ""))
        src = html.escape(it.get("source", ""))
        rew = html.escape(it.get("reward", "")) or "-"
        ch  = html.escape(it.get("chain", "")) or "-"
        lines.append(f"• <b>{nm}</b>  <i>({src})</i>\n  Reward: {rew}\n  Chain: {ch}")

    kb = [
        [
            InlineKeyboardButton("⬅️ Prev", callback_data=f"air_prev_{page}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"air_next_{page}"),
        ]
    ]
    return ("\n".join(lines), InlineKeyboardMarkup(kb))

async def airupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Update airdrops…")
    try:
        items = scrape_airdrops_io()
        global AIRDROP_CACHE
        AIRDROP_CACHE = merge_unique(items)
        await msg.edit_text(f"✅ Selesai. Terkumpul {len(AIRDROP_CACHE)} airdrop.\n/airdrops untuk daftar.")
    except Exception as e:
        log.warning("airupdate gagal: %s", e)
        await msg.edit_text("❌ Gagal update (sumber mungkin memblokir atau down).")

async def airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not AIRDROP_CACHE:
        await update.message.reply_text("Belum ada data. Jalankan /airupdate dulu.")
        return
    txt, kb = render_airdrops_page(1)
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=kb)

async def air_nav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    m = re.match(r"air_(prev|next)_(\d+)", data)
    if not m:
        return
    kind, cur = m.groups()
    cur = int(cur)
    page = cur - 1 if kind == "prev" else cur + 1
    txt, kb = render_airdrops_page(page)
    try:
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
    except:
        # kalau tidak bisa edit (mis. sudah terlalu lama), kirim baru
        await q.message.reply_text(txt, parse_mode="HTML", reply_markup=kb)

async def tugas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>")
        return
    kw = " ".join(ctx.args).lower().strip()
    if not AIRDROP_CACHE:
        await update.message.reply_text("Belum ada data. Jalankan /airupdate dulu.")
        return
    hits = [a for a in AIRDROP_CACHE if kw in a.get("name", "").lower() or kw in a.get("slug", "")]
    if not hits:
        await update.message.reply_text("❌ Tidak ditemukan.")
        return
    it = hits[0]
    name = html.escape(it.get("name", "Airdrop"))
    url  = html.escape(it.get("url", ""))
    rew  = html.escape(it.get("reward", "")) or "-"
    ch   = html.escape(it.get("chain", "")) or "-"
    src  = html.escape(it.get("source", "")) or "-"
    txt = (
        f"<b>{name}</b>\n"
        f"Sumber: <i>{src}</i>\n"
        f"Reward: {rew}\n"
        f"Chain: {ch}\n"
        f"Link: {url}\n\n"
        "Catatan: tugas detail bisa berubah; buka link untuk instruksi lengkap."
    )
    await update.message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)

# ====== MENU CALLBACK RINGKAS ======
async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "menu_price":
        txt = ("Contoh:\n"
               "• /price btc usdt\n"
               "• /prices btc,eth idr\n"
               "• /convert 0.25 btc idr")
    elif data == "menu_conv":
        txt = "Format convert: /convert 12.3 sol usd"
    elif data == "menu_air":
        txt = "• /airupdate untuk update\n• /airdrops untuk daftar\n• /tugas <keyword> untuk detail"
    else:
        txt = "Ketik pertanyaan apa saja untuk AI."
    await q.edit_message_text(txt)

# ====== TEXT ROUTER ======
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # 1) amount pair: "0.25 btc idr"
    m = AMOUNT_PAIR_PATTERN.match(text)
    if m:
        amt = float(m.group(1))
        sym = m.group(2)
        fiat = (m.group(3) or get_chat_fiat(chat_id)).lower()
        # gunakan handler convert secara langsung
        class _Args:
            args = [str(amt), sym, fiat]
        ctx.args = _Args.args
        await convert(update, ctx)
        return

    # 2) pair: "btc idr" atau "btc/usdt" atau "eth"
    m = PAIR_PATTERN.match(text)
    if m:
        sym = m.group(1)
        fiat = (m.group(2) or get_chat_fiat(chat_id)).lower()
        await _reply_price(update, sym, fiat)
        return

    # 3) AI fallback
    ans = await ai_answer(text)
    if ans:
        await update.message.reply_text(ans)

async def _reply_price(update: Update, sym: str, fiat: str):
    # hemat VPS: kalau fiat tidak didukung -> tidak balas
    if fiat not in FIAT_ALLOWED:
        return
    cid = norm_symbol(sym)
    try:
        data = cg_simple_price([cid], fiat)
        node = data.get(cid)
        if not node or node.get(fiat) is None:
            return  # coin/pair tidak tersedia -> diam
        val = float(node[fiat])
        chg = node.get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
        await update.message.reply_text(
            f"💰 {sym.upper()} = {fmt_price(val, fiat)}{chg_txt}"
        )
    except Exception as e:
        log.warning("price error: %s", e)

# ====== APP ======
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("convert", convert))
    app.add_handler(CommandHandler("airupdate", airupdate))
    app.add_handler(CommandHandler("airdrops", airdrops))
    app.add_handler(CommandHandler("tugas", tugas))

    # callbacks
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(air_nav, pattern=r"^air_(prev|next)_\d+$"))

    # text router (AI & pair bebas)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN kosong. Set di .env")
    app = build_app()
    log.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

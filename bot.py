# bot.py
import os, re, logging, requests
from typing import List, Tuple, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ===================== OpenAI (opsional) =====================
# Gunakan library OpenAI baru: pip install openai
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # supaya tidak error kalau openai belum terpasang

# ===================== Konfigurasi dasar =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_TOKEN_KAMU")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "ISI_API_KEY_KAMU")
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()  # bisa diubah via /setfiat

# Inisialisasi OpenAI client bila kunci tersedia & lib ada
client = None
if OpenAI and OPENAI_API_KEY and not OPENAI_API_KEY.startswith("ISI_"):
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("OpenAI init fail:", e)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ===================== Utilitas =====================
# Pemetaan ticker→id CoinGecko (boleh ditambah)
SYMBOL_MAP: Dict[str, str] = {
    # layer-1 / bluechips
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "ada": "cardano",
    "xrp": "ripple", "dot": "polkadot", "avax": "avalanche-2", "atom": "cosmos",
    # exchange / stable
    "bnb": "binancecoin", "trx": "tron", "usdt": "tether", "usdc": "usd-coin",
    # layer-2 / ekosistem populer
    "matic": "polygon", "arb": "arbitrum", "op": "optimism", "base": "base-protocol",
    # meme / others
    "doge": "dogecoin", "shib": "shiba-inu", "pepe": "pepe",
}

# sebagian vs_currency CoinGecko tidak mendukung "usdt".
# Kita map "usdt" → "usd" untuk query, tapi label tetap USDT.
def _fiat_for_query(fiat: str) -> Tuple[str, str]:
    f = fiat.lower()
    if f == "usdt":
        return "usd", "USDT"  # query pakai USD, label tampilkan USDT
    return f, f.upper()

def norm_symbol(sym: str) -> str:
    s = (sym or "").lower()
    return SYMBOL_MAP.get(s, s)

def fetch_price(symbol_ids: List[str], fiat: str) -> dict:
    if not symbol_ids:
        return {}
    q_fiat, _ = _fiat_for_query(fiat)
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        r = requests.get(
            url,
            params={
                "ids": ",".join(symbol_ids),
                "vs_currencies": q_fiat,
                "include_24hr_change": "true",
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("fetch_price error")
        return {}

def fmt_price(val, fiat: str) -> str:
    # angka besar → 2 desimal, kecil → 6 desimal
    if val is None:
        return "-"
    dec = 2 if float(val) >= 1 else 6
    _, fiat_label = _fiat_for_query(fiat)
    return f"{float(val):,.{dec}f} {fiat_label}"

# Pattern untuk "harga/price XXX YYY" dan pasangan sederhana
RE_HARGA = re.compile(r"^(?:harga|price)\s+([a-z0-9]+)(?:\s+([a-z0-9]+))?$", re.I)
PAIR_PATTERN = re.compile(r"^([a-z0-9]+)[/ ]([a-z0-9]+)$", re.I)

# ===================== Command handlers =====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("📊 Top & Indikator", callback_data="menu_top")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di AirdropCore Bot!\nPilih menu di bawah ini 👇",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah:\n"
        "• /price <symbol> [fiat]\n"
        "  contoh: /price btc usdt\n"
        "• /prices <sym1,sym2,...> [fiat]\n"
        "  contoh: /prices btc,eth,idr\n"
        "• /convert <jumlah> <symbol> <fiat>\n"
        "  contoh: /convert 0.25 btc idr\n"
        "• /setfiat idr|usd|usdt|eur\n"
        "• /ask <pertanyaan>\n"
        "• /status"
    )

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cg = "OK"
    ai = "ON" if client else "OFF"
    await update.message.reply_text(
        f"🩺 Status:\n"
        f"• CoinGecko: {cg}\n"
        f"• FIAT: {FIAT_DEFAULT.upper()}\n"
        f"• OpenAI: {ai}"
    )

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT  # <-- HARUS DI BARIS PALING ATAS DALAM FUNGSI

    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: {FIAT_DEFAULT.upper()}\n"
            "Format: /setfiat idr|usd|usdt|eur"
        )
        return

    fiat = ctx.args[0].lower()
    if fiat not in {"idr", "usd", "usdt", "eur"}:
        await update.message.reply_text("❌ Fiat tidak valid. Pilih: idr|usd|usdt|eur")
        return

    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default diset ke {fiat.upper()}")

async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not client:
        await update.message.reply_text("❌ OpenAI belum dikonfigurasi.")
        return
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>")
        return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.5,
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\nContoh: /price btc usdt")
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await _reply_price(update, sym, fiat)

async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices <sym1,sym2,...> [fiat]")
        return
    raw = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    syms = [s.strip() for s in re.split(r"[,\s]+", raw) if s.strip()]
    ids = [norm_symbol(s) for s in syms]
    data = fetch_price(ids, fiat)
    if not data:
        await update.message.reply_text("❌ Data tidak ditemukan.")
        return
    lines = []
    for s, cid in zip(syms, ids):
        val = (data.get(cid) or {}).get(_fiat_for_query(fiat)[0])
        chg = (data.get(cid) or {}).get(f"{_fiat_for_query(fiat)[0]}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
        lines.append(f"• {s.upper():<6} = {fmt_price(val, fiat)}{chg_txt}")
    await update.message.reply_text("📈 Harga:\n" + "\n".join(lines))

async def convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <jumlah> <symbol> <fiat>\nContoh: /convert 0.25 btc idr")
        return
    try:
        amount = float(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Jumlah tidak valid.")
        return
    sym = ctx.args[1]
    fiat = ctx.args[2].lower()
    cid = norm_symbol(sym)
    data = fetch_price([cid], fiat)
    q_fiat, _ = _fiat_for_query(fiat)
    if cid not in data or q_fiat not in data[cid]:
        await update.message.reply_text("❌ Data tidak ditemukan.")
        return
    px = float(data[cid][q_fiat])
    total = amount * px
    await update.message.reply_text(
        f"🔁 {amount:g} {sym.upper()} ≈ {fmt_price(total, fiat)} (1 {sym.upper()} = {fmt_price(px, fiat)})"
    )

# ===================== Menu callback =====================
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = (q.data or "").strip()
    await q.answer()
    if data == "menu_price":
        txt = (
            "Contoh:\n"
            "• /price btc usdt\n"
            "• /prices btc,eth idr\n"
            "• /convert 0.25 btc idr"
        )
    elif data == "menu_top":
        txt = (
            "Contoh indikator:\n"
            "• /top 10 (belum diaktifkan)\n"
            "• /dominance (coming soon)\n"
            "• /fear (coming soon)"
        )
    elif data == "menu_air":
        txt = (
            "Airdrop (coming soon):\n"
            "• /airdrops\n"
            "• /airdrops zk\n"
            "• /hunt monad"
        )
    else:
        txt = "Tanya AI: /ask <pertanyaan>"
    await q.edit_message_text(txt)

# ===================== Router pesan teks =====================
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Pola "harga/price <sym> [fiat]"
    m = RE_HARGA.match(text)
    if m:
        sym, fiat = m.groups()
        fiat = (fiat or FIAT_DEFAULT).lower()
        await _reply_price(update, sym, fiat)
        return

    # Pola "<sym>/<fiat>" atau "<sym> <fiat>"
    m2 = PAIR_PATTERN.match(text)
    if m2:
        sym, fiat = m2.groups()
        await _reply_price(update, sym, fiat.lower())
        return

    # Fallback ke AI bila tersedia
    if client and text:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": text}],
                max_tokens=220,
                temperature=0.6,
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer)
            return
        except Exception as e:
            log.exception("AI fallback error")
            await update.message.reply_text(f"❌ Error: {e}")

    # Bila tidak cocok apa pun
    await update.message.reply_text("Ketik contoh: harga btc usdt  /  /price eth idr  /  /help")

# ===================== Helper reply =====================
async def _reply_price(update: Update, sym: str, fiat: str):
    try:
        cid = norm_symbol(sym)
        data = fetch_price([cid], fiat)
        q_fiat, _ = _fiat_for_query(fiat)
        if cid not in data or q_fiat not in data[cid]:
            await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan.")
            return
        price_val = data[cid][q_fiat]
        chg = data[cid].get(f"{q_fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price_val, fiat)}{chg_txt}")
    except Exception:
        log.exception("price error")
        await update.message.reply_text("❌ Error mengambil harga.")

# ===================== Runner =====================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("convert", convert))
    app.add_handler(CallbackQueryHandler(on_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

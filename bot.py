import os, re, logging, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from openai import OpenAI

# ===== Konfigurasi dasar =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_TOKEN_KAMU")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "ISI_API_KEY_KAMU")
FIAT_DEFAULT = "usd"

client = None
if OPENAI_API_KEY and not OPENAI_API_KEY.startswith("ISI"):
    client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ===== Utilitas =====
def fetch_price(symbols, fiat="usd"):
    url = f"https://api.coingecko.com/api/v3/simple/price"
    try:
        resp = requests.get(url, params={"ids": ",".join(symbols),
                                         "vs_currencies": fiat,
                                         "include_24hr_change": "true"}, timeout=20)
        return resp.json()
    except Exception as e:
        log.exception("fetch_price error")
        return {}

def fmt_price(val, fiat):
    return f"{val:,.4f} {fiat.upper()}"

def norm_symbol(sym):
    mapping = {
        "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin",
        "usdt": "tether", "usdc": "usd-coin", "sol": "solana",
        "ada": "cardano", "xrp": "ripple", "dot": "polkadot",
        "doge": "dogecoin", "trx": "tron", "matic": "polygon"
    }
    return mapping.get(sym.lower(), sym.lower())

PAIR_PATTERN = re.compile(r"^([a-zA-Z0-9]+)[/ ]?([a-zA-Z0-9]+)?$")
PRICE_WORD = re.compile(r"^[a-zA-Z]{2,6}(/[a-zA-Z]{2,6})?$")

# ===== Command handlers =====
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("📊 Top & Indikator", callback_data="menu_top")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di AirdropCore Bot!\nGunakan menu di bawah ini:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah tersedia:\n"
        "/price btc usdt – harga 1 coin\n"
        "/prices btc,eth idr – harga beberapa coin\n"
        "/convert 0.25 btc idr – konversi jumlah\n"
        "/setfiat idr|usd|usdt|eur – ganti fiat default\n"
        "/ask <pertanyaan> – tanya AI\n"
    )

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: {FIAT_DEFAULT.upper()}\n"
            "Format: /setfiat idr|usd|usdt|eur"
        )
        return
    fiat = ctx.args[0].lower()
    if fiat not in {"idr","usd","usdt","eur"}:
        await update.message.reply_text("❌ Fiat tidak valid.")
        return
    global FIAT_DEFAULT
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default diset ke {fiat.upper()}")

async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args)
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>")
        return
    if not client:
        await update.message.reply_text("❌ API Key OpenAI belum diatur.")
        return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=400, temperature=0.4
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        logging.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\ncontoh: /price btc usdt")
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await _reply_price(update, sym, fiat)

async def _reply_price(update: Update, sym: str, fiat: str):
    try:
        cid = norm_symbol(sym)
        data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]:
            await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan.")
            return
        price_val = data[cid][fiat]
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price_val, fiat)}{chg_txt}")
    except Exception as e:
        logging.exception("price error")
        await update.message.reply_text(f"❌ Error harga: {e}")

# ===== Menu callback =====
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = ("Contoh: /price btc usdt\n"
               "• /price btc usdt\n"
               "• /prices btc,eth idr\n"
               "• /convert 0.25 btc idr")
    elif data == "menu_top":
        txt = ("• /top 10\n"
               "• /dominance\n"
               "• /fear\n"
               "• /gas")
    elif data == "menu_air":
        txt = ("• /airdrops\n"
               "• /airdrops zk\n"
               "• /hunt monad")
    else:
        txt = "• /ask pertanyaan apa saja"
    await q.edit_message_text(txt)

# ===== Router untuk pesan teks =====
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = PAIR_PATTERN.match(text) if PRICE_WORD.match(text) else None
    if m:
        sym, fiat = m.groups()
        fiat = (fiat or FIAT_DEFAULT).lower()
        await _reply_price(update, sym, fiat)
        return
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": text}],
                max_tokens=220, temperature=0.6
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer)
        except Exception as e:
            logging.exception("AI fallback error")
            await update.message.reply_text(f"❌ Error: {e}")

# ===== Runner =====
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CallbackQueryHandler(on_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

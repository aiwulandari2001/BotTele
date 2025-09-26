#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, re, logging, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# --- OpenAI opsional (agar tidak crash kalau modul belum terpasang) ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ===== Konfigurasi dasar =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_TOKEN_KAMU")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "ISI_API_KEY_KAMU")
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

client = None
if OpenAI and OPENAI_API_KEY and not OPENAI_API_KEY.startswith("ISI_"):
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        client = None
        logging.warning("OpenAI init gagal: %s", e)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ===== Utilitas =====
def fetch_price(symbols, fiat="usd"):
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        resp = requests.get(
            url,
            params={
                "ids": ",".join(symbols),
                "vs_currencies": fiat,
                "include_24hr_change": "true",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.exception("fetch_price error")
        return {}

def fmt_price(val, fiat):
    try:
        if fiat == "idr":
            return f"Rp {val:,.0f}".replace(",", ".")
        if fiat in ("usd", "usdt"):
            return f"${val:,.2f}"
        if fiat == "eur":
            return f"€{val:,.2f}"
    except Exception:
        pass
    return f"{val:,.4f} {fiat.upper()}"

def norm_symbol(sym):
    mapping = {
        "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin",
        "usdt": "tether", "usdc": "usd-coin", "sol": "solana",
        "ada": "cardano", "xrp": "ripple", "dot": "polkadot",
        "doge": "dogecoin", "trx": "tron", "matic": "matic-network",
        "ton": "toncoin", "avax": "avalanche-2", "ltc": "litecoin",
        "shib": "shiba-inu", "link": "chainlink", "op": "optimism",
        "arb": "arbitrum", "sui": "sui", "sei": "sei-network",
        "near": "near", "atom": "cosmos", "cake": "pancakeswap-token"
    }
    return mapping.get(sym.lower(), sym.lower())

# Deteksi teks natural:
PRICE_WORD = re.compile(r"(?i)^(harga|price)\b")
PAIR_PATTERN = re.compile(r"(?i)^(?:harga|price)\s+([a-z0-9$.,]+)(?:[\/\s]+([a-z]{2,6}))?$")
CONVERT_PATTERN = re.compile(r"(?i)^(\d+(?:[.,]\d+)?)\s*([a-z0-9$]{2,12})\s*(?:ke|to)\s*([a-z]{2,6})$")

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
        reply_markup=InlineKeyboardMarkup(kb),
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
    if fiat not in {"idr", "usd", "usdt", "eur"}:
        await update.message.reply_text("❌ Fiat tidak valid. Pilih: idr|usd|usdt|eur")
        return
    global FIAT_DEFAULT
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default diset ke {fiat.upper()}")

async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>")
        return
    if not client:
        await update.message.reply_text("❌ AI nonaktif (OPENAI_API_KEY kosong / modul openai belum terpasang).")
        return
    try:
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400, temperature=0.4,
            timeout=30  # timeout network
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

async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices <sym1,sym2,...> [fiat]\ncontoh: /prices btc,eth idr")
        return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await _reply_prices(update, syms, fiat)

async def convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amount> <coin> <fiat>\ncontoh: /convert 0.25 btc idr")
        return
    try:
        amount = float(str(ctx.args[0]).replace(",", "."))
    except ValueError:
        await update.message.reply_text("Jumlah tidak valid.")
        return
    sym = ctx.args[1]
    fiat = ctx.args[2].lower()
    await _reply_convert(update, amount, sym, fiat)

# ===== Implementasi =====
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

async def _reply_prices(update: Update, syms, fiat: str):
    try:
        ids = [norm_symbol(s) for s in syms]
        data = fetch_price(ids, fiat)
        lines = []
        for s, cid in zip(syms, ids):
            if cid in data and fiat in data[cid]:
                p = data[cid][fiat]
                chg = data[cid].get(f"{fiat}_24h_change")
                chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
                lines.append(f"{s.upper():>5} = {fmt_price(p, fiat)}{chg_txt}")
            else:
                lines.append(f"{s.upper():>5} = n/a")
        await update.message.reply_text("📊 Harga:\n" + "\n".join(lines))
    except Exception as e:
        logging.exception("prices error")
        await update.message.reply_text(f"❌ Error harga: {e}")

async def _reply_convert(update: Update, amount: float, sym: str, fiat: str):
    try:
        cid = norm_symbol(sym)
        data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]:
            await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan.")
            return
        p = float(data[cid][fiat])
        total = amount * p
        await update.message.reply_text(
            f"🔁 {amount:g} {sym.upper()} ≈ {fmt_price(total, fiat)} (1 {sym.upper()} = {fmt_price(p, fiat)})"
        )
    except Exception as e:
        logging.exception("convert error")
        await update.message.reply_text(f"❌ Error konversi: {e}")

# ===== Menu callback =====
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
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
            "• /setfiat idr|usd|usdt|eur\n"
            "• /ask <pertanyaan>\n"
            "• /help"
        )
    elif data == "menu_air":
        txt = (
            "Airdrop (versi ringkas). Kamu bisa tanya pakai /ask untuk ringkasan airdrop/project."
        )
    else:
        txt = "• /ask pertanyaan apa saja"
    await q.edit_message_text(txt)

# ===== Router untuk pesan teks =====
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # "0.1 btc ke idr"
    m2 = CONVERT_PATTERN.match(text)
    if m2:
        amt = float(m2.group(1).replace(",", "."))
        sym, fiat = m2.group(2), m2.group(3).lower()
        await _reply_convert(update, amt, sym, fiat)
        return

    # "harga btc usdt" / "price eth usd"
    m = PAIR_PATTERN.match(text) if PRICE_WORD.match(text) else None
    if m:
        sym, fiat = m.groups()
        fiat = (fiat or FIAT_DEFAULT).lower()
        await _reply_price(update, sym, fiat)
        return

    # fallback ke AI
    if client:
        try:
            resp = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
                messages=[{"role": "user", "content": text}],
                max_tokens=220, temperature=0.6,
                timeout=30
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer)
        except Exception as e:
            logging.exception("AI fallback error")
            await update.message.reply_text(f"❌ Error: {e}")

# ===== Runner =====
def main():
    if not BOT_TOKEN or BOT_TOKEN.startswith("ISI_"):
        print("ERROR: BOT_TOKEN belum diisi. Set environment atau .env Anda.")
        raise SystemExit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
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

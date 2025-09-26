
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, logging, re, time, io
from pathlib import Path
from typing import Dict, List
from datetime import datetime, timezone

import requests
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, InlineQueryHandler, CallbackQueryHandler

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

from utils.ai import init_openai, chat as ai_chat
from utils.crypto import norm_symbol, fmt_price, fetch_price, COINGECKO_MARKETS, COINGECKO_GLOBAL, COINGECKO_MC, COINGECKO_OHLC
from utils.airdrops import fetch_airdrops

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
if load_dotenv and ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH, override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN","").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY","").strip()
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY","").strip()
DEFAULT_GLOBAL_FIAT = os.getenv("FIAT_DEFAULT","usd").lower()

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("airdropcore-plus")

SETTINGS_FILE  = BASE_DIR / "data" / "settings.json"
ALERTS_FILE    = BASE_DIR / "data" / "alerts.json"
PORTFOLIO_FILE = BASE_DIR / "data" / "portfolio.json"
SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_json(p, default):
    if p.exists():
        try: return json.loads(p.read_text("utf-8"))
        except Exception: return default
    return default
def save_json(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

SETTINGS  = load_json(SETTINGS_FILE, {})
ALERTS    = load_json(ALERTS_FILE, [])
PORTFOLIO = load_json(PORTFOLIO_FILE, {})

def get_chat_fiat(chat_id: int) -> str:
    return SETTINGS.get(str(chat_id), {}).get("fiat", DEFAULT_GLOBAL_FIAT)
def set_chat_fiat(chat_id: int, fiat: str):
    SETTINGS.setdefault(str(chat_id), {})["fiat"] = fiat
    save_json(SETTINGS_FILE, SETTINGS)

_last_global = 0.0
_last_user: Dict[int, float] = {}
def allowed_now(user_id: int) -> bool:
    global _last_global
    now = time.time()
    if now - _last_global < 1.0: return False
    if now - _last_user.get(user_id, 0) < 1.0: return False
    _last_global = now; _last_user[user_id] = now; return True

def md2_escape(text: str) -> str:
    import re as _re
    return _re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

async def send_typing(ctx, chat_id):
    try: await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception: pass

HELP_TEXT_HTML = (
    "🤖 <b>AirdropCore SUPER Bot</b> — AI + Crypto + Airdrop Hunter<br/><br/>"
    "Perintah cepat:<br/>"
    "• /ask &lt;pertanyaan&gt;<br/>"
    "• /price &lt;sym&gt; [fiat] — contoh: /price btc usdt<br/>"
    "• /prices &lt;sym1,sym2,...&gt; [fiat]<br/>"
    "• /convert &lt;amount&gt; &lt;sym&gt; &lt;fiat&gt;<br/>"
    "• /top, /dominance, /fear, /gas<br/>"
    "• /ohlc, /chart<br/>"
    "• /alerts, /alert add|del<br/>"
    "• /addport, /delport, /portfolio, /clearport<br/>"
    "• /airdrops [keyword], /hunt &lt;keyword&gt;"
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("📊 Top", callback_data="menu_top")],
        [InlineKeyboardButton("🎁 Airdrops", callback_data="menu_air"),
         InlineKeyboardButton("🤖 Tanya AI", callback_data="menu_ai")]
    ])
    await update.message.reply_html(HELP_TEXT_HTML, reply_markup=kb)
help_cmd = start

async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()

    if data == "menu_price":
        txt = """Contoh:
• /price btc usdt
• /prices btc,eth idr
• /convert 0.25 btc idr"""
    elif data == "menu_top":
        txt = """• /top 10
• /dominance
• /fear
• /gas"""
    elif data == "menu_air":
        txt = """• /airdrops
• /airdrops zk
• /hunt monad"""
    else:
        txt = "• /ask pertanyaan apa saja"

    await q.edit_message_text(txt)
async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fiat = get_chat_fiat(update.effective_chat.id)
    t0=time.time()
    try:
        from utils.crypto import fetch_price
        fetch_price(["bitcoin"], fiat)
        cg = f"✅ {(time.time()-t0)*1000:.0f} ms"
    except Exception as e:
        cg=f"❌ {e.__class__.__name__}"
    await update.message.reply_text(f"""🩺 Status:
• CoinGecko: {cg}
• FIAT: {fiat.upper()}""")

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(f"""FIAT saat ini: {get_chat_fiat(update.effective_chat.id).upper()}
Format: /setfiat idr|usd|usdt|eur""")
return

fiat = ctx.args[0].lower()
if fiat not in {"idr", "usd", "usdt", "eur"}:
    await update.message.reply_text("❌ FIAT tidak valid. Pilih salah satu: idr, usd, usdt, eur.")
    returnset_chat_fiat(update.effective_chat.id, fiat)
    await update.message.reply_text(f"✅ FIAT default di-set ke {fiat.upper()}")

# === AI ===
async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args) if ctx.args else ""
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>"); return
    await send_typing(ctx, update.effective_chat.id)
    await update.message.reply_text(ai_chat(prompt), disable_web_page_preview=True)

# === Price & market ===
async def _reply_price(update: Update, sym: str, fiat: str):
    from utils.crypto import fetch_price
    try:
        cid = norm_symbol(sym); data = fetch_price([cid], fiat.lower())
        if cid not in data or fiat.lower() not in data[cid]:
            await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan."); return
        p = data[cid][fiat.lower()]; chg = data[cid].get(f"{fiat.lower()}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(p, fiat)}{chg_txt}")
    except Exception as e:
        logging.exception("price error")
        await update.message.reply_text(f"❌ Error harga: {e}")

async def _reply_prices(update: Update, syms: List[str], fiat: str):
    from utils.crypto import fetch_price
    try:
        ids = [norm_symbol(s) for s in syms]; data = fetch_price(ids, fiat.lower()); lines = []
        for s, cid in zip(syms, ids):
            if cid in data and fiat.lower() in data[cid]:
                p = data[cid][fiat.lower()]; chg = data[cid].get(f"{fiat.lower()}_24h_change")
                chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
                lines.append(f"{s.upper():>5} = {fmt_price(p, fiat)}{chg_txt}")
            else:
                lines.append(f"{s.upper():>5} = n/a")
        await update.message.reply_text("📊 Harga:
" + "
".join(lines))
    except Exception as e:
        logging.exception("prices error")
        await update.message.reply_text(f"❌ Error harga: {e}")

async def _reply_convert(update: Update, amount: float, sym: str, fiat: str):
    from utils.crypto import fetch_price
    try:
        cid = norm_symbol(sym); data = fetch_price([cid], fiat.lower())
        if cid not in data or fiat.lower() not in data[cid]:
            await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan."); return
        p = data[cid][fiat.lower()]; total = amount * float(p)
        await update.message.reply_text(f"🔁 {amount:g} {sym.upper()} ≈ {fmt_price(total, fiat.lower())} (1 {sym.upper()} = {fmt_price(p, fiat.lower())})")
    except Exception as e:
        logging.exception("convert error")
        await update.message.reply_text(f"❌ Error konversi: {e}")

async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]
ex: /price btc usdt"); return
    sym = ctx.args[0]; fiat = (ctx.args[1] if len(ctx.args) > 1 else get_chat_fiat(update.effective_chat.id)).lower()
    await _reply_price(update, sym, fiat)

async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices <sym1,sym2,...> [fiat]
ex: /prices btc,eth idr"); return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else get_chat_fiat(update.effective_chat.id)).lower()
    await _reply_prices(update, syms, fiat)

async def convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amount> <coin> <fiat>
ex: /convert 0.25 btc idr"); return
    try: amount = float(str(ctx.args[0]).replace(",", "."))
    except ValueError: await update.message.reply_text("Jumlah tidak valid."); return
    sym = ctx.args[1]; fiat = ctx.args[2].lower()
    await _reply_convert(update, amount, sym, fiat)

async def top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = requests.get(COINGECKO_MARKETS, params={
        "vs_currency":"usd","order":"market_cap_desc","per_page":10,"page":1,"price_change_percentage":"24h"
    }, timeout=20)
    r.raise_for_status(); coins = r.json()
    lines = []
    for i,c in enumerate(coins,1):
        sym = c.get("symbol","").upper(); price = c.get("current_price"); chg = c.get("price_change_percentage_24h")
        chg_txt = f"{chg:+.2f}%" if isinstance(chg,(int,float)) else "n/a"
        lines.append(f"{i:>2}. {sym:<6} ${price:,.2f}  ({chg_txt})")
    await update.message.reply_text("🏆 Top Market Cap:
" + "
".join(lines))

async def dominance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = requests.get(COINGECKO_GLOBAL, timeout=15); r.raise_for_status(); data = r.json()["data"]["market_cap_percentage"]
    btc_dom = data.get("btc"); await update.message.reply_text(f"👑 BTC Dominance: {btc_dom:.2f}%")

async def fear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = requests.get("https://api.alternative.me/fng/", timeout=15).json()
    val = r["data"][0]["value"]; classification = r["data"][0]["value_classification"]
    await update.message.reply_text(f"📉 Fear & Greed Index: {val} ({classification})")

async def gas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ETHERSCAN_API_KEY:
        await update.message.reply_text("⚠️ ETHERSCAN_API_KEY belum diisi."); return
    r = requests.get("https://api.etherscan.io/api",
        params={"module":"gastracker","action":"gasoracle","apikey":ETHERSCAN_API_KEY}, timeout=15).json()
    if r.get("status") != "1": await update.message.reply_text("❌ Tidak bisa ambil gas data."); return
    g = r["result"]
    await update.message.reply_text(f"⛽ Gas ETH
• Safe: {g['SafeGasPrice']} gwei
• Propose: {g['ProposeGasPrice']} gwei
• Fast: {g['FastGasPrice']} gwei")

# === OHLC & Chart ===
async def ohlc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Format: /ohlc <sym> <fiat> [days]
contoh: /ohlc btc usd 7"); return
    sym = ctx.args[0].lower(); fiat = ctx.args[1].lower()
    days = 1
    if len(ctx.args) >= 3:
        try: days = int(ctx.args[2])
        except ValueError: days = 1
    if days not in (1,7,14,30,90,180,365): days = 1
    url = COINGECKO_OHLC.format(id=norm_symbol(sym))
    try:
        r = requests.get(url, params={"vs_currency":fiat,"days":days}, timeout=20); r.raise_for_status(); arr = r.json()
        if not isinstance(arr,list) or not arr: await update.message.reply_text("Data OHLC tidak tersedia."); return
        last = arr[-5:] if len(arr)>=5 else arr
        rows = []
        for t,o,h,l,c in last:
            ts = datetime.fromtimestamp(t/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            rows.append(f"{ts} UTC  O:{fmt_price(o,fiat)}  H:{fmt_price(h,fiat)}  L:{fmt_price(l,fiat)}  C:{fmt_price(c,fiat)}")
        await update.message.reply_text(f"🕯️ OHLC {sym.upper()}/{fiat.upper()} (days={days})
" + "\n".join(rows))
    except Exception as e:
        logging.exception("ohlc error")
        await update.message.reply_text(f"❌ Error OHLC: {e}")

async def chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Format: /chart <sym> <fiat> [days]
contoh: /chart btc usd 30"); return
    sym = ctx.args[0].lower(); fiat = ctx.args[1].lower(); days = int(ctx.args[2]) if len(ctx.args)>=3 and ctx.args[2].isdigit() else 30
    try:
        r = requests.get(COINGECKO_MC.format(id=norm_symbol(sym)), params={"vs_currency":fiat,"days":days}, timeout=20); r.raise_for_status()
        data = r.json(); prices = data.get("prices") or []
        if not prices: await update.message.reply_text("Data chart tidak tersedia."); return
        xs = [datetime.fromtimestamp(p[0]/1000, tz=timezone.utc) for p in prices]
        ys = [p[1] for p in prices]
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig = plt.figure()
        plt.plot(xs, ys)
        plt.title(f"{sym.upper()}/{fiat.upper()} — {days}D")
        plt.xlabel("Time (UTC)"); plt.ylabel(f"Price ({fiat.upper()})")
        plt.tight_layout()
        buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=140); plt.close(fig); buf.seek(0)
        await update.message.reply_photo(photo=InputFile(buf, filename=f"{sym}_{fiat}_{days}d.png"))
    except Exception as e:
        logging.exception("chart error")
        await update.message.reply_text(f"❌ Error chart: {e}")

# === Alerts ===
async def alerts_list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; mine = [a for a in ALERTS if a["chat_id"] == chat_id]
    if not mine: await update.message.reply_text("📭 Tidak ada alert aktif."); return
    lines = [f"{i}. {a['sym'].upper()} {a['fiat'].upper()} {a['op']} {a['price']}" for i,a in enumerate(mine,1)]
    await update.message.reply_text("⏰ Alerts kamu:\n" + "\n".join(lines))

async def alert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format:\n• /alert add <sym> <fiat> above|below <price>\n• /alert del <index>\n• /alerts"); return
    sub = ctx.args[0].lower(); chat_id = update.effective_chat.id
    if sub == "add":
        if len(ctx.args) < 5:
            await update.message.reply_text("Format: /alert add <sym> <fiat> above|below <price>"); return
        sym = ctx.args[1].lower(); fiat = ctx.args[2].lower(); op = ctx.args[3].lower()
        try: price = float(str(ctx.args[4]).replace(",", ""))
        except ValueError: await update.message.reply_text("Harga tidak valid."); return
        if op not in ("above","below"): await update.message.reply_text("Operator harus 'above' atau 'below'."); return
        ALERTS.append({"chat_id":chat_id,"sym":sym,"cid":norm_symbol(sym),"fiat":fiat,"op":op,"price":price})
        save_json(ALERTS_FILE, ALERTS)
        await update.message.reply_text(f"✅ Alert: {sym.upper()} {fiat.upper()} {op} {price} ditambahkan.")
        return
    if sub == "del":
        if len(ctx.args) < 2: await update.message.reply_text("Format: /alert del <index>"); return
        mine_idx = [i for i,a in enumerate(ALERTS) if a["chat_id"] == chat_id]
        if not mine_idx: await update.message.reply_text("📭 Tidak ada alert aktif."); return
        try: idx = int(ctx.args[1]) - 1
        except ValueError: await update.message.reply_text("Index tidak valid."); return
        if idx < 0 or idx >= len(mine_idx): await update.message.reply_text("Index di luar jangkauan."); return
        real_idx = mine_idx[idx]; a = ALERTS.pop(real_idx); save_json(ALERTS_FILE, ALERTS)
        await update.message.reply_text(f"🗑️ Alert dihapus: {a['sym'].upper()} {a['fiat'].upper()} {a['op']} {a['price']}"); return
    await update.message.reply_text("Perintah tidak dikenal. Gunakan /alerts untuk melihat daftar.")

async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    if not ALERTS: return
    by_fiat = {}
    for a in ALERTS: by_fiat.setdefault(a["fiat"], []).append(a)
    to_remove = []
    for fiat, arr in by_fiat.items():
        ids = list({a["cid"] for a in arr})
        try:
            data = fetch_price(ids, fiat)
        except Exception:
            continue
        for a in list(ALERTS):
            if a["fiat"] != fiat: continue
            cid = a["cid"]
            try: cur = data[cid][fiat]
            except Exception: continue
            hit = (a["op"] == "above" and cur >= a["price"]) or (a["op"] == "below" and cur <= a["price"])
            if hit:
                try:
                    txt = f"⏰ Alert hit: {a['sym'].upper()} {fiat.upper()} {a['op']} {a['price']}\nNow: {fmt_price(cur, fiat)}"
                    await context.bot.send_message(chat_id=a["chat_id"], text=txt)
                except Exception:
                    pass
                to_remove.append(a)
    if to_remove:
        for a in to_remove:
            try: ALERTS.remove(a)
            except ValueError: pass
        save_json(ALERTS_FILE, ALERTS)

# === Airdrops ===
def ai_summarize(text: str) -> str:
    return ai_chat("Ringkas airdrop ini (maks 3 kalimat, Indonesia, tambahkan langkah join singkat & peringatan scam bila perlu):\n\n" + (text or ""),
                   temp=0.3, max_tokens=220)

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = " ".join(ctx.args) if ctx.args else ""
    items = fetch_airdrops(query=key, limit=6)
    if not items:
        await update.message.reply_text("🎁 Tidak ada airdrop yang cocok saat ini."); return
    parts = []
    for t,u,s in items:
        summary = ai_summarize(s or t)
        parts.append(f"▫️ <b>{t}</b>\n{summary}\n<a href=\"{u}\">{u}</a>")
    await update.message.reply_html("🎁 <b>Airdrop Potensial</b>\n\n" + "\n\n".join(parts), disable_web_page_preview=True)

async def hunt_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /hunt <keyword>"); return
    await airdrops_cmd(update, ctx)

# === Text / Inline ===
PRICE_WORD = re.compile(r"(?i)^(harga|price)\b")
PAIR_PATTERN = re.compile(r"(?i)^(?:harga|price)\s+([a-z0-9$.,]+)(?:[\/\s]+([a-z]{2,6}))?$")
CONVERT_PATTERN = re.compile(r"(?i)^(\d+(?:[.,]\d+)?)\s*([a-z0-9$]{2,12})\s*(?:ke|to)\s*([a-z]{2,6})$")

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not allowed_now(update.effective_user.id): return

    m2 = CONVERT_PATTERN.match(text)
    if m2:
        amt = float(m2.group(1).replace(",", ".")); sym, fiat = m2.group(2), m2.group(3).lower()
        await _reply_convert(update, amt, sym, fiat); return

    m = PAIR_PATTERN.match(text) if PRICE_WORD.match(text) else None
    if m:
        raw, fiat = m.groups(); fiat = (fiat or get_chat_fiat(update.effective_chat.id)).lower()
        syms = [s.strip() for s in raw.replace(" ","").split(",")]
        if len(syms)==1: await _reply_price(update, syms[0], fiat)
        else: await _reply_prices(update, syms, fiat)
        return

    await send_typing(ctx, update.effective_chat.id)
    await update.message.reply_text(ai_chat(text, temp=0.6, max_tokens=280))

async def inline_query(update, ctx):
    q = (update.inline_query.query or "").strip()
    if not q: return
    m = re.match(r"(?i)^([a-z0-9$.,]+)(?:[\/\s]+([a-z]{2,6}))?$", q)
    if not m: return
    sym, fiat = m.groups(); fiat = (fiat or DEFAULT_GLOBAL_FIAT).lower()
    try:
        cid = norm_symbol(sym); data = fetch_price([cid], fiat)
        if cid not in data or fiat not in data[cid]: return
        p = data[cid][fiat]; text = f"💰 {sym.upper()} = {fmt_price(p, fiat)}"
        await update.inline_query.answer([InlineQueryResultArticle(id="1", title=text, input_message_content=InputTextMessageContent(text))],
            cache_time=10, is_personal=True)
    except Exception:
        pass

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Unhandled: %s", context.error)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Isi BOT_TOKEN di .env dahulu.")
    init_openai(OPENAI_API_KEY)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_menu_cb))

    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))

    app.add_handler(CommandHandler("ask", ask))

    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("convert", convert))

    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("dominance", dominance))
    app.add_handler(CommandHandler("fear", fear))
    app.add_handler(CommandHandler("gas", gas))

    app.add_handler(CommandHandler("ohlc", ohlc))
    app.add_handler(CommandHandler("chart", chart))

    app.add_handler(CommandHandler("alerts", alerts_list_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    if app.job_queue:
        app.job_queue.run_repeating(check_alerts, interval=60, first=5)

    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("hunt", hunt_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_error_handler(on_error)

    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

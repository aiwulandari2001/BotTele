#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, logging, datetime as dt
from typing import Optional, List, Dict, Tuple
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv(override=True)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------- KONFIGURASI DASAR ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Jika mau otomatis baca .env, uncomment 2 baris ini:
 

BOT_NAME = "AirdropCore (AI)"
FIAT_DEFAULT = "usd"
DATA_DIR = os.getenv("DATA_DIR", "./data")

COIN_CACHE_FILE = os.path.join(DATA_DIR, "coins.json")
AIRDROP_FILE    = os.path.join(DATA_DIR, "airdrops.json")
AIRLOG_FILE     = os.path.join(DATA_DIR, "airlog.json")

os.makedirs(DATA_DIR, exist_ok=True)

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# ---------- OPENAI (opsional) ----------
client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI aktif")
    else:
        log.info("OpenAI nonaktif (OPENAI_API_KEY kosong)")
except Exception as e:
    log.warning(f"OpenAI init gagal: {e}")
    client = None

# ---------- UTIL ----------
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; AirdropCoreBot/1.0; +https://t.me/)"
}

def now_ts() -> int:
    return int(time.time())

def read_json(path: str, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback

def write_json(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"write_json gagal: {e}")

def pretty_float(n: float) -> str:
    if n >= 1000:
        return f"{n:,.2f}"
    if n >= 1:
        return f"{n:,.4f}"
    return f"{n:.8f}"

# ---------- COINGECKO COINS/LIST CACHE ----------
def load_coins_cache(force: bool = False) -> Dict[str, List[str]]:
    """
    Return mapping symbol -> [ids...] (lowercase), cached 24 jam.
    """
    cache = read_json(COIN_CACHE_FILE, {})
    ttl_ok = isinstance(cache, dict) and (now_ts() - cache.get("_ts", 0) < 24*3600)
    if cache and not force and ttl_ok and "map" in cache:
        return cache["map"]

    try:
        log.info("Refresh coins/list dari CoinGecko…")
        r = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=30)
        r.raise_for_status()
        rows = r.json()
        mp: Dict[str, List[str]] = {}
        for row in rows:
            sym = (row.get("symbol") or "").lower()
            cid = (row.get("id") or "").lower()
            if not sym or not cid:
                continue
            mp.setdefault(sym, []).append(cid)
        write_json(COIN_CACHE_FILE, {"_ts": now_ts(), "map": mp})
        return mp
    except Exception as e:
        log.warning(f"coins/list gagal: {e}")
        return cache.get("map", {}) if isinstance(cache, dict) else {}

COIN_MAP = load_coins_cache(force=False)

def symbol_to_id(sym: str) -> Optional[str]:
    """
    Mapping symbol ke CoinGecko id (pilih kandidat pertama).
    """
    s = (sym or "").lower().strip()
    if not s:
        return None
    ids = COIN_MAP.get(s)
    if ids:
        # Jika ada 'bitcoin' utk 'btc', sering item pertama sudah benar.
        return ids[0]
    # fallback: kalau user langsung kirim id, terima saja
    return s

def fetch_simple_price(ids: List[str], vs: str) -> Dict[str, Dict[str, float]]:
    if not ids:
        return {}
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        r = requests.get(url, params={
            "ids": ",".join(ids),
            "vs_currencies": vs,
            "include_24hr_change": "true"
        }, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"simple/price gagal: {e}")
        return {}

# ---------- PARSER HARGA BEBAS ----------
_re_convert = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-z0-9]+)\s+([a-z0-9]+)\s*$", re.I)
_re_pair    = re.compile(r"^\s*([a-z0-9]+)[/\s]+([a-z0-9]+)\s*$", re.I)
_re_prices  = re.compile(r"^\s*prices?\s+([a-z0-9,]+)\s+([a-z0-9]+)\s*$", re.I)
_re_price   = re.compile(r"^\s*price\s+([a-z0-9]+)\s+([a-z0-9]+)\s*$", re.I)

SUPPORTED_FIAT = {"usd","usdt","idr","eur"}

async def reply_price(update: Update, sym: str, fiat: str, amount: float = 1.0, silent_if_missing: bool = True):
    cid = symbol_to_id(sym)
    if not cid or fiat.lower() not in SUPPORTED_FIAT:
        # hemat VPS: diam jika tidak valid
        if not silent_if_missing:
            await update.message.reply_text("❌ Pair tidak valid.")
        return

    data = fetch_simple_price([cid], fiat.lower())
    if cid not in data or fiat.lower() not in data[cid]:
        if not silent_if_missing:
            await update.message.reply_text("❌ Pair tidak tersedia.")
        return

    px = float(data[cid][fiat.lower()])
    chg = data[cid].get(f"{fiat.lower()}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""

    if amount != 1.0:
        total = px * amount
        await update.message.reply_text(
            f"💱 {amount:g} {sym.upper()} → {pretty_float(total)} {fiat.upper()}{chg_txt}"
        )
    else:
        await update.message.reply_text(
            f"💰 {sym.upper()} = {pretty_float(px)} {fiat.upper()}{chg_txt}"
        )

# ---------- SCRAPER AIRDROP ----------
def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

class Airdrop(dict):
    # dict dengan key: slug,name,url,chain,reward,source
    pass

def scrape_airdrops_io() -> List[Airdrop]:
    url = "https://airdrops.io/latest/"
    out: List[Airdrop] = []
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for item in soup.select(".airdrops-list .item, article, .card"):
        title = item.select_one(".title, h2, h3")
        name = _clean(title.get_text() if title else "")
        if not name:
            continue
        href = item.select_one("a")
        reward = _clean((item.select_one(".reward, .prize, .subtitle") or {}).get_text() if item.select_one(".reward, .prize, .subtitle") else "")
        chain = _clean((item.select_one(".chain, .platform") or {}).get_text() if item.select_one(".chain, .platform") else "")
        link = href["href"] if (href and href.has_attr("href")) else url
        slug = name.lower().replace(" ", "-")
        out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward, url=link, source="airdrops.io"))
    return out

def scrape_airdropalert() -> List[Airdrop]:
    url = "https://airdropalert.com/airdrops"
    out: List[Airdrop] = []
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for card in soup.select("article, .airdrops-list .card, .grid .card"):
        title = card.select_one("h2, h3, .card-title, .title")
        name = _clean(title.get_text() if title else "")
        if not name:
            continue
        href = card.select_one("a")
        link = href["href"] if (href and href.has_attr("href")) else url
        reward = _clean((card.select_one(".reward, .subtitle, .label") or {}).get_text() if card.select_one(".reward, .subtitle, .label") else "")
        chain  = _clean((card.select_one(".chain, .network") or {}).get_text() if card.select_one(".chain, .network") else "")
        slug = name.lower().replace(" ", "-")
        out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward, url=link, source="airdropalert.com"))
    return out

def _scrape_all_sources() -> Tuple[List[Airdrop], Dict[str,int], List[str]]:
    results: List[Airdrop] = []
    per_source: Dict[str,int] = {}
    errors: List[str] = []

    def run(fn, label):
        try:
            rows = fn()
            results.extend(rows)
            per_source[label] = len(rows)
        except Exception as e:
            msg = f"{label}: {type(e).__name__}: {e}"
            log.warning(msg)
            errors.append(msg)

    run(scrape_airdrops_io, "airdrops.io")
    run(scrape_airdropalert, "airdropalert.com")

    # Unikkan by slug (pilih yang punya reward)
    mp: Dict[str, Airdrop] = {}
    for a in results:
        s = str(a.get("slug") or "")
        if not s:
            continue
        if s not in mp or (a.get("reward") and not mp[s].get("reward")):
            mp[s] = a

    uniq = list(mp.values())
    return uniq, per_source, errors

def load_aircache() -> Dict:
    return read_json(AIRDROP_FILE, {"updated_at":0, "items":[], "per_source":{}, "errors":[]})

def save_aircache(data: Dict) -> None:
    write_json(AIRDROP_FILE, data)

def save_airlog(msg: str) -> None:
    logdata = read_json(AIRLOG_FILE, {"logs":[]})
    logdata["logs"].append({"ts":now_ts(), "msg":msg})
    logdata["logs"] = logdata["logs"][-200:]  # keep last 200
    write_json(AIRLOG_FILE, logdata)

def paginate(items: List[Airdrop], page: int, size: int=5, keyword: str="") -> Tuple[str, InlineKeyboardMarkup]:
    total = len(items)
    start = (page-1)*size
    end   = start + size
    page_items = items[start:end]

    if keyword:
        title = f"Airdrop (filter: {keyword}) [{page}/{max(1, (total+size-1)//size)}]\n"
    else:
        title = f"Airdrop [{page}/{max(1, (total+size-1)//size)}]\n"

    lines = []
    for a in page_items:
        nm = a.get("name") or "-"
        src = a.get("source") or "-"
        rw  = a.get("reward") or ""
        ch  = f" ({a['chain']})" if a.get("chain") else ""
        url = a.get("url") or "-"
        lines.append(f"• <b>{nm}</b>{ch}\n  {rw}\n  <i>{src}</i> — {url}")

    text = title + ("\n".join(lines) if lines else "Tidak ada data.")
    # KB
    qk = keyword.replace(" ", "+")
    kb = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"airpg:{qk}:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"airpg:{qk}:{page+1}"))
    if nav:
        kb.append(nav)
    return text, InlineKeyboardMarkup(kb) if kb else None

# ---------- COMMANDS ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu:price"),
         InlineKeyboardButton("💱 Convert", callback_data="menu:conv")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu:air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu:ai")],
    ]
    await update.message.reply_text(
        f"Selamat datang di <b>{BOT_NAME}</b>!\n\n"
        "(AI juga tanpa <code>/ask</code>)\n"
        "• <code>/price</code> <coin> <fiat>\n"
        "• <code>/prices</code> btc,eth idr\n"
        "• <code>/convert</code> 123 sol usd\n"
        "• <code>/setfiat</code> idr|usd|usdt|eur\n"
        "• <code>/airupdate</code> | <code>/airdrops</code> | <code>/tugas</code> &lt;keyword&gt;\n"
        "• <code>/airstatus</code> | <code>/airnews</code> | <code>/airdebug</code> | <code>/airclear</code>\n",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML"
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT sekarang: {FIAT_DEFAULT.upper()}\nFormat: /setfiat idr|usd|usdt|eur"
        ); return
    fiat = ctx.args[0].lower()
    if fiat not in SUPPORTED_FIAT:
        await update.message.reply_text("❌ Fiat tidak valid."); return
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default diset ke {fiat.upper()}")

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 1:
        await update.message.reply_text("Format: /price <coin> [fiat]\nContoh: /price btc usdt"); return
    sym  = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat, amount=1.0, silent_if_missing=True)

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Format: /prices btc,eth idr"); return
    symbols = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = ctx.args[1].lower()
    ids = [symbol_to_id(s) for s in symbols]
    ids = [i for i in ids if i]
    if not ids or fiat not in SUPPORTED_FIAT:
        return  # silent
    data = fetch_simple_price(ids, fiat)
    if not data:
        return
    lines = []
    for s in symbols:
        cid = symbol_to_id(s)
        if not cid or cid not in data or fiat not in data[cid]:
            continue
        px = float(data[cid][fiat])
        chg = data[cid].get(f"{fiat}_24h_change")
        chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
        lines.append(f"{s.upper()}: {pretty_float(px)} {fiat.upper()}{chg_txt}")
    if lines:
        await update.message.reply_text("📊 " + " | ".join(lines))

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <jumlah> <coin> <fiat>"); return
    try:
        amount = float(ctx.args[0])
    except Exception:
        await update.message.reply_text("Jumlah tidak valid."); return
    sym, fiat = ctx.args[1], ctx.args[2].lower()
    await reply_price(update, sym, fiat, amount=amount, silent_if_missing=True)

# --- AIRDROP COMMANDS ---
async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Update airdrops…")
    items, ps, errs = _scrape_all_sources()
    cache = load_aircache()
    cache["updated_at"] = now_ts()
    cache["items"] = items
    cache["per_source"] = ps
    cache["errors"] = errs
    save_aircache(cache)
    per_src_txt = " | ".join([f"{k}: {v}" for k,v in ps.items()]) if ps else "-"
    err_txt = f"\n⚠️ {len(errs)} error" if errs else ""
    await msg.edit_text(
        f"✅ Selesai. Terkumpul {len(items)} airdrop.\nPer sumber: {per_src_txt}{err_txt}"
    )

def _filter_items(keyword: str) -> List[Airdrop]:
    cache = load_aircache()
    items = cache.get("items", [])
    if not keyword:
        return items
    k = keyword.lower().strip()
    filtered = []
    for a in items:
        hay = " ".join([a.get("name",""), a.get("chain",""), a.get("reward",""), a.get("source","")]).lower()
        if k in hay:
            filtered.append(a)
    return filtered

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyword = " ".join(ctx.args).strip()
    items = _filter_items(keyword)
    text, kb = paginate(items, page=1, size=5, keyword=keyword)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def airpg_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data.split(":",2)
    if len(data) != 3: return
    keyword = data[1].replace("+"," ")
    page = int(data[2])
    items = _filter_items(keyword)
    text, kb = paginate(items, page=page, size=5, keyword=keyword)
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        # kalau edit gagal (mis. same content), kirim baru
        await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>"); return
    keyword = " ".join(ctx.args).strip()
    items = _filter_items(keyword)
    if not items:
        await update.message.reply_text("❌ Tidak ditemukan."); return
    # Ambil yang pertama
    url = items[0].get("url")
    tasks = []
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # cari bullet list
        for li in soup.select("li"):
            t = _clean(li.get_text())
            if 5 <= len(t) <= 200 and any(k in t.lower() for k in ["follow","join","invite","task","tweet","discord","telegram","wallet","bridge","testnet","stake","swap"]):
                tasks.append("• " + t)
        tasks = tasks[:20]
    except Exception as e:
        log.warning(f"Parse tugas gagal: {e}")

    if not tasks:
        await update.message.reply_text(f"Detail: {url}\n(daftar tugas tidak terdeteksi otomatis)")
    else:
        await update.message.reply_text(f"✅ Tugas {items[0].get('name')}\n" + "\n".join(tasks))

async def airstatus_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cache = load_aircache()
    ts = cache.get("updated_at", 0)
    when = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
    per = cache.get("per_source", {})
    errs = cache.get("errors", [])
    txt = [
        f"🩺 Status Airdrop",
        f"• Total: {len(cache.get('items', []))}",
        f"• Update: {when}",
        f"• Sumber: " + (", ".join([f"{k}:{v}" for k,v in per.items()]) if per else "-"),
        f"• Error terakhir: {len(errs)}"
    ]
    await update.message.reply_text("\n".join(txt))

async def airnews_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cache = load_aircache()
    it = cache.get("items", [])[:10]
    if not it:
        await update.message.reply_text("Belum ada data. Jalankan /airupdate dulu."); return
    lines = []
    for a in it:
        lines.append(f"• {a.get('name')} — {a.get('url')}")
    await update.message.reply_text("📰 Terbaru:\n" + "\n".join(lines))

async def airdebug_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cache = load_aircache()
    errs = cache.get("errors", [])
    if not errs:
        await update.message.reply_text("Tidak ada error terakhir."); return
    await update.message.reply_text("⚠️ Error scraper:\n" + "\n".join(f"- {e}" for e in errs[:10]))

async def airclear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    write_json(AIRDROP_FILE, {"updated_at":0, "items":[], "per_source":{}, "errors":[]})
    await update.message.reply_text("🧹 Cache airdrop dibersihkan.")

# ---------- MENU CB ----------
async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "menu:price":
        await q.edit_message_text("Contoh:\n• btc usd\n• prices btc,eth idr\n• 0.25 eth idr\n• convert 12 sol usd")
    elif q.data == "menu:conv":
        await q.edit_message_text("Gunakan: /convert <jumlah> <coin> <fiat>")
    elif q.data == "menu:air":
        await q.edit_message_text("• /airupdate untuk refresh\n• /airdrops [keyword]\n• /tugas <keyword> (ambil tugas)")
    else:
        await q.edit_message_text("Tanya AI bebas. Ketik apa saja…")

# ---------- AI & TEXT ROUTER ----------
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not client:
        await update.message.reply_text("AI belum aktif (OPENAI_API_KEY kosong)."); return
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>"); return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            max_tokens=300, temperature=0.5
        )
        ans = resp.choices[0].message.content.strip()
        await update.message.reply_text(ans)
    except Exception as e:
        log.warning(f"AI error: {e}")
        await update.message.reply_text(f"❌ AI error: {e}")

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    # 1) "0.25 eth idr"
    m = _re_convert.match(text)
    if m:
        amount = float(m.group(1))
        sym, fiat = m.group(2), m.group(3).lower()
        await reply_price(update, sym, fiat, amount=amount, silent_if_missing=True)
        return

    # 2) "btc usd" atau "btc/usd"
    m = _re_pair.match(text)
    if m:
        sym, fiat = m.group(1), m.group(2).lower()
        await reply_price(update, sym, fiat, amount=1.0, silent_if_missing=True)
        return

    # 3) "prices btc,eth idr"
    m = _re_prices.match(text)
    if m:
        ctx.args = [m.group(1), m.group(2)]
        await prices_cmd(update, ctx)
        return

    # 4) "price btc usdt"
    m = _re_price.match(text)
    if m:
        ctx.args = [m.group(1), m.group(2)]
        await price_cmd(update, ctx)
        return

    # 5) fallback AI (jika ada)
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content":text}],
                max_tokens=250, temperature=0.6
            )
            ans = resp.choices[0].message.content.strip()
            await update.message.reply_text(ans)
        except Exception as e:
            log.warning(f"AI fallback error: {e}")
    # jika tidak ada AI → diam (hemat)

# ---------- MAIN ----------
def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN kosong. Set env BOT_TOKEN dulu.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))

    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))
    app.add_handler(CommandHandler("airstatus", airstatus_cmd))
    app.add_handler(CommandHandler("airnews", airnews_cmd))
    app.add_handler(CommandHandler("airdebug", airdebug_cmd))
    app.add_handler(CommandHandler("airclear", airclear_cmd))

    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(airpg_cb, pattern=r"^airpg:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    return app

def main():
    app = build_app()
    log.info("Bot polling start")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

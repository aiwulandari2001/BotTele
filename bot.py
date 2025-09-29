# bot.py
# AirdropCore (AI) — Ultra Stable, Smart & Modern Telegram Bot
# © 2025

import os, re, json, time, math, asyncio, logging, html
from typing import List, Dict, Optional, Tuple
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from dotenv import load_dotenv
from openai import OpenAI

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# =========================
# Boot & Config
# =========================
load_dotenv(override=True)

BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
FIAT_DEFAULT    = os.getenv("FIAT_DEFAULT", "usd").lower()

APP_NAME        = "airdropcore.bot"
DATA_DIR        = Path("./data"); DATA_DIR.mkdir(parents=True, exist_ok=True)
AIRDROP_JSON    = DATA_DIR / "airdrops.json"
AIRDROP_LOCK    = DATA_DIR / "airdrop.lock"     # untuk throttle update
AIRDROP_TTL     = 60*10                         # 10 menit anti-spam update

COINGECKO       = "https://api.coingecko.com/api/v3"
UA_HEADERS      = {"User-Agent":"Mozilla/5.0 (AirdropCoreBot)"}

# Logging rapi
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger(APP_NAME)


# =========================
# Utils — Markdown safer + helpers
# =========================
def _md(s: str) -> str:
    """Escape untuk MarkdownV2 Telegram."""
    if not s: return ""
    # escape karakter MarkdownV2
    for ch in r"_*[]()~`>#+-=|{}.!" :
        s = s.replace(ch, "\\"+ch)
    return s

def pretty_num(n: float) -> str:
    try:
        if n >= 1_000_000_000:  return f"{n/1_000_000_000:.2f}B"
        if n >= 1_000_000:      return f"{n/1_000_000:.2f}M"
        if n >= 1_000:          return f"{n/1_000:.2f}K"
        return f"{n:,.4f}"
    except Exception:
        return str(n)

def now_ts() -> int:
    return int(time.time())


# =========================
# AI Client (OpenAI)
# =========================
client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI aktif")
    except Exception as e:
        log.warning("OpenAI init gagal: %s", e)


async def ai_answer(prompt: str) -> str:
    """Jawaban AI gaya informatif, to the point tapi lengkap."""
    if not client:
        return "❌ AI belum diaktifkan (OPENAI_API_KEY kosong)."
    sys = (
        "Kamu adalah asisten Telegram untuk komunitas kripto dan airdrop. "
        "Jawab ringkas, langkah demi langkah jika perlu, beri daftar rapi, "
        "sertakan disclaimer singkat bila spekulatif. Hindari halusinasi link."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":prompt},
            ],
            max_tokens=500, temperature=0.5
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("AI error")
        return f"❌ Gagal panggil AI: {e}"


# =========================
# Crypto — harga, convert
# =========================
# Peta simbol populer → id Coingecko (tambahan gampang)
SYMBOL_MAP: Dict[str,str] = {
    "btc":"bitcoin","eth":"ethereum","usdt":"tether","usdc":"usd-coin",
    "sol":"solana","bnb":"binancecoin","xrp":"ripple","ada":"cardano",
    "doge":"dogecoin","trx":"tron","matic":"polygon","dot":"polkadot",
    "ton":"the-open-network","arb":"arbitrum","op":"optimism",
    "pi":"pi-network"  # jika tidak ada di CG, tangani gracefully
}

FIAT_ALLOWED = {"usd","usdt","idr","eur"}

def map_symbol(sym: str) -> Optional[str]:
    s = sym.lower()
    if s in SYMBOL_MAP: return SYMBOL_MAP[s]
    # fallback as-is (user kirim id coingecko langsung)
    return s if re.fullmatch(r"[a-z0-9\-]{2,}", s) else None

def cg_simple_price(ids: List[str], fiat: str) -> Dict:
    url = f"{COINGECKO}/simple/price"
    try:
        r = requests.get(url, params={
            "ids": ",".join(ids),
            "vs_currencies": fiat,
            "include_24hr_change": "true",
        }, headers=UA_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("cg_simple_price err: %s", e)
        return {}

async def reply_price(update: Update, sym: str, fiat: str):
    cid = map_symbol(sym)
    if (not cid) or (fiat not in FIAT_ALLOWED):
        # hemat VPS: diam saja bila pattern harga tapi data invalid
        return
    data = cg_simple_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        return
    price = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f"(24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_text(
        f"💰 *{_md(sym.upper())}* = *{_md(pretty_num(price))}* *{_md(fiat.upper())}* { _md(chg_txt) }",
        parse_mode=ParseMode.MARKDOWN_V2
    )

# command /price <coin> <fiat>
async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Format: `/price btc idr`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat)

# command /prices btc,eth idr
async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Format: `/prices btc,eth usdt`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    ids: List[str] = []
    for s in syms:
        ms = map_symbol(s)
        if ms: ids.append(ms)
    if not ids or fiat not in FIAT_ALLOWED:
        return
    data = cg_simple_price(ids, fiat)
    lines = []
    for s in syms:
        cid = map_symbol(s)
        if not cid: continue
        if cid in data and fiat in data[cid]:
            p = data[cid][fiat]; chg = data[cid].get(f"{fiat}_24h_change")
            ch = f"(24h {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
            lines.append(f"• *{_md(s.upper())}* = *{_md(pretty_num(p))}* *{_md(fiat.upper())}* { _md(ch) }")
    if lines:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

# command /convert 0.25 btc idr
async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text(
            "Format: `/convert 0.25 btc idr`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    try:
        amt  = float(ctx.args[0])
        sym  = ctx.args[1]
        fiat = ctx.args[2].lower()
    except Exception:
        return
    cid = map_symbol(sym)
    if not cid or fiat not in FIAT_ALLOWED:
        return
    data = cg_simple_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        return
    val = amt * float(data[cid][fiat])
    await update.message.reply_text(
        f"🔁 *{_md(amt)}* *{_md(sym.upper())}* ≈ *{_md(pretty_num(val))}* *{_md(fiat.upper())}*",
        parse_mode=ParseMode.MARKDOWN_V2
    )

# command /setfiat idr|usd|usdt|eur
async def setfiat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: *{_md(FIAT_DEFAULT.upper())}*\n"
            "Format: `/setfiat idr|usd|usdt|eur`",
            parse_mode=ParseMode.MARKDOWN_V2
        ); return
    f = ctx.args[0].lower()
    if f in FIAT_ALLOWED:
        FIAT_DEFAULT = f
        await update.message.reply_text(
            f"✅ FIAT default diset ke *{_md(f.upper())}*",
            parse_mode=ParseMode.MARKDOWN_V2
        )


# =========================
# Airdrop — Scraper (cryptorank.io)
# =========================
class Airdrop(dict):
    # slug, name, reward, chain, url, source, steps(list[str])
    pass

def load_airdrops() -> List[Airdrop]:
    if AIRDROP_JSON.exists():
        try:
            return json.loads(AIRDROP_JSON.read_text("utf-8"))
        except Exception:
            return []
    return []

def save_airdrops(items: List[Airdrop]):
    AIRDROP_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")

def _clean(s: Optional[str]) -> str:
    if not s: return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _mk_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+","-", name.lower()).strip("-")

def scrape_cryptorank(pages:int=1) -> List[Airdrop]:
    base = "https://cryptorank.io/drophunting"
    out: List[Airdrop] = []
    for pg in range(1, pages+1):
        url = base if pg==1 else f"{base}?page={pg}"
        r = requests.get(url, headers=UA_HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # Kartu umum: 'card' / 'list' per situs sering berubah;
        # ambil semua link ke detail drophunting
        cards = soup.select("a[href*='/drophunting/']")
        seen = set()
        for a in cards:
            href = a.get("href") or ""
            if "/drophunting/" not in href: continue
            full = "https://cryptorank.io"+href if href.startswith("/") else href
            title = _clean(a.get_text())
            if not title: continue
            slug  = _mk_slug(title)
            if slug in seen: continue
            seen.add(slug)
            out.append(Airdrop(
                slug=slug, name=title, reward="", chain="",
                url=full, source="cryptorank.io", steps=[]
            ))
    return out

def scrape_detail_steps(url: str) -> Tuple[str, List[str]]:
    """Ambil deskripsi + langkah dari halaman detail (best-effort)."""
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # Deskripsi (paragraf pertama yang panjang)
        desc_el = soup.select_one("p")
        desc = _clean(desc_el.get_text()) if desc_el else ""
        # Langkah: cari <li>
        steps = []
        for li in soup.select("li"):
            tx = _clean(li.get_text())
            if len(tx) >= 3:
                steps.append(tx)
        # Potong maksimal 15 langkah agar ringkas
        steps = steps[:15] if steps else []
        return desc, steps
    except Exception as e:
        log.warning("detail steps fail: %s", e)
        return "", []


def merge_airdrops(new_items: List[Airdrop], old_items: List[Airdrop]) -> List[Airdrop]:
    idx = {a["slug"]: a for a in old_items}
    for a in new_items:
        if a["slug"] not in idx:
            idx[a["slug"]] = a
        else:
            # update ringan
            for k in ("reward","chain","url","source"):
                if a.get(k): idx[a["slug"]][k] = a[k]
    return list(idx.values())


# ===== Commands: /airupdate, /airdrops, /air, /tugas
PAGE_SIZE = 12

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # throttle
    if AIRDROP_LOCK.exists():
        last = int(AIRDROP_LOCK.read_text() or "0")
        if now_ts() - last < AIRDROP_TTL:
            remain = AIRDROP_TTL - (now_ts()-last)
            await update.message.reply_text(f"⏳ Tunggu {remain}s sebelum update lagi.")
            return
    AIRDROP_LOCK.write_text(str(now_ts()))

    pages = 1
    if ctx.args:
        try: pages = max(1, int(ctx.args[0]))
        except: pass

    await update.message.reply_text("🔄 Update airdrops (sumber: Cryptorank)…")

    old = load_airdrops()
    new = []
    ok_by_source: Dict[str,int] = {"cryptorank":0}

    try:
        items = scrape_cryptorank(pages=pages)
        new.extend(items)
        ok_by_source["cryptorank"] = len(items)
    except Exception as e:
        log.warning("scrape cryptorank gagal: %s", e)

    merged = merge_airdrops(new, old)
    save_airdrops(merged)

    total = len(merged)
    msg = ( "✅ Selesai.\n"
            f"• cryptorank: {ok_by_source.get('cryptorank',0)}\n"
            f"Total tersimpan: *{_md(total)}*")
    await update.message.reply_text(_md(msg), parse_mode=ParseMode.MARKDOWN_V2)

def _paged(items: List[Airdrop], page: int) -> Tuple[List[Airdrop], int]:
    total_pages = max(1, math.ceil(len(items)/PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page-1)*PAGE_SIZE
    end   = start + PAGE_SIZE
    return items[start:end], total_pages

def _kb_page(page:int, total:int) -> InlineKeyboardMarkup:
    btns = []
    if total>1:
        prev_p = max(1, page-1); next_p = min(total, page+1)
        btns = [[
            InlineKeyboardButton("⬅️ Prev", callback_data=f"air_prev_{prev_p}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"air_next_{next_p}")
        ]]
    return InlineKeyboardMarkup(btns) if btns else InlineKeyboardMarkup([])

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = load_airdrops()
    page_items, total = _paged(items, 1)
    if not items:
        await update.message.reply_text("Kosong. Jalankan /airupdate dulu.")
        return

    lines = ["🎁 *Airdrop* _(hal 1/{})_".format(total)]
    for a in page_items:
        name = _md(a.get("name",""))
        url  = a.get("url","")
        src  = _md(a.get("source","cryptorank.io"))
        reward = _md(a.get("reward","-"))
        chain  = _md(a.get("chain","-"))
        # link aman (MarkdownV2 url)
        url_md = f"[{_md('selengkapnya')}]({url})" if url else "-"
        lines.append(f"• *{name}* ({_md(src)})\n  Reward: {reward}\n  Chain: {chain}\n  {url_md}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_kb_page(1, total)
    )

async def air_paging_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""
    await q.answer()
    m = re.match(r"air_(prev|next)_(\d+)", data)
    if not m: return
    page = int(m.group(2))
    items = load_airdrops()
    page_items, total = _paged(items, page)

    lines = [f"🎁 *Airdrop* _(hal {page}/{total})_"]
    for a in page_items:
        name = _md(a.get("name",""))
        url  = a.get("url","")
        src  = _md(a.get("source","cryptorank.io"))
        reward = _md(a.get("reward","-"))
        chain  = _md(a.get("chain","-"))
        url_md = f"[{_md('selengkapnya')}]({url})" if url else "-"
        lines.append(f"• *{name}* ({_md(src)})\n  Reward: {reward}\n  Chain: {chain}\n  {url_md}")

    await q.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_kb_page(page, total)
    )

def _find_air(items: List[Airdrop], kw:str) -> Optional[Airdrop]:
    s = kw.lower()
    for a in items:
        if s in a.get("slug","") or s in a.get("name","").lower():
            return a
    return None

# /air <keyword> → deskripsi + link
async def air_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: `/air <keyword>`", parse_mode=ParseMode.MARKDOWN_V2); return
    kw = " ".join(ctx.args)
    items = load_airdrops()
    a = _find_air(items, kw)
    if not a:
        await update.message.reply_text("❌ Tidak ditemukan."); return
    desc, steps = scrape_detail_steps(a.get("url",""))
    name = _md(a.get("name",""))
    url  = a.get("url","")
    url_md = f"[{_md('tautan')}]({url})" if url else "-"
    body = f"*{name}* — *Detail*\n{_md(desc) or '-'}\n{url_md}"
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN_V2)

# /tugas <keyword> → daftar langkah + link
async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: `/tugas <keyword>`", parse_mode=ParseMode.MARKDOWN_V2); return
    kw = " ".join(ctx.args)
    items = load_airdrops()
    a = _find_air(items, kw)
    if not a:
        await update.message.reply_text("❌ Tidak ditemukan."); return
    desc, steps = scrape_detail_steps(a.get("url",""))
    name = _md(a.get("name",""))
    url  = a.get("url","")
    url_md = f"[{_md('tautan')}]({url})" if url else "-"

    if steps:
        st = "\n".join([f"{i+1}. {_md(s)}" for i,s in enumerate(steps)])
    else:
        st = "-"

    txt = f"*{name}* — *Tugas*\n{st}\n{url_md}"
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


# =========================
# Menu & Help
# =========================
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
        InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
    ],[
        InlineKeyboardButton("🤖 AI", callback_data="menu_ai"),
    ]])
    txt = (
        "🧩 *AirdropCore (AI)*\n"
        "• /airupdate _(update daftar)_\n"
        "• /airdrops _(daftar & paging)_\n"
        "• /air <keyword> _(detail)_\n"
        "• /tugas <keyword> _(steps + link)_\n\n"
        "💱 *Harga & Market*\n"
        "• /price btc idr\n"
        "• /prices btc,eth usdt\n"
        "• /convert 0.25 sol usd\n"
        "• /setfiat idr|usd|usdt|eur\n\n"
        "AI bisa chat bebas (tanpa /ask)."
    )
    await update.message.reply_text(_md(txt), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, ctx)

async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = "Contoh:\n• /price btc usdt\n• /prices btc,eth idr\n• /convert 0.1 eth idr"
    elif data == "menu_air":
        txt = "Airdrop:\n• /airupdate\n• /airdrops\n• /air <kw>\n• /tugas <kw>"
    else:
        txt = "Ketik bebas untuk bertanya apa saja (tanpa /ask)."
    await q.edit_message_text(_md(txt), parse_mode=ParseMode.MARKDOWN_V2)


# =========================
# Router Pesan Bebas
# =========================
PAIR_WORD = re.compile(r"^\s*([0-9]*\.?[0-9]+)?\s*([a-zA-Z0-9\-]{2,})\s+([a-zA-Z]{3,4})\s*$")
SIMPLE_PAIR = re.compile(r"^\s*([a-zA-Z0-9\-]{2,})\s+([a-zA-Z]{3,4})\s*$")

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) convert bebas: "0.25 btc idr"
    m = PAIR_WORD.match(text)
    if m and m.group(1):
        try:
            amt  = float(m.group(1))
            sym  = m.group(2)
            fiat = m.group(3).lower()
            cid  = map_symbol(sym)
            if cid and fiat in FIAT_ALLOWED:
                data = cg_simple_price([cid], fiat)
                if cid in data and fiat in data[cid]:
                    val = amt * float(data[cid][fiat])
                    await update.message.reply_text(
                        f"🔁 *{_md(amt)}* *{_md(sym.upper())}* ≈ *{_md(pretty_num(val))}* *{_md(fiat.upper())}*",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    return
        except: pass
        return  # hemat VPS bila gagal

    # 2) harga bebas: "btc idr"
    m2 = SIMPLE_PAIR.match(text)
    if m2:
        await reply_price(update, m2.group(1), m2.group(2).lower())
        return

    # 3) selain itu → AI
    await update.chat.send_action(ChatAction.TYPING)
    ans = await ai_answer(text)
    await update.message.reply_text(ans)


# =========================
# App Builder & Main
# =========================
def build_app() -> Application:
    # Perbaiki kasus VPS python 3.8: tidak ada loop aktif
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Harga
    app.add_handler(CommandHandler("setfiat", setfiat_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))

    # Airdrop
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("air", air_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))
    app.add_handler(CallbackQueryHandler(air_paging_cb, pattern=r"^air_(prev|next)_"))

    # Menu
    app.add_handler(CallbackQueryHandler(on_menu_cb, pattern=r"^menu_"))

    # Router AI & harga bebas
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    return app

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN kosong. Isi di .env")
    app = build_app()
    log.info("Bot polling start…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

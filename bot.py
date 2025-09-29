# bot.py — AirdropCore Ultra Pro (final)
# Stable • Modern • AI Pintar • Crypto • Airdrop (cryptorank)
# 2025

import os, re, json, time, math, asyncio, logging
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

APP_NAME        = "airdropcore.bot"
BOT_TOKEN       = (os.getenv("BOT_TOKEN") or "").strip()
OPENAI_API_KEY  = (os.getenv("OPENAI_API_KEY") or "").strip()
FIAT_DEFAULT    = (os.getenv("FIAT_DEFAULT") or "usd").lower()

DATA_DIR        = Path("./data"); DATA_DIR.mkdir(parents=True, exist_ok=True)
AIRDROP_JSON    = DATA_DIR / "airdrops.json"
AIRDROP_LOCK    = DATA_DIR / "airdrop.lock"
AIRDROP_TTL     = 60 * 10   # 10 menit throttle

COINGECKO       = "https://api.coingecko.com/api/v3"
UA_HEADERS      = {"User-Agent": "Mozilla/5.0 (AirdropCoreBot/1.0)"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger(APP_NAME)

# =========================
# Helpers — Markdown & time
# =========================
def _md(s) -> str:
    """Escape aman MarkdownV2 (termasuk '=' & '(')."""
    s = str(s) if s is not None else ""
    for ch in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s

def now_ts() -> int:
    return int(time.time())

def pretty_num(n) -> str:
    try:
        n = float(n)
        if n >= 1_000_000_000:  return f"{n/1_000_000_000:.2f}B"
        if n >= 1_000_000:      return f"{n/1_000_000:.2f}M"
        if n >= 1_000:          return f"{n/1_000:.2f}K"
        if n >= 1:              return f"{n:,.2f}"
        return f"{n:.6f}"
    except Exception:
        return str(n)

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
    """Jawaban AI panjang, cerdas, profesional; aman dikirim tanpa parse_mode."""
    if not client:
        return "❌ AI belum aktif. Isi OPENAI_API_KEY di .env"
    sys = (
        "Kamu adalah asisten AI profesional untuk komunitas kripto & airdrop. "
        "Jawablah secara komprehensif, terstruktur (judul/point/step bila perlu), "
        "jelas, dan hindari halusinasi tautan. Jika spekulatif, beri disclaimer singkat."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content": sys},
                      {"role":"user","content": prompt}],
            max_tokens=700, temperature=0.65
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("AI error")
        return f"❌ Error AI: {e}"

# =========================
# Crypto — harga/convert/top/dom/fear
# =========================
SYMBOL_MAP: Dict[str,str] = {
    "btc":"bitcoin","eth":"ethereum","usdt":"tether","usdc":"usd-coin",
    "sol":"solana","bnb":"binancecoin","xrp":"ripple","ada":"cardano",
    "doge":"dogecoin","trx":"tron","matic":"polygon","dot":"polkadot",
    "ton":"the-open-network","arb":"arbitrum","op":"optimism",
    "pi":"pi-network"
}
FIAT_ALLOWED = {"usd","usdt","idr","eur"}

def map_symbol(sym: str) -> Optional[str]:
    s = (sym or "").lower().strip()
    if s in SYMBOL_MAP: return SYMBOL_MAP[s]
    if re.fullmatch(r"[a-z0-9\-]{2,}", s):
        return s
    return None

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
        return  # hemat VPS: diam kalau invalid
    data = cg_simple_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        return
    p = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f"(24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    txt = f"💰 *{_md(sym.upper())}* = *{_md(pretty_num(p))}* *{_md(fiat.upper())}* {_md(chg_txt)}"
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: `/price btc idr`", parse_mode=ParseMode.MARKDOWN_V2); return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat)

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: `/prices btc,eth usdt`", parse_mode=ParseMode.MARKDOWN_V2); return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    ids = [map_symbol(s) for s in syms]
    ids = [i for i in ids if i]
    if not ids or fiat not in FIAT_ALLOWED: return
    data = cg_simple_price(ids, fiat)
    lines = []
    for s in syms:
        cid = map_symbol(s)
        if not cid: continue
        if cid in data and fiat in data[cid]:
            p = data[cid][fiat]
            chg = data[cid].get(f"{fiat}_24h_change")
            ch = f"(24h {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
            lines.append(f"• *{_md(s.upper())}* = *{_md(pretty_num(p))}* *{_md(fiat.upper())}* {_md(ch)}")
    if lines:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: `/convert 0.25 btc idr`", parse_mode=ParseMode.MARKDOWN_V2); return
    try:
        amt = float(ctx.args[0]); sym = ctx.args[1]; fiat = ctx.args[2].lower()
    except Exception:
        return
    cid = map_symbol(sym)
    if not cid or fiat not in FIAT_ALLOWED: return
    data = cg_simple_price([cid], fiat)
    if cid not in data or fiat not in data[cid]: return
    val = amt * float(data[cid][fiat])
    await update.message.reply_text(
        f"🔁 *{_md(amt)}* *{_md(sym.upper())}* ≈ *{_md(pretty_num(val))}* *{_md(fiat.upper())}*",
        parse_mode=ParseMode.MARKDOWN_V2
    )

# Top market (by market cap) dari CoinGecko
async def top_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = 10
    if ctx.args and ctx.args[0].isdigit():
        n = max(1, min(25, int(ctx.args[0])))
    try:
        r = requests.get(f"{COINGECKO}/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": n, "page": 1, "sparkline": "false"
        }, headers=UA_HEADERS, timeout=20)
        r.raise_for_status()
        rows = r.json()
        lines = [f"📊 *Top {n} by MC*"]
        for i, row in enumerate(rows, 1):
            sym = row.get("symbol","").upper()
            name = row.get("name","")
            price = row.get("current_price")
            chg = row.get("price_change_percentage_24h")
            lines.append(f"{i}. *{_md(sym)}* — {_md(name)}  ${_md(pretty_num(price))}  {_md(f'{chg:+.2f}%') if chg is not None else ''}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil top: {e}")

# Dominance BTC/ETH (CoinGecko global)
async def dominance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get(f"{COINGECKO}/global", headers=UA_HEADERS, timeout=20)
        r.raise_for_status()
        g = r.json().get("data", {})
        dom = g.get("market_cap_percentage", {})
        btc = dom.get("btc"); eth = dom.get("eth")
        lines = ["🧭 *Dominance*", f"• BTC: *{_md(f'{btc:.2f}%') if btc is not None else '-'}*", f"• ETH: *{_md(f'{eth:.2f}%') if eth is not None else '-'}*"]
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil dominance: {e}")

# Fear & Greed Index
async def fear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=20)
        r.raise_for_status()
        dt = r.json().get("data", [])
        if not dt: 
            await update.message.reply_text("❌ Tidak ada data F&G"); return
        d0 = dt[0]
        value = d0.get("value"); cls = d0.get("value_classification","")
        txt = f"😨 *Fear & Greed*\n• Index: *{_md(value)}*  ({_md(cls)})"
        await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil Fear&Greed: {e}")

# =========================
# Airdrop — cryptorank only
# =========================
def load_airdrops() -> List[Dict]:
    if AIRDROP_JSON.exists():
        try:
            data = json.loads(AIRDROP_JSON.read_text("utf-8"))
            if isinstance(data, list):  # versi lama
                return data
            return data.get("items", [])
        except Exception:
            return []
    return []

def save_airdrops(items: List[Dict]):
    data = {"items": items, "updated_at": now_ts()}
    AIRDROP_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

def _clean(s: Optional[str]) -> str:
    if not s: return ""
    return re.sub(r"\s+", " ", s).strip()

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def scrape_cryptorank(pages:int=1) -> List[Dict]:
    base = "https://cryptorank.io/drophunting"
    out: List[Dict] = []
    for pg in range(1, pages+1):
        url = base if pg==1 else f"{base}?page={pg}"
        r = requests.get(url, headers=UA_HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.select("a[href*='/drophunting/']")
        seen = set()
        for a in anchors:
            href = a.get("href") or ""
            if "/drophunting/" not in href: continue
            full = "https://cryptorank.io"+href if href.startswith("/") else href
            title = _clean(a.get_text())
            if not title: continue
            slug = _slug(title)
            if slug in seen: continue
            seen.add(slug)
            out.append({
                "slug": slug, "name": title, "reward": "", "chain": "",
                "url": full, "source": "cryptorank.io", "steps":[]
            })
    return out

def scrape_detail_steps(url: str) -> Tuple[str, List[str]]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        desc = _clean((soup.select_one("p") or {}).get_text() if soup.select_one("p") else "")
        steps = []
        for li in soup.select("li"):
            tx = _clean(li.get_text())
            if len(tx) >= 3:
                steps.append(tx)
        return desc, steps[:15] if steps else []
    except Exception as e:
        log.warning("detail fail: %s", e)
        return "", []

def merge_airdrops(new_items: List[Dict], old_items: List[Dict]) -> List[Dict]:
    idx = {a["slug"]: a for a in old_items if "slug" in a}
    for a in new_items:
        slug = a.get("slug")
        if not slug: continue
        if slug not in idx:
            idx[slug] = a
        else:
            for k in ("reward","chain","url","source"):
                if a.get(k): idx[slug][k] = a[k]
    return list(idx.values())

PAGE_SIZE = 12

def _paged(items: List[Dict], page:int) -> Tuple[List[Dict], int]:
    total = max(1, math.ceil(len(items)/PAGE_SIZE))
    page = max(1, min(page, total))
    start = (page-1)*PAGE_SIZE
    return items[start:start+PAGE_SIZE], total

def _kb_page(page:int, total:int) -> InlineKeyboardMarkup:
    btns = []
    if total>1:
        prev_p = max(1, page-1); next_p = min(total, page+1)
        btns = [[
            InlineKeyboardButton("⬅️ Prev", callback_data=f"air_prev_{prev_p}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"air_next_{next_p}")
        ]]
    return InlineKeyboardMarkup(btns) if btns else InlineKeyboardMarkup([])

async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # throttle
    if AIRDROP_LOCK.exists():
        last = int((AIRDROP_LOCK.read_text() or "0").strip() or 0)
        if now_ts() - last < AIRDROP_TTL:
            left = AIRDROP_TTL - (now_ts() - last)
            await update.message.reply_text(f"⏳ Tunggu {_md(left)} detik untuk update lagi.", parse_mode=ParseMode.MARKDOWN_V2)
            return
    AIRDROP_LOCK.write_text(str(now_ts()))

    pages = 1
    if ctx.args and ctx.args[0].isdigit():
        pages = max(1, min(5, int(ctx.args[0])))

    await update.message.reply_text("🔄 Update airdrops dari *cryptorank*…", parse_mode=ParseMode.MARKDOWN_V2)

    old = load_airdrops()
    try:
        fresh = scrape_cryptorank(pages=pages)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal scrape: {e}")
        return
    merged = merge_airdrops(fresh, old)
    save_airdrops(merged)

    msg = f"✅ Selesai. cryptorank: +{_md(len(fresh))}\nTotal tersimpan: *_"+_md(len(merged))+"_*"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = load_airdrops()
    if not items:
        await update.message.reply_text("Kosong. Jalankan /airupdate dulu.")
        return
    page = 1
    if ctx.args and ctx.args[0].isdigit():
        page = int(ctx.args[0])
    chunk, total = _paged(items, page)

    lines = [f"🎁 *Airdrop* _(hal {page}/{total})_"]
    for it in chunk:
        name = _md(it.get("name",""))
        reward = _md(it.get("reward","-"))
        chain = _md(it.get("chain","-"))
        url = it.get("url","")
        src = _md(it.get("source","cryptorank.io"))
        url_md = f"[{_md('selengkapnya')}]({url})" if url else "-"
        lines.append(f"• *{name}* ({src})\n  Reward: {reward}\n  Chain: {chain}\n  {url_md}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_kb_page(page, total))

async def air_paging_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    m = re.match(r"air_(prev|next)_(\d+)", data)
    if not m: return
    page = int(m.group(2))
    items = load_airdrops()
    chunk, total = _paged(items, page)

    lines = [f"🎁 *Airdrop* _(hal {page}/{total})_"]
    for it in chunk:
        name = _md(it.get("name",""))
        reward = _md(it.get("reward","-"))
        chain = _md(it.get("chain","-"))
        url = it.get("url","")
        src = _md(it.get("source","cryptorank.io"))
        url_md = f"[{_md('selengkapnya')}]({url})" if url else "-"
        lines.append(f"• *{name}* ({src})\n  Reward: {reward}\n  Chain: {chain}\n  {url_md}")

    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_kb_page(page, total))

def _find_air(items: List[Dict], kw: str) -> Optional[Dict]:
    s = (kw or "").lower()
    for it in items:
        if s in (it.get("slug","")) or s in (it.get("name","").lower()):
            return it
    return None

async def air_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: `/air <keyword>`", parse_mode=ParseMode.MARKDOWN_V2); return
    kw = " ".join(ctx.args)
    items = load_airdrops()
    it = _find_air(items, kw)
    if not it:
        await update.message.reply_text("❌ Tidak ditemukan."); return
    desc, steps = scrape_detail_steps(it.get("url",""))
    name = _md(it.get("name",""))
    url = it.get("url","")
    url_md = f"[{_md('tautan')}]({url})" if url else "-"
    body = f"*{name}* — *Detail*\n{_md(desc) or '-'}\n{url_md}"
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN_V2)

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: `/tugas <keyword>`", parse_mode=ParseMode.MARKDOWN_V2); return
    kw = " ".join(ctx.args)
    items = load_airdrops()
    it = _find_air(items, kw)
    if not it:
        await update.message.reply_text("❌ Tidak ditemukan."); return
    desc, steps = scrape_detail_steps(it.get("url",""))
    name = _md(it.get("name",""))
    url = it.get("url","")
    url_md = f"[{_md('tautan')}]({url})" if url else "-"
    if steps:
        st = "\n".join([f"{i+1}. {_md(s)}" for i,s in enumerate(steps)])
    else:
        st = "-"
    txt = f"*{name}* — *Tugas*\n{st}\n{url_md}"
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)

# =========================
# Menu, Help, Ask, Router
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
        "• /air <kw> _(detail)_\n"
        "• /tugas <kw> _(steps + link)_\n\n"
        "💱 *Harga & Market*\n"
        "• /price btc idr\n"
        "• /prices btc,eth usdt\n"
        "• /convert 0.25 sol usd\n"
        "• /top 10, /dominance, /fear\n\n"
        "AI bisa chat bebas (tanpa /ask)."
    )
    await update.message.reply_text(_md(txt), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, ctx)

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("Format: `/ask <pertanyaan>`", parse_mode=ParseMode.MARKDOWN_V2); return
    await update.effective_chat.send_action(ChatAction.TYPING)
    ans = await ai_answer(q)
    # kirim tanpa parse_mode: aman untuk teks AI panjang
    await update.message.reply_text(ans)

async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = "Contoh:\n• /price btc usdt\n• /prices btc,eth idr\n• /convert 0.1 eth idr\n• /top 10\n• /dominance\n• /fear"
    elif data == "menu_air":
        txt = "Airdrop:\n• /airupdate\n• /airdrops\n• /air <kw>\n• /tugas <kw>"
    else:
        txt = "Ketik bebas untuk bertanya apa saja (tanpa /ask)."
    await q.edit_message_text(_md(txt), parse_mode=ParseMode.MARKDOWN_V2)

PAIR_WORD   = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s+([a-zA-Z0-9\-]{2,})\s+([a-zA-Z]{3,4})\s*$")
SIMPLE_PAIR = re.compile(r"^\s*([a-zA-Z0-9\-]{2,})\s+([a-zA-Z]{3,4})\s*$")

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # convert bebas: "0.25 btc idr"
    m = PAIR_WORD.match(text)
    if m:
        try:
            amt  = float(m.group(1)); sym = m.group(2); fiat = m.group(3).lower()
            cid  = map_symbol(sym)
            if cid and fiat in FIAT_ALLOWED:
                data = cg_simple_price([cid], fiat)
                if cid in data and fiat in data[cid]:
                    val = amt * float(data[cid][fiat])
                    await update.message.reply_text(
                        f"🔁 *{_md(amt)}* *{_md(sym.upper())}* ≈ *{_md(pretty_num(val))}* *{_md(fiat.upper())}*",
                        parse_mode=ParseMode.MARKDOWN_V2
                    ); return
        except: pass
        return

    # harga bebas: "btc idr"
    m2 = SIMPLE_PAIR.match(text)
    if m2:
        await reply_price(update, m2.group(1), m2.group(2).lower()); return

    # lainnya → AI
    await update.effective_chat.send_action(ChatAction.TYPING)
    ans = await ai_answer(text)
    await update.message.reply_text(ans)

# =========================
# Error handler
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("❌ Terjadi error. Sudah saya catat, coba lagi.")
    except Exception:
        pass

# =========================
# App Builder & Main
# =========================
def build_app() -> Application:
    # Fix VPS python 3.8 (event loop)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))

    # Crypto
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("dominance", dominance_cmd))
    app.add_handler(CommandHandler("fear", fear_cmd))

    # Airdrop (cryptorank)
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("air", air_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))
    app.add_handler(CallbackQueryHandler(air_paging_cb, pattern=r"^air_(prev|next)_"))
    app.add_handler(CallbackQueryHandler(on_menu_cb, pattern=r"^menu_"))

    # Router AI/harga bebas
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Error handler
    app.add_error_handler(on_error)
    return app

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN kosong. Isi di .env")
    app = build_app()
    log.info("Bot polling start…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

# bot.py
import os, re, json, time, html, uuid, logging, asyncio
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict

from dotenv import load_dotenv
load_dotenv(override=True)

import httpx
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent,
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, InlineQueryHandler, filters
)

# ========== CONFIG ==========
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
FIAT_DEFAULT    = os.getenv("FIAT_DEFAULT", "usd").lower()

DATA_DIR        = os.getenv("DATA_DIR", ".")
COINMAP_JSON    = os.path.join(DATA_DIR, "coinmap.json")
AIRDROP_JSON    = os.path.join(DATA_DIR, "airdrops.json")
WATCH_JSON      = os.path.join(DATA_DIR, "watch.json")
AUTO_JSON       = os.path.join(DATA_DIR, "auto.json")

USER_AGENT = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AirdropCoreBot/2025 (+https://t.me/)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("airdropcore.bot")

# OpenAI (optional)
client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI aktif")
except Exception as e:
    log.warning("OpenAI init gagal: %s", e)

# ========== UTIL JSON ==========
def _read_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _write_json(path: str, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ========== COIN MAPPING (symbol -> coingecko id) ==========
COIN_MAP: Dict[str, str] = _read_json(COINMAP_JSON, {})  # "btc" -> "bitcoin"
COIN_LAST_SYNC = 0

async def sync_coin_map():
    global COIN_MAP, COIN_LAST_SYNC
    # refresh tiap 24 jam
    if time.time() - COIN_LAST_SYNC < 24*3600 and COIN_MAP:
        return
    url = "https://api.coingecko.com/api/v3/coins/list"
    try:
        async with httpx.AsyncClient(timeout=30, headers=USER_AGENT) as hx:
            r = await hx.get(url)
            r.raise_for_status()
            mp = {}
            for it in r.json():
                sym = (it.get("symbol") or "").lower()
                cid = (it.get("id") or "").lower()
                if sym and cid and sym not in mp:
                    mp[sym] = cid
            if mp:
                COIN_MAP = mp
                _write_json(COINMAP_JSON, COIN_MAP)
                COIN_LAST_SYNC = time.time()
                log.info("Coin map synced: %d symbols", len(COIN_MAP))
    except Exception as e:
        log.warning("Sync coin map gagal: %s", e)

def norm_symbol(sym: str) -> Optional[str]:
    s = (sym or "").lower().strip()
    # beberapa alias populer
    aliases = {"doge":"dogecoin","matic":"matic-network","bnb":"binancecoin","xrp":"ripple"}
    if s in aliases: return aliases[s]
    return COIN_MAP.get(s)

# ========== HARGA ==========
FIAT_SET = {"usd","usdt","idr","eur","gbp","jpy","inr"}

async def cg_simple_price(ids: List[str], fiat: str) -> Dict:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": ",".join(ids),
        "vs_currencies": fiat,
        "include_24hr_change": "true"
    }
    async with httpx.AsyncClient(timeout=30, headers=USER_AGENT) as hx:
        r = await hx.get(url, params=params)
        r.raise_for_status()
        return r.json()

def fmt_price(x: float, fiat: str) -> str:
    if fiat in {"usd","usdt","eur","gbp"}:
        return f"${x:,.4f}" if fiat in {"usd","usdt"} else f"{x:,.4f} {fiat.upper()}"
    if fiat=="idr":
        return f"Rp {x:,.0f}"
    return f"{x:,.4f} {fiat.upper()}"

def valid_pair(sym: str, fiat: str) -> bool:
    if fiat.lower() not in FIAT_SET: return False
    return norm_symbol(sym) is not None

# ========== AIRDROP SCRAPER ==========
@dataclass
class Airdrop:
    slug: str
    name: str
    reward: str = "-"
    chain: str = "-"
    url: str = ""
    source: str = ""

def _clean(s: Optional[str]) -> str:
    if not s: return "-"
    return re.sub(r"\s+", " ", s).strip()

def save_airdrops(items: List[Airdrop]):
    _write_json(AIRDROP_JSON, [asdict(a) for a in items])

def load_airdrops() -> List[Airdrop]:
    return [Airdrop(**x) for x in _read_json(AIRDROP_JSON, [])]

async def scrape_irdrops_io() -> List[Airdrop]:
    url = "https://airdrops.io/latest/"
    out: List[Airdrop] = []
    try:
        async with httpx.AsyncClient(timeout=30, headers=USER_AGENT) as hx:
            r = await hx.get(url)
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".airdrops-list .item, article"):
            t = card.select_one(".title, h3, h2")
            name = _clean(t.get_text() if t else None)
            if name == "-": continue
            href = card.select_one("a")
            reward = _clean((card.select_one(".reward, .subtitle") or {}).get_text() if card.select_one(".reward, .subtitle") else None)
            chain = _clean((card.select_one(".chain, .platform") or {}).get_text() if card.select_one(".chain, .platform") else None)
            link = href["href"] if href and href.has_attr("href") else url
            slug = re.sub(r"[^a-z0-9]+","-",name.lower()).strip("-")
            out.append(Airdrop(slug, name, reward, chain, link, "airdrops.io"))
    except Exception as e:
        log.warning("scrape airdrops.io gagal: %s", e)
    return out

async def scrape_airdropalert() -> List[Airdrop]:
    url = "https://airdropalert.com/latest-airdrops"
    out: List[Airdrop] = []
    try:
        async with httpx.AsyncClient(timeout=30, headers=USER_AGENT) as hx:
            r = await hx.get(url)
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("article, .airdrop-card, .list-item"):
            t = row.select_one("h2, h3, .title")
            name = _clean(t.get_text() if t else None)
            if name == "-": continue
            href = row.select_one("a")
            reward = _clean((row.select_one(".reward, .prize, .badge") or {}).get_text() if row.select_one(".reward, .prize, .badge") else None)
            chain = _clean((row.select_one(".chain, .network") or {}).get_text() if row.select_one(".chain, .network") else None)
            link = href["href"] if href and href.has_attr("href") else url
            slug = re.sub(r"[^a-z0-9]+","-",name.lower()).strip("-")
            out.append(Airdrop(slug, name, reward, chain, link, "airdropalert"))
    except Exception as e:
        log.warning("scrape airdropalert gagal: %s", e)
    return out

async def scrape_airdrops_fun() -> List[Airdrop]:
    url = "https://airdrops.fun/"
    out: List[Airdrop] = []
    try:
        async with httpx.AsyncClient(timeout=30, headers=USER_AGENT) as hx:
            r = await hx.get(url)
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("article, .post-card"):
            t = row.select_one("h2, h3, .entry-title")
            name = _clean(t.get_text() if t else None)
            if name == "-": continue
            href = row.select_one("a")
            link = href["href"] if href and href.has_attr("href") else url
            slug = re.sub(r"[^a-z0-9]+","-",name.lower()).strip("-")
            out.append(Airdrop(slug, name, "-", "-", link, "airdrops.fun"))
    except Exception as e:
        log.warning("scrape airdrops.fun gagal: %s", e)
    return out

async def scrape_all() -> List[Airdrop]:
    tasks = [scrape_irdrops_io(), scrape_airdropalert(), scrape_airdrops_fun()]
    results: List[Airdrop] = []
    for coro in asyncio.as_completed(tasks):
        try:
            results += await coro
        except Exception as e:
            log.warning("scrape task fail: %s", e)
    # unik berdasar slug; utamakan yang punya reward
    mp: Dict[str, Airdrop] = {}
    for a in results:
        cur = mp.get(a.slug)
        if (not cur) or (a.reward != "-" and cur.reward == "-"):
            mp[a.slug] = a
    return list(mp.values())

def extract_tasks(url: str) -> List[str]:
    # heuristik generik ambil <li> berisi kata follow/join/retweet/bridge dll
    try:
        r = httpx.get(url, headers=USER_AGENT, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        cand = []
        for li in soup.select("li"):
            tx = re.sub(r"\s+", " ", li.get_text(" ").strip())
            if len(tx) < 4: continue
            if re.search(r"(follow|join|retweet|like|bridge|swap|task|quest|quest|points|wallet|stake|discord|telegram|twitter|x\.com)", tx, re.I):
                cand.append(tx)
        # de-dup
        seen=set(); out=[]
        for s in cand:
            k=s.lower()
            if k in seen: continue
            seen.add(k); out.append(s)
        return out[:12]
    except Exception as e:
        log.warning("extract tasks fail: %s", e)
        return []

# ========== TEXT PARSERS ==========
PAIR_ONLY    = re.compile(r"^\s*([a-zA-Z0-9]{2,10})\s+([a-zA-Z]{2,5})\s*$")
AMOUNT_PAIR  = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s+([a-zA-Z0-9]{2,10})\s+([a-zA-Z]{2,5})\s*$")

def price_card(sym: str, fiat: str, price: float, chg: Optional[float]) -> str:
    chg_txt = "" if chg is None else f" (24h: {chg:+.2f}%)"
    return f"💰 <b>{html.escape(sym.upper())}</b> = <b>{fmt_price(price, fiat)}</b>{chg_txt}"

async def cmd_price(sym: str, fiat: str) -> Optional[str]:
    if not valid_pair(sym, fiat): return None
    cid = norm_symbol(sym)
    js = await cg_simple_price([cid], fiat)
    if cid not in js or fiat not in js[cid]: return None
    price = js[cid][fiat]; chg = js[cid].get(f"{fiat}_24h_change")
    return price_card(sym, fiat, price, chg if isinstance(chg,(int,float)) else None)

def cmd_convert_text(amount: float, sym: str, fiat: str) -> Optional[str]:
    # placeholder; fetch in async wrapper below
    return None

async def cmd_convert(amount: float, sym: str, fiat: str) -> Optional[str]:
    if not valid_pair(sym, fiat): return None
    cid = norm_symbol(sym)
    js = await cg_simple_price([cid], fiat)
    if cid not in js or fiat not in js[cid]: return None
    price = js[cid][fiat]
    total = price * amount
    return f"🔁 <b>{amount:g} {html.escape(sym.upper())}</b> ≈ <b>{fmt_price(total, fiat)}</b>  (1 {sym.upper()} = {fmt_price(price, fiat)})"

# ========== COMMANDS ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="go:price"),
         InlineKeyboardButton("🔁 Convert", callback_data="go:convert")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="go:air"),
         InlineKeyboardButton("🤖 AI", callback_data="go:ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di <b>Airdrop Core (AI)</b>\n"
        "(AI bisa juga tanpa /ask)\n\n"
        "• <code>/price &lt;coin&gt; &lt;fiat&gt;</code>\n"
        "• <code>/prices btc,eth idr</code>\n"
        "• <code>/convert 0.25 sol usd</code>\n"
        "• <code>/setfiat idr|usd|usdt|eur</code>\n"
        "• <code>/airupdate</code>, <code>/airdrops</code>, <code>/air &lt;keyword&gt;</code>, <code>/tugas &lt;keyword&gt;</code>\n"
        "• <code>/watch btc usdt above 105000</code>, <code>/watchlist</code>, <code>/unwatch</code>\n"
        "• <code>/airauto on|off|menit</code>\n",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True
    )

async def menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="go:price"),
         InlineKeyboardButton("🔁 Convert", callback_data="go:convert")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="go:air"),
         InlineKeyboardButton("🤖 AI", callback_data="go:ai")],
    ]
    await update.message.reply_text("Pilih menu:", reply_markup=InlineKeyboardMarkup(kb))

async def go_nav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""
    await q.answer()
    if data=="go:price":
        txt="Contoh:\n• /price btc usdt\n• btc usd\n• 0.25 eth idr"
    elif data=="go:convert":
        txt="Contoh:\n• /convert 0.5 sol usd\n• 12 arb idr"
    elif data=="go:air":
        txt="• /airupdate (update)\n• /airdrops (daftar)\n• /air <keyword> (detail)\n• /tugas <keyword> (steps)"
    else:
        txt="Tanya apa saja terkait kripto/airdrop ✨"
    await q.edit_message_text(txt)

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
    if not ctx.args:
        await update.message.reply_text(f"FIAT saat ini: {FIAT_DEFAULT.upper()}\nFormat: /setfiat idr|usd|usdt|eur")
        return
    f = ctx.args[0].lower()
    if f not in FIAT_SET:
        await update.message.reply_text("❌ Fiat tidak valid.")
        return
    FIAT_DEFAULT = f
    await update.message.reply_text(f"✅ FIAT default: {f.upper()}")

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <coin> <fiat>")
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await sync_coin_map()
    txt = await cmd_price(sym, fiat)
    if txt:
        await update.message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # /prices btc,eth idr
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth [fiat]")
        return
    parts = ctx.args[0].split(",")
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await sync_coin_map()
    ids=[]; syms=[]
    for p in parts:
        cid=norm_symbol(p)
        if cid: ids.append(cid); syms.append(p.upper())
    if not ids: return
    js = await cg_simple_price(ids, fiat)
    lines = [f"📊 <b>Harga ({fiat.upper()})</b>"]
    for i,p in enumerate(parts):
        cid = norm_symbol(p)
        if cid and cid in js and fiat in js[cid]:
            price = js[cid][fiat]; chg = js[cid].get(f"{fiat}_24h_change")
            lines.append("• "+price_card(p, fiat, price, chg if isinstance(chg,(int,float)) else None))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args)<2:
        await update.message.reply_text("Format: /convert <jumlah> <coin> [fiat]")
        return
    try:
        amount = float(ctx.args[0])
    except Exception:
        await update.message.reply_text("Jumlah tidak valid.")
        return
    sym = ctx.args[1]
    fiat = (ctx.args[2] if len(ctx.args)>2 else FIAT_DEFAULT).lower()
    await sync_coin_map()
    txt = await cmd_convert(amount, sym, fiat)
    if txt:
        await update.message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)

# ===== Inline mode =====
async def inline_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = (update.inline_query.query or "").strip()
    await sync_coin_map()
    results=[]
    m = AMOUNT_PAIR.match(q)
    if m:
        amount=float(m.group(1)); sym=m.group(2); fiat=m.group(3).lower()
        if valid_pair(sym, fiat):
            txt = await cmd_convert(amount, sym, fiat)
            if txt:
                results.append(InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title=f"{amount:g} {sym.upper()} → {fiat.upper()}",
                    input_message_content=InputTextMessageContent(txt, parse_mode="HTML"),
                    description=html.unescape(re.sub("<.*?>","",txt))
                ))
    else:
        m = PAIR_ONLY.match(q)
        if m:
            sym=m.group(1); fiat=m.group(2).lower()
            if valid_pair(sym, fiat):
                txt = await cmd_price(sym, fiat)
                if txt:
                    results.append(InlineQueryResultArticle(
                        id=str(uuid.uuid4()),
                        title=f"Harga {sym.upper()}/{fiat.upper()}",
                        input_message_content=InputTextMessageContent(txt, parse_mode="HTML"),
                        description=html.unescape(re.sub("<.*?>","",txt))
                    ))
    if not results and q:
        results.append(InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="Format: btc usd atau 0.1 btc idr",
            input_message_content=InputTextMessageContent("Contoh: btc usd"),
            description="Ketik simbol & fiat / jumlah simbol fiat"
        ))
    await update.inline_query.answer(results, cache_time=20, is_personal=True)

# ===== Airdrops =====
def page_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    btns=[]
    if page>1: btns.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"air:{page-1}"))
    if page<pages: btns.append(InlineKeyboardButton("Next ➡️", callback_data=f"air:{page+1}"))
    return InlineKeyboardMarkup([btns]) if btns else None

async def airupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pages = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 1
    await update.message.reply_text(f"🔄 Update airdrops (pages={pages})…")
    items: List[Airdrop] = []
    for _ in range(pages):
        batch = await scrape_all()
        items += batch
        await asyncio.sleep(1)
    # uniq by slug
    mp={}; 
    for a in items:
        if a.slug not in mp or (a.reward!="- " and mp[a.slug].reward=="-"):
            mp[a.slug]=a
    final=list(mp.values())
    save_airdrops(final)
    await update.message.reply_text(
        f"✅ Selesai. Terkumpul <b>{len(final)}</b> airdrop.\nKetik /airdrops untuk daftar.",
        parse_mode="HTML"
    )

async def airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = load_airdrops()
    if not items:
        await update.message.reply_text("Belum ada data. Jalankan /airupdate dulu.")
        return
    per=7
    page=1
    pages=(len(items)+per-1)//per
    await show_air_page(update, items, page, pages)

async def air_nav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    page = int(q.data.split(":")[1])
    items = load_airdrops()
    per=7
    pages=(len(items)+per-1)//per
    await show_air_page(update, items, page, pages, edit=True)

async def show_air_page(update_or_q, items: List[Airdrop], page: int, pages: int, edit=False):
    per=7
    start=(page-1)*per; chunk=items[start:start+per]
    lines=[f"<b>Airdrop (hal {page}/{pages})</b>"]
    for a in chunk:
        nm=html.escape(a.name)
        lines.append(f"• <b>{nm}</b> (<a href=\"{html.escape(a.url)}\">{html.escape(a.source)}</a>)\n"
                     f"  Reward: {html.escape(a.reward)}\n  Chain: {html.escape(a.chain)}")
    kb = page_kb(page, pages)
    txt = "\n".join(lines)
    if edit and hasattr(update_or_q, "edit_message_text"):
        await update_or_q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        await update_or_q.message.reply_text(txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

async def air_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /air <keyword>")
        return
    kw=" ".join(ctx.args).lower()
    items=load_airdrops()
    pick=next((a for a in items if kw in a.slug or kw in a.name.lower()), None)
    if not pick:
        await update.message.reply_text("Tidak ditemukan.")
        return
    kb=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Buka", url=pick.url),
        InlineKeyboardButton("🧩 Tugas", callback_data=f"task:{pick.slug}"),
        InlineKeyboardButton("📣 Share", switch_inline_query=pick.name)
    ]])
    txt=f"<b>{html.escape(pick.name)}</b>\nReward: {html.escape(pick.reward)}\nChain: {html.escape(pick.chain)}\nSumber: {html.escape(pick.source)}"
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

async def task_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    slug=q.data.split(":",1)[1]
    items=load_airdrops()
    pick=next((a for a in items if a.slug==slug), None)
    if not pick:
        await q.edit_message_text("Tidak ditemukan.")
        return
    steps=extract_tasks(pick.url)
    if not steps:
        await q.edit_message_text(f"Tidak menemukan daftar tugas.\n{pick.url}")
        return
    lines=[f"<b>{html.escape(pick.name)}</b> – Tugas:"]
    for i,s in enumerate(steps,1):
        lines.append(f"{i}. {html.escape(s)}")
    await q.edit_message_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

# ===== Alerts =====
def load_watch(): return _read_json(WATCH_JSON, {})
def save_watch(d): _write_json(WATCH_JSON, d)

def parse_dir(s: str)->Optional[str]:
    s=s.lower()
    if s in {">","above","atas"}: return "above"
    if s in {"<","below","bawah"}: return "below"
    return None

async def watch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args)<4:
        await update.message.reply_text("Format: /watch <coin> <fiat> <above|below> <price>")
        return
    sym, fiat, d, p = ctx.args[0], ctx.args[1].lower(), ctx.args[2], ctx.args[3]
    await sync_coin_map()
    if not valid_pair(sym, fiat): return
    direction=parse_dir(d)
    try: price=float(p)
    except: return
    db=load_watch()
    arr=db.get(str(update.effective_chat.id), [])
    arr.append({"sym":sym.lower(),"fiat":fiat,"dir":direction,"price":price})
    db[str(update.effective_chat.id)]=arr
    save_watch(db)
    await update.message.reply_text(f"✅ Watch: {sym.upper()}/{fiat.upper()} {direction} {price:g}")

async def unwatch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db=load_watch(); db.pop(str(update.effective_chat.id), None); save_watch(db)
    await update.message.reply_text("✅ Semua watch dihapus.")

async def watchlist_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db=load_watch(); arr=db.get(str(update.effective_chat.id), [])
    if not arr: await update.message.reply_text("Watchlist kosong."); return
    lines=["Watchlist:"]
    for i,r in enumerate(arr,1):
        lines.append(f"{i}. {r['sym'].upper()}/{r['fiat'].upper()} {r['dir']} {r['price']}")
    await update.message.reply_text("\n".join(lines))

async def watch_job(ctx: ContextTypes.DEFAULT_TYPE):
    db=load_watch()
    want: Dict[str,set]={}
    for _,arr in db.items():
        for r in arr:
            cid=norm_symbol(r["sym"])
            if not cid: continue
            want.setdefault(r["fiat"], set()).add(cid)
    if not want: return
    prices: Dict[Tuple[str,str], float]={}
    for fiat, ids in want.items():
        try:
            js=await cg_simple_price(list(ids), fiat)
            for cid in ids:
                if cid in js and fiat in js[cid]:
                    prices[(cid,fiat)] = js[cid][fiat]
        except Exception as e:
            log.warning("watch fetch fail (%s): %s", fiat, e)
    changed=False
    for chat_id, arr in list(db.items()):
        new=[]
        for r in arr:
            cid=norm_symbol(r["sym"]); cur=prices.get((cid,r["fiat"]))
            if cur is None: new.append(r); continue
            hit=(r["dir"]=="above" and cur>=r["price"]) or (r["dir"]=="below" and cur<=r["price"])
            if hit:
                try:
                    await ctx.bot.send_message(int(chat_id),
                        text=f"⏰ Alert {r['sym'].upper()}/{r['fiat'].upper()} {r['dir']} {r['price']} — now {fmt_price(cur,r['fiat'])}",
                        disable_web_page_preview=True)
                except Exception as e:
                    log.warning("notify fail: %s", e)
                changed=True
            else:
                new.append(r)
        db[chat_id]=new
    if changed: save_watch(db)

# ===== Auto Airupdate =====
def load_auto(): return _read_json(AUTO_JSON, {"airupdate": True, "interval_min": 360})
def save_auto(d): _write_json(AUTO_JSON, d)

async def airauto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    st=load_auto()
    if not ctx.args:
        await update.message.reply_text(f"Auto AirUpdate: {'ON' if st.get('airupdate') else 'OFF'} / interval {st.get('interval_min',360)} menit")
        return
    a=ctx.args[0].lower()
    if a in {"on","off"}:
        st["airupdate"]=(a=="on"); save_auto(st); await update.message.reply_text(f"Auto -> {a.upper()}")
    elif a.isdigit():
        st["interval_min"]=int(a); save_auto(st); await update.message.reply_text(f"Interval {st['interval_min']} menit")
    else:
        await update.message.reply_text("Gunakan: /airauto on|off|<menit>")

async def auto_job(ctx: ContextTypes.DEFAULT_TYPE):
    st=load_auto()
    if not st.get("airupdate", True): return
    try:
        items=await scrape_all()
        # merge dengan data lama untuk jaga detail yang ada
        old={a.slug:a for a in load_airdrops()}
        for a in items:
            if a.slug not in old or (a.reward!="- " and old[a.slug].reward=="-"):
                old[a.slug]=a
        save_airdrops(list(old.values()))
    except Exception as e:
        log.warning("auto airupdate fail: %s", e)

# ===== AI & Router =====
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not client:
        await update.message.reply_text("❌ AI belum aktif.")
        return
    prompt=" ".join(ctx.args)
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>")
        return
    try:
        resp=client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            max_tokens=300, temperature=0.5
        )
        answer=resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.warning("AI error: %s", e)
        await update.message.reply_text("Maaf, AI sedang sibuk.")

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text=(update.message.text or "").strip()
    # 1) convert: "0.1 btc idr"
    m=AMOUNT_PAIR.match(text)
    if m:
        await sync_coin_map()
        amount=float(m.group(1)); sym=m.group(2); fiat=m.group(3).lower()
        if not valid_pair(sym, fiat): return
        txt=await cmd_convert(amount, sym, fiat)
        if txt: await update.message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)
        return
    # 2) price: "btc usd"
    m=PAIR_ONLY.match(text)
    if m:
        await sync_coin_map()
        sym=m.group(1); fiat=m.group(2).lower()
        if not valid_pair(sym, fiat): return
        txt=await cmd_price(sym, fiat)
        if txt: await update.message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)
        return
    # 3) fallback AI
    if client and text:
        try:
            resp=client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content":text}],
                max_tokens=220, temperature=0.6
            )
            answer=resp.choices[0].message.content.strip()
            await update.message.reply_text(answer)
        except Exception as e:
            log.warning("AI fallback error: %s", e)

# ===== MAIN =====
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(go_nav, pattern=r"^go:"))

    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))

    # airdrop
    app.add_handler(CommandHandler("airupdate", airupdate))
    app.add_handler(CommandHandler("airdrops", airdrops))
    app.add_handler(CallbackQueryHandler(air_nav, pattern=r"^air:"))
    app.add_handler(CommandHandler("air", air_detail))
    app.add_handler(CallbackQueryHandler(task_cb, pattern=r"^task:"))

    # alerts
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))

    # auto
    app.add_handler(CommandHandler("airauto", airauto))
    app.job_queue.run_repeating(watch_job, interval=60, first=15)
    st=load_auto()
    app.job_queue.run_repeating(auto_job, interval=60*st.get("interval_min",360), first=30)

    # AI
    app.add_handler(CommandHandler("ask", ask_cmd))

    # inline & text
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # prefetch coin map beberapa detik setelah start
    async def _warmup(_):
        try:
            await sync_coin_map()
        except Exception as e:
            log.warning("Warmup coin map gagal: %s", e)

    app.job_queue.run_once(lambda ctx: asyncio.create_task(_warmup(ctx)), when=5)
    
    return app

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN belum diisi (.env)")
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(sync_coin_map())  # warm-up mapping
    main()

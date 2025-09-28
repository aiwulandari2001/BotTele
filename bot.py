#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AirdropCore Telegram Bot (final)
- Crypto: /price, /prices, /convert, natural text ("btc usd", "0.25 eth idr", "btc")
- Airdrop: /airupdate [pages] [force], /airdrops (pagination), /tugas <keyword>,
           /airnews (yang baru), /airstatus, /airdebug, /airclear
- AI: tanpa /ask (ngetik biasa), /ask tetap tersedia (opsional)
"""

import os, re, json, pathlib, logging, asyncio, socket, random, time
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler,
)

# ===================== ENV & LOGGING =====================
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

BOT_TOKEN         = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "").strip()  # opsional
FIAT_DEFAULT      = os.getenv("FIAT_DEFAULT", "usd").lower()
AIR_REFRESH_HOURS = int(os.getenv("AIR_REFRESH_HOURS", "6"))
AIR_CACHE         = os.getenv("AIR_CACHE", "airdrops_cache.json")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN belum diisi. Set di .env atau environment variable.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("airdropcore.bot")

# ===================== OpenAI (opsional) =====================
client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    log.warning("OpenAI client nonaktif: %s", e)
    client = None
log.info("OpenAI client aktif" if client else "OpenAI client nonaktif")

# ===================== Preferensi FIAT per chat =====================
FIAT_PREFS: Dict[int, str] = {}   # chat_id -> fiat

def get_chat_fiat(chat_id: int) -> str:
    return FIAT_PREFS.get(chat_id, FIAT_DEFAULT)

def set_chat_fiat(chat_id: int, fiat: str) -> None:
    FIAT_PREFS[chat_id] = fiat.lower()

# ===================== Crypto helpers =====================
CG_BASE = "https://api.coingecko.com/api/v3"

SYMBOL_MAP = {
    "btc":"bitcoin","eth":"ethereum","bnb":"binancecoin","usdt":"tether","usdc":"usd-coin",
    "sol":"solana","ada":"cardano","xrp":"ripple","dot":"polkadot","doge":"dogecoin",
    "trx":"tron","matic":"polygon","ltc":"litecoin","avax":"avalanche-2","link":"chainlink",
    "ton":"the-open-network","op":"optimism","arb":"arbitrum","atom":"cosmos","sui":"sui",
    "apt":"aptos","near":"near","fil":"filecoin","bch":"bitcoin-cash","etc":"ethereum-classic",
}

def resolve_coin_id(sym: str) -> Optional[str]:
    s = sym.lower().strip()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if re.fullmatch(r"[a-z0-9-]{3,}", s):
        return s
    try:
        r = requests.get(f"{CG_BASE}/search", params={"query": s}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("coins"):
            return data["coins"][0]["id"]
    except Exception as e:
        log.warning("resolve_coin_id gagal untuk %s: %s", sym, e)
    return None

def fetch_price(ids: List[str], fiat: str) -> Dict[str, Dict[str, float]]:
    try:
        r = requests.get(
            f"{CG_BASE}/simple/price",
            params={
                "ids": ",".join(ids),
                "vs_currencies": fiat,
                "include_24hr_change": "true",
            },
            timeout=20
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("fetch_price gagal: %s", e)
        return {}

def fmt_price(val: float, fiat: str) -> str:
    try:
        return f"{val:,.4f} {fiat.upper()}"
    except Exception:
        return f"{val} {fiat.upper()}"

# Natural text parsing
PAIR_PATTERN   = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z0-9]+)\s+([a-zA-Z0-9]+)\s*$")  # "0.25 eth idr"
COIN_FIAT_PAT  = re.compile(r"^\s*([a-zA-Z0-9]+)[/ ]+([a-zA-Z0-9]+)\s*$")            # "btc usd"
SINGLE_COIN    = re.compile(r"^\s*([a-zA-Z0-9]{2,10})\s*$")                          # "btc"

# ===================== Airdrop: Model & Helpers =====================
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: str = ""
    reward: str = ""
    url: str = ""
    source: str = ""
    tasks: List[str] = field(default_factory=list)
    # tracking
    created_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())
    updated_at: float = field(default_factory=lambda: datetime.utcnow().timestamp())

AIRDROPS: List[Airdrop] = []
LAST_AIR_UPDATE: Optional[datetime] = None
NEW_SLUGS: Set[str] = set()

# Rotating UA
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; rv:123.0) Gecko/20100101 Firefox/123.0",
]
def rand_headers():
    return {
        "User-Agent": random.choice(UAS),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
    }

def _clean_text(s: Optional[str]) -> str:
    if not s: return ""
    return re.sub(r"\s+", " ", s).strip()

def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")

def _dns_ok(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except Exception:
        return False

# ===================== Scrapers (4 sumber) =====================
def scrape_airdrops_io(max_pages: int = 1) -> List[Airdrop]:
    base = "https://airdrops.io"
    urls = [f"{base}/latest/"]
    if max_pages >= 2:
        urls.append(f"{base}/upcoming/")

    out: List[Airdrop] = []
    for url in urls:
        r = requests.get(url, headers=rand_headers(), timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".airdrops-list .item, article, .card"):
            title_el = card.select_one(".title, h3, h2, a[title]") or card.select_one("a")
            name = _clean_text(title_el.get_text(" ", strip=True) if title_el else "")
            if not name: 
                continue
            a = card.select_one("a")
            href = a["href"] if (a and a.has_attr("href")) else url
            full_url = urljoin(base, href)
            reward_el = card.select_one(".reward, .prize, .subtitle, .reward-amount")
            chain_el  = card.select_one(".chain, .platform, .network")
            reward = _clean_text(reward_el.get_text() if reward_el else "")
            chain  = _clean_text(chain_el.get_text()  if chain_el  else "")
            slug = _slugify(name)
            out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward,
                               url=full_url, source="airdrops.io"))
    return out

def scrape_airdropking(max_pages: int = 1) -> List[Airdrop]:
    host = "airdropking.io"
    if not _dns_ok(host):
        raise RuntimeError("DNS airdropking.io tidak resolve, skip.")
    base = f"https://{host}"
    urls = [f"{base}/airdrops/"]
    out: List[Airdrop] = []
    for url in urls[:max_pages]:
        r = requests.get(url, headers=rand_headers(), timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("article, .airdrop-card, .card"):
            title_el = row.select_one("h2, h3, .title, a[title]") or row.select_one("a")
            name = _clean_text(title_el.get_text(" ", strip=True) if title_el else "")
            if not name: 
                continue
            a = row.select_one("a")
            href = a["href"] if (a and a.has_attr("href")) else url
            full_url = urljoin(base, href)
            reward_el = row.select_one(".reward, .rewards, .badge, .prize")
            chain_el  = row.select_one(".chain, .network, .platform")
            reward = _clean_text(reward_el.get_text() if reward_el else "")
            chain  = _clean_text(chain_el.get_text()  if chain_el  else "")
            slug = _slugify(name)
            out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward,
                               url=full_url, source="airdropking.io"))
    return out

def scrape_cryptorank(max_pages: int = 1) -> List[Airdrop]:
    host = "cryptorank.io"
    if not _dns_ok(host):
        raise RuntimeError("DNS cryptorank.io tidak resolve, skip.")
    base = f"https://{host}"
    urls = [f"{base}/drophunting"]
    out: List[Airdrop] = []
    for url in urls[:max_pages]:
        r = requests.get(url, headers=rand_headers(), timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("a[href*='/ico/'], a[href*='/airdrops/'], a[href*='/project/']")
        seen_links = set()
        for a in rows:
            href = a.get("href", ""); 
            if not href: 
                continue
            full = urljoin(base, href)
            if full in seen_links: 
                continue
            seen_links.add(full)
            name_el = a.select_one("h3, h2, .name, .title") or a
            name = _clean_text(name_el.get_text(" ", strip=True)) if name_el else ""
            if not name or len(name) < 2:
                continue
            wrap_txt = _clean_text(a.get_text(" ", strip=True))
            reward = ""
            m_reward = re.search(r"(reward|value|worth)\s*[:\-]?\s*([^\|]{3,60})", wrap_txt, re.I)
            if m_reward:
                reward = _clean_text(m_reward.group(2))
            chain = ""
            m_chain = re.search(r"\b(ETH|Ethereum|BSC|BNB|Solana|SOL|Polygon|Base|Arbitrum|Optimism|Aptos|Sui|Linea|zkSync|Starknet|TON|Tron|AVAX)\b", wrap_txt, re.I)
            chain = m_chain.group(0) if m_chain else ""
            slug = _slugify(name)
            out.append(Airdrop(slug=slug, name=name, reward=reward, chain=chain,
                               url=full, source="cryptorank.io"))
    return out

def scrape_coingecko_airdrops(max_pages: int = 1) -> List[Airdrop]:
    host = "www.coingecko.com"
    if not _dns_ok(host):
        raise RuntimeError("DNS coingecko.com tidak resolve, skip.")
    base = f"https://{host}"
    urls = [f"{base}/airdrops"]
    out: List[Airdrop] = []
    for url in urls[:max_pages]:
        r = requests.get(url, headers=rand_headers(), timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("a[href*='/airdrops/'], tr a[href*='/coins/'], .tw-card a")
        seen = set()
        for a in cards:
            href = a.get("href", "")
            if not href: 
                continue
            full = urljoin(base, href)
            if full in seen: 
                continue
            seen.add(full)
            name_el = a.select_one("h3, h2, .font-bold, .tw-text, .tw-truncate") or a
            name = _clean_text(name_el.get_text(" ", strip=True)) if name_el else ""
            if not name or len(name) < 2:
                continue
            parent = a.find_parent(["tr","li","div"]) or a
            ptxt = _clean_text(parent.get_text(" ", strip=True))
            reward = ""
            chain  = ""
            m = re.search(r"(reward|worth|value)\s*[:\-]?\s*([^\|]{3,60})", ptxt, re.I)
            if m:
                reward = _clean_text(m.group(2))
            m2 = re.search(r"\b(ETH|Ethereum|BSC|BNB|Solana|SOL|Polygon|Base|Arbitrum|Optimism|Aptos|Sui|Linea|zkSync|Starknet|TON|Tron|AVAX)\b", ptxt, re.I)
            chain = m2.group(0) if m2 else ""
            slug = _slugify(name)
            out.append(Airdrop(slug=slug, name=name, reward=reward, chain=chain,
                               url=full, source="coingecko"))
    return out

# ===================== Aggregator (return list+stats) =====================
def scrape_airdrops_sync(max_pages: int = 1):
    results: List[Airdrop] = []
    stats = []

    for fn, label in [
        (scrape_airdrops_io, "airdrops.io"),
        (scrape_airdropking, "airdropking.io"),
        (scrape_cryptorank, "cryptorank"),
        (scrape_coingecko_airdrops, "coingecko"),
    ]:
        try:
            before = len(results)
            results.extend(fn(max_pages=max_pages))
            stats.append((label, len(results) - before, "OK"))
        except Exception as e:
            stats.append((label, 0, f"{type(e).__name__}: {e}"))

    # dedup sementara
    uniq: Dict[str, Airdrop] = {}
    for a in results:
        if a.slug not in uniq:
            uniq[a.slug] = a
        else:
            if (a.reward and not uniq[a.slug].reward) or (a.chain and not uniq[a.slug].chain):
                uniq[a.slug] = a

    return list(uniq.values()), stats

# ===================== Enrich detail (tasks + tombol) =====================
def enrich_airdrop_details(a: Airdrop) -> Airdrop:
    try:
        r = requests.get(a.url, headers=rand_headers(), timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        tasks: List[str] = []
        for sel in ["article li", ".content li", ".single-post li", ".steps li", ".howto li", "li"]:
            for li in soup.select(sel):
                txt = _clean_text(li.get_text(" ", strip=True))
                if txt and 5 <= len(txt) <= 180:
                    tasks.append(txt)
            if tasks:
                break

        buttons: List[InlineKeyboardButton] = []
        def add_btn(label: str, href: str):
            if href and href.startswith("http"):
                buttons.append(InlineKeyboardButton(label, url=href))

        for a_tag in soup.select("a[href]"):
            href = a_tag["href"].strip()
            low  = href.lower()
            if "t.me/" in low:
                add_btn("📨 Telegram", href)
            elif "twitter.com" in low or "x.com" in low:
                add_btn("🐦 X/Twitter", href)
            elif "discord.gg" in low or "discord.com" in low:
                add_btn("💬 Discord", href)
            elif "galxe.com" in low:
                add_btn("🪐 Galxe", href)
            elif "zealy.io" in low:
                add_btn("⚡ Zealy", href)
            elif "questn" in low or "quest3" in low:
                add_btn("🎯 QuestN", href)
            elif "app." in low or "claim" in low:
                add_btn("🧩 App/Claim", href)

        if tasks:
            a.tasks = tasks[:12]
        setattr(a, "_buttons", buttons[:6])
    except Exception as e:
        log.warning("enrich_airdrop_details gagal untuk %s: %s", a.url, e)
    return a

# ===================== Cache helpers =====================
def save_cache():
    try:
        tmp = AIR_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(a) for a in AIRDROPS], f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, AIR_CACHE)
    except Exception as e:
        log.warning("save_cache gagal: %s", e)

def load_cache():
    try:
        p = pathlib.Path(AIR_CACHE)
        if p.exists():
            data = json.load(open(p, "r", encoding="utf-8"))
            AIRDROPS.clear()
            for d in data:
                # kompatibel jika cache lama belum punya created_at/updated_at
                if "created_at" not in d: d["created_at"] = datetime.utcnow().timestamp()
                if "updated_at" not in d: d["updated_at"] = d["created_at"]
                AIRDROPS.append(Airdrop(**d))
            log.info("Cache dimuat: %d airdrops", len(AIRDROPS))
    except Exception as e:
        log.warning("load_cache gagal: %s", e)

# ===================== Debug konektivitas =====================
AIR_DEBUG_HOSTS = [
    ("airdrops.io", "https://airdrops.io/latest/"),
    ("airdropking.io", "https://airdropking.io/airdrops/"),
    ("cryptorank.io", "https://cryptorank.io/drophunting"),
    ("coingecko.com", "https://www.coingecko.com/airdrops"),
]
def _http_head(url: str, timeout=12):
    try:
        r = requests.get(url, headers=rand_headers(), timeout=timeout)
        return r.status_code, len(r.text)
    except Exception as e:
        return f"ERR: {type(e).__name__}", 0

# ===================== Pagination util =====================
def _paged(items: List[Airdrop], page: int, per_page: int = 5) -> List[Airdrop]:
    start = (page - 1) * per_page
    return items[start:start + per_page]

def _air_kb(page: int, total: int, per_page: int = 5):
    btns = []
    if page > 1:
        btns.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"air_prev:{page-1}"))
    if page * per_page < total:
        btns.append(InlineKeyboardButton("Next ➡️", callback_data=f"air_next:{page+1}"))
    if not btns:
        btns = [InlineKeyboardButton("🔄 Refresh", callback_data="air_refresh:1")]
    return InlineKeyboardMarkup([btns])

def _air_list_text(items: List[Airdrop]) -> str:
    lines = ["📋 Airdrop terdeteksi:\n"]
    for a in items:
        is_new = " [NEW]" if a.slug in NEW_SLUGS else ""
        lines.append(f"• <b>{a.name}</b>{is_new} — {a.reward or '-'} ({a.chain or '-'})\n  {a.url}")
    return "\n".join(lines)

# ===================== Commands: start/help =====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔁 Convert", callback_data="menu_conv")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di AirdropCore Bot!\n\n"
        "• Ketik bebas: `btc usd`, `0.25 eth idr` (AI juga tanpa /ask)\n"
        "• /price <coin> [fiat]\n• /prices btc,eth idr\n• /convert 123 sol usd\n"
        "• /setfiat idr|usd|usdt|eur\n"
        "• /airupdate [pages] [force], /airdrops, /tugas <keyword>\n"
        "• /airnews, /airstatus, /airdebug, /airclear\n",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

# ===================== Commands: FIAT & AI =====================
async def setfiat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: {get_chat_fiat(chat_id).upper()}\n"
            "Format: /setfiat idr|usd|usdt|eur"
        )
        return
    fiat = ctx.args[0].lower()
    if fiat not in {"idr","usd","usdt","eur"}:
        await update.message.reply_text("❌ Fiat tidak valid.")
        return
    set_chat_fiat(chat_id, fiat)
    await update.message.reply_text(f"✅ FIAT diset ke {fiat.upper()}")

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # opsional: /ask <pertanyaan>
    return await ai_answer(update, " ".join(ctx.args).strip())

async def ai_answer(update: Update, text: str):
    if not text:
        await update.message.reply_text("Tulis pertanyaanmu.")
        return
    if not client:
        # tanpa AI, diamkan agar router lain bisa jawab
        await update.message.reply_text("❌ Fitur AI belum aktif (OPENAI_API_KEY kosong).")
        return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": text}],
            max_tokens=400, temperature=0.5
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

# ===================== Commands: Harga/Convert =====================
async def reply_price(update: Update, sym: str, fiat: str):
    cid = resolve_coin_id(sym)
    if not cid:
        await update.message.reply_text(f"❌ {sym.upper()} tidak ditemukan.")
        return
    data = fetch_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        await update.message.reply_text(f"❌ Pair {sym.upper()}-{fiat.upper()} tidak tersedia.")
        return
    price = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price, fiat)}{chg_txt}")

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\ncontoh: /price btc usdt")
        return
    sym  = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else get_chat_fiat(chat_id)).lower()
    await reply_price(update, sym, fiat)

async def prices_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Format: /prices btc,eth [fiat]")
        return
    coins = [c.strip() for c in ctx.args[0].split(",") if c.strip()]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else get_chat_fiat(chat_id)).lower()
    ids = []; name_map = {}
    for c in coins:
        cid = resolve_coin_id(c)
        if cid:
            ids.append(cid); name_map[cid] = c.upper()
    if not ids:
        await update.message.reply_text("❌ Coin tidak ditemukan.")
        return
    data = fetch_price(ids, fiat)
    lines = []
    for cid in ids:
        if cid in data and fiat in data[cid]:
            price = data[cid][fiat]
            chg = data[cid].get(f"{fiat}_24h_change")
            chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
            lines.append(f"• {name_map[cid]} = {fmt_price(price, fiat)}{chg_txt}")
    await update.message.reply_text("\n".join(lines) if lines else "❌ Gagal ambil harga.")

async def convert_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amount> <coin> <fiat>\nContoh: /convert 0.25 btc idr")
        return
    try:
        amount = float(ctx.args[0])
    except Exception:
        await update.message.reply_text("❌ amount harus angka. Contoh: /convert 0.25 btc idr")
        return
    sym    = ctx.args[1]
    fiat   = ctx.args[2].lower() if len(ctx.args) >= 3 else get_chat_fiat(chat_id)
    cid = resolve_coin_id(sym)
    if not cid:
        await update.message.reply_text("❌ Coin tidak ditemukan.")
        return
    data = fetch_price([cid], fiat)
    if cid not in data or fiat not in data[cid]:
        await update.message.reply_text("❌ Pair tidak tersedia.")
        return
    price = data[cid][fiat]
    total = price * amount
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg,(int,float)) else ""
    await update.message.reply_text(f"{amount:g} {sym.upper()} ≈ {fmt_price(total, fiat)}{chg_txt}")

# ===================== Commands: Airdrop =====================
async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # /airupdate [pages] [force]
    pages = 1
    force = False
    if ctx.args:
        for arg in ctx.args:
            if arg.isdigit():
                pages = max(1, min(5, int(arg)))
            elif arg.lower() == "force":
                force = True

    await update.message.reply_text(f"🔄 Update airdrops (pages={pages}, force={force})…")
    loop = asyncio.get_running_loop()
    try:
        fresh_list, stats = await loop.run_in_executor(None, scrape_airdrops_sync, pages)

        # --- MERGE ke cache lama, bukan overwrite mentah ---
        old_by_slug: Dict[str, Airdrop] = {a.slug: a for a in AIRDROPS}
        global NEW_SLUGS
        NEW_SLUGS = set()

        merged: Dict[str, Airdrop] = {}
        for a in fresh_list:
            if a.slug in old_by_slug:
                prev = old_by_slug[a.slug]
                a.created_at = prev.created_at
                a.updated_at = datetime.utcnow().timestamp()
                if not a.reward and prev.reward: a.reward = prev.reward
                if not a.chain  and prev.chain:  a.chain  = prev.chain
            else:
                NEW_SLUGS.add(a.slug)
                a.created_at = datetime.utcnow().timestamp()
                a.updated_at = a.created_at
            merged[a.slug] = a

        prev_cnt = len(AIRDROPS)
        new_cnt  = len(merged)
        threshold = max(3, int(prev_cnt * 0.40))  # minimal 40% dari cache lama
        if not force and prev_cnt > 0 and new_cnt < threshold:
            await update.message.reply_text(
                f"⚠️ Hasil baru terlalu sedikit ({new_cnt}) < threshold {threshold}. Cache lama dipertahankan.\n"
                f"Jalankan '/airupdate {pages} force' untuk menimpa."
            )
            return

        # commit & sort terbaru dulu
        AIRDROPS.clear()
        AIRDROPS.extend(merged.values())
        AIRDROPS.sort(key=lambda x: (x.created_at, x.updated_at), reverse=True)

        global LAST_AIR_UPDATE
        LAST_AIR_UPDATE = datetime.utcnow()
        save_cache()

        rep = [f"✅ Selesai. Terkumpul {len(AIRDROPS)} airdrop."]
        if NEW_SLUGS:
            rep.append(f"🆕 Baru: {len(NEW_SLUGS)}")
        rep.append("<b>Per sumber</b>:")
        for label, count, note in stats:
            rep.append(f"• {label}: +{count}" if count > 0 else f"• {label}: 0 ({note})")
        rep.append("\n/airdrops untuk daftar, /airnews untuk yang baru.")
        await update.message.reply_html("\n".join(rep))

    except Exception as e:
        await update.message.reply_text(f"❌ Gagal update: {e}")

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not AIRDROPS:
        await update.message.reply_text("⚠️ Belum ada data. Kirim /airupdate untuk mengisi daftar.")
        return
    AIRDROPS.sort(key=lambda x: (x.created_at, x.updated_at), reverse=True)
    page = 1
    per_page = 5
    chunk = _paged(AIRDROPS, page, per_page)
    txt = _air_list_text(chunk)
    await update.message.reply_html(txt, reply_markup=_air_kb(page, len(AIRDROPS), per_page))

async def airnews_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not AIRDROPS:
        await update.message.reply_text("⚠️ Belum ada data. Jalankan /airupdate dulu.")
        return
    news = [a for a in AIRDROPS if a.slug in NEW_SLUGS]
    if not news:
        await update.message.reply_text("Tidak ada item baru sejak update terakhir.")
        return
    news.sort(key=lambda x: x.created_at, reverse=True)
    subset = news[:10]
    await update.message.reply_html(_air_list_text(subset))

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>\nContoh: /tugas monad")
        return
    key = " ".join(ctx.args).lower()
    found = [a for a in AIRDROPS if key in a.slug or key in a.name.lower()]
    if not found:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{key}'.")
        return
    a = enrich_airdrop_details(found[0])  # ambil detail saat diminta
    tasks = a.tasks or ["Join Telegram", "Follow X", "Claim in app"]
    task_txt = "\n".join([f"• {t}" for t in tasks])
    kb = None
    btns = getattr(a, "_buttons", None)
    if btns:
        rows = [btns[i:i+2] for i in range(0, len(btns), 2)]
        kb = InlineKeyboardMarkup(rows)
    await update.message.reply_html(
        f"🎁 <b>{a.name}</b>\n"
        f"Reward: {a.reward or '-'}\n"
        f"Chain: {a.chain or '-'}\n"
        f"Sumber: {a.source}\n"
        f"URL: {a.url}\n\n"
        f"<b>Tugas:</b>\n{task_txt}",
        reply_markup=kb
    )

async def airstatus_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ts = LAST_AIR_UPDATE.isoformat(timespec="seconds") + "Z" if LAST_AIR_UPDATE else "-"
    await update.message.reply_text(
        f"📡 Airdrop cached: {len(AIRDROPS)}\n"
        f"⏱️ Last update (UTC): {ts}\n"
        f"⏲️ Interval: {AIR_REFRESH_HOURS} jam"
    )

async def airdebug_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["🔎 <b>AirDebug</b>"]
    for host, url in AIR_DEBUG_HOSTS:
        dns_ok = _dns_ok(host)
        status, size = _http_head(url, timeout=15) if dns_ok else ("DNS_FAIL", 0)
        lines.append(f"• {host}: DNS={'OK' if dns_ok else 'FAIL'} | HTTP={status} | size={size}")
    lines.append(f"\nCache items: {len(AIRDROPS)}")
    lines.append(f"Last update: {LAST_AIR_UPDATE.isoformat(timespec='seconds')+'Z' if LAST_AIR_UPDATE else '-'}")
    await update.message.reply_html("\n".join(lines))

async def airclear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LAST_AIR_UPDATE, NEW_SLUGS
    AIRDROPS.clear()
    NEW_SLUGS = set()
    LAST_AIR_UPDATE = None
    try:
        p = pathlib.Path(AIR_CACHE)
        if p.exists(): p.unlink()
    except Exception as e:
        log.warning("hapus cache gagal: %s", e)
    await update.message.reply_text("🧹 Cache airdrop dihapus. Jalankan /airupdate untuk isi ulang.")

# ===================== Menu & Text Router =====================
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = ("Contoh:\n• /price btc usdt\n• btc usd\n• 0.25 eth idr\n"
               "• /prices btc,eth idr\n• /convert 2 sol usd")
    elif data == "menu_conv":
        txt = ("Convert:\n• /convert <amount> <coin> <fiat>\n"
               "• Contoh: /convert 0.1 btc idr")
    elif data == "menu_air":
        txt = ("Airdrop:\n• /airupdate [pages] [force]\n"
               "• /airdrops (list + Next/Prev)\n"
               "• /tugas <keyword> (detail + tombol link)\n"
               "• /airnews (yang baru), /airstatus, /airdebug, /airclear")
    elif data == "menu_ai":
        txt = "AI: ketik pertanyaan langsung (tanpa /ask)."
    else:
        txt = "Pilih menu di bawah ini."
    await q.edit_message_text(txt)

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) "0.25 eth idr"
    m = PAIR_PATTERN.match(text)
    if m:
        try:
            amount = float(m.group(1))
        except Exception:
            return await update.message.reply_text("Format: 0.25 eth idr")
        sym = m.group(2); fiat = m.group(3).lower()
        class DummyArgs(list): pass
        ctx.args = DummyArgs([str(amount), sym, fiat])
        return await convert_cmd(update, ctx)

    # 2) "btc usd" atau "eth idr"
    m = COIN_FIAT_PAT.match(text)
    if m:
        sym, fiat = m.groups()
        return await reply_price(update, sym, fiat.lower())

    # 3) "btc" saja => pakai fiat default chat
    m = SINGLE_COIN.match(text)
    if m:
        sym = m.group(1)
        fiat = get_chat_fiat(update.effective_chat.id)
        return await reply_price(update, sym, fiat)

    # 4) fallback AI (tanpa /ask)
    if client:
        return await ai_answer(update, text)

    await update.message.reply_text("Perintah tidak dikenali. Ketik /help.")

# ===================== Auto-refresh (JobQueue) =====================
async def job_airupdate(context):
    log.info("JobQueue: mulai auto-refresh…")
    loop = asyncio.get_running_loop()
    try:
        fresh_list, _stats = await loop.run_in_executor(None, scrape_airdrops_sync, 1)

        # merge ringan tanpa NEW_SLUGS (job periodik)
        old_by_slug: Dict[str, Airdrop] = {a.slug: a for a in AIRDROPS}
        merged: Dict[str, Airdrop] = {}
        for a in fresh_list:
            if a.slug in old_by_slug:
                prev = old_by_slug[a.slug]
                a.created_at = prev.created_at
                a.updated_at = datetime.utcnow().timestamp()
                if not a.reward and prev.reward: a.reward = prev.reward
                if not a.chain  and prev.chain:  a.chain  = prev.chain
            merged[a.slug] = a
        if merged:
            AIRDROPS.clear()
            AIRDROPS.extend(merged.values())
            AIRDROPS.sort(key=lambda x: (x.created_at, x.updated_at), reverse=True)

        global LAST_AIR_UPDATE
        LAST_AIR_UPDATE = datetime.utcnow()
        save_cache()
        log.info("Auto-refresh OK: %d airdrops", len(AIRDROPS))
    except Exception as e:
        log.warning("Auto-refresh gagal: %s", e)

# ===================== Runner =====================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # load cache dulu
    load_cache()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))

    # airdrop
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))
    app.add_handler(CommandHandler("airstatus", airstatus_cmd))
    app.add_handler(CommandHandler("airdebug", airdebug_cmd))
    app.add_handler(CommandHandler("airclear", airclear_cmd))
    app.add_handler(CommandHandler("airnews", airnews_cmd))
    app.add_handler(CallbackQueryHandler(on_menu_cb))
    app.add_handler(CallbackQueryHandler(lambda u, c: None, pattern=r"^air_(prev|next|refresh):"))  # keep pattern reserved
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # schedule auto-refresh (mulai 10 detik, lalu tiap AIR_REFRESH_HOURS jam)
    app.job_queue.run_repeating(job_airupdate, interval=timedelta(hours=AIR_REFRESH_HOURS), first=10)

    log.info("Bot polling started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, logging, asyncio, socket
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler,
)

# ---------- ENV ----------
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN belum diisi di ENV!")

# ---------- OpenAI (opsional) ----------
client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    client = None

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("airdropcore.bot")
log.info("OpenAI client aktif" if client else "OpenAI client nonaktif")

# ---------- FIAT & preferensi per chat ----------
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()
FIAT_PREFS: Dict[int, str] = {}   # chat_id -> fiat

def get_chat_fiat(chat_id: int) -> str:
    return FIAT_PREFS.get(chat_id, FIAT_DEFAULT)

def set_chat_fiat(chat_id: int, fiat: str) -> None:
    FIAT_PREFS[chat_id] = fiat.lower()

# ---------- Crypto helpers ----------
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
    # coba langsung: kalau user sudah kirim id coingecko, pakai saja
    # (id coingecko biasanya huruf kecil dan ada tanda '-')
    if re.fullmatch(r"[a-z0-9-]{3,}", s):
        return s
    # fallback: /search
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

# ---------- Natural text parsing ----------
PAIR_PATTERN   = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z0-9]+)\s+([a-zA-Z0-9]+)\s*$")
COIN_FIAT_PAT  = re.compile(r"^\s*([a-zA-Z0-9]+)[/ ]+([a-zA-Z0-9]+)\s*$")
SINGLE_COIN    = re.compile(r"^\s*([a-zA-Z0-9]{2,10})\s*$")

# ---------- Airdrop: model & helpers ----------
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: str = ""
    reward: str = ""
    url: str = ""
    source: str = ""
    tasks: List[str] = field(default_factory=list)

AIRDROPS: List[Airdrop] = []

UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
}

def _clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s

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

def scrape_airdrops_io(max_pages: int = 1) -> List[Airdrop]:
    base = "https://airdrops.io"
    urls = [f"{base}/latest/"]
    if max_pages >= 2:
        urls.append(f"{base}/upcoming/")

    out: List[Airdrop] = []
    for url in urls:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for card in soup.select(".airdrops-list .item, article, .card"):
            title_el = card.select_one(".title, h3, h2, a[title]")
            name = _clean_text(title_el.get_text() if title_el else None)
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
            out.append(Airdrop(
                slug=slug, name=name, chain=chain, reward=reward,
                url=full_url, source="airdrops.io"
            ))
    return out

def scrape_airdropking(max_pages: int = 1) -> List[Airdrop]:
    host = "airdropking.io"
    if not _dns_ok(host):
        raise RuntimeError("DNS airdropking.io tidak resolve, skip sumber ini.")

    base = f"https://{host}"
    urls = [f"{base}/airdrops/"]
    out: List[Airdrop] = []

    for url in urls[:max_pages]:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for row in soup.select("article, .airdrop-card, .card"):
            title_el = row.select_one("h2, h3, .title, a[title]")
            name = _clean_text(title_el.get_text() if title_el else None)
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
            out.append(Airdrop(
                slug=slug, name=name, chain=chain, reward=reward,
                url=full_url, source="airdropking.io"
            ))
    return out

def scrape_airdrops_sync(max_pages: int = 1) -> List[Airdrop]:
    results: List[Airdrop] = []

    try:
        results.extend(scrape_airdrops_io(max_pages=max_pages))
    except Exception as e:
        log.warning("scrape_airdrops_io gagal: %s", e)

    try:
        results.extend(scrape_airdropking(max_pages=max_pages))
    except Exception as e:
        log.warning("scrape_airdropking gagal: %s", e)

    uniq: Dict[str, Airdrop] = {}
    for a in results:
        if a.slug not in uniq or (a.reward and not uniq[a.slug].reward):
            uniq[a.slug] = a

    final_list = list(uniq.values())
    if not final_list:
        final_list = [
            Airdrop(
                slug="example-airdrop",
                name="Example Airdrop",
                reward="100 TEST",
                chain="ETH",
                url="https://example.com",
                source="fallback",
                tasks=["Join Telegram", "Follow X", "Claim in app"],
            )
        ]
    return final_list

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

# ---------- Commands ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔁 Convert", callback_data="menu_conv")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di AirdropCore Bot!\n\n"
        "• Ketik bebas: `btc usd`, `0.25 eth idr`\n"
        "• /price <coin> [fiat]\n• /prices btc,eth idr\n• /convert 123 sol usd\n"
        "• /setfiat idr|usd|usdt|eur\n"
        "• /airupdate, /airdrops, /tugas <keyword>\n",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

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
    if not client:
        await update.message.reply_text("❌ Fitur AI belum aktif (OPENAI_API_KEY kosong).")
        return
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>")
        return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=400, temperature=0.5
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

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
    ids = []
    name_map = {}
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
    amount = float(ctx.args[0])
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
    await update.message.reply_text(
        f"{amount:g} {sym.upper()} ≈ {fmt_price(total, fiat)}{chg_txt}"
    )

# ---------- Airdrop Commands ----------
async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Sedang update daftar airdrops…")
    loop = asyncio.get_running_loop()
    try:
        new_list = await loop.run_in_executor(None, scrape_airdrops_sync, 1)
        AIRDROPS.clear()
        AIRDROPS.extend(new_list)
        await update.message.reply_text(f"✅ Scraper selesai. Terkumpul {len(AIRDROPS)} airdrop.\nKetik /airdrops untuk melihat daftar.")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal update: {e}")

def _air_kb(page: int, total: int, per_page: int = 5):
    btns = []
    if page > 1:
        btns.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"air_prev:{page-1}"))
    if page * per_page < total:
        btns.append(InlineKeyboardButton("Next ➡️", callback_data=f"air_next:{page+1}"))
    if not btns:
        btns = [InlineKeyboardButton("🔄 Refresh", callback_data="air_refresh:1")]
    return InlineKeyboardMarkup([btns])

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not AIRDROPS:
        await update.message.reply_text("⚠️ Belum ada data. Kirim /airupdate untuk mengisi daftar.")
        return
    page = 1
    per_page = 5
    chunk = _paged(AIRDROPS, page, per_page)
    lines = [f"📋 Airdrop terdeteksi (Top {len(AIRDROPS)}):\n"]
    for a in chunk:
        lines.append(f"• <b>{a.name}</b> — {a.reward or '-'} ({a.chain or '-'})\n  {a.url}")
    txt = "\n".join(lines)
    await update.message.reply_html(txt, reply_markup=_air_kb(page, len(AIRDROPS), per_page))

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>\nContoh: /tugas monad")
        return
    key = " ".join(ctx.args).lower()
    found = [a for a in AIRDROPS if key in a.slug or key in a.name.lower()]
    if not found:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{key}'.")
        return
    a = found[0]
    tasks = a.tasks or ["Join Telegram", "Follow X", "Claim in app"]
    task_txt = "\n".join([f"• {t}" for t in tasks])
    await update.message.reply_html(
        f"🎁 <b>{a.name}</b>\nReward: {a.reward or '-'}\nChain: {a.chain or '-'}\n"
        f"Sumber: {a.source}\nURL: {a.url}\n\n<b>Tugas:</b>\n{task_txt}"
    )

async def air_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data or ""
    if data.startswith(("air_prev", "air_next", "air_refresh")):
        try:
            page = int(data.split(":")[1])
        except Exception:
            page = 1
        per_page = 5
        if not AIRDROPS:
            await q.edit_message_text("⚠️ Belum ada data. Kirim /airupdate untuk mengisi daftar.")
            return
        chunk = _paged(AIRDROPS, page, per_page)
        lines = [f"📋 Airdrop terdeteksi (Top {len(AIRDROPS)}):\n"]
        for a in chunk:
            lines.append(f"• <b>{a.name}</b> — {a.reward or '-'} ({a.chain or '-'})\n  {a.url}")
        txt = "\n".join(lines)
        await q.edit_message_text(
            text=txt, reply_markup=_air_kb(page, len(AIRDROPS), per_page), parse_mode="HTML"
        )

# ---------- Core price reply ----------
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

# ---------- Free text router ----------
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) "0.25 eth idr"
    m = PAIR_PATTERN.match(text)
    if m:
        amount = float(m.group(1)); sym = m.group(2); fiat = m.group(3).lower()
        # gunakan convert
        class DummyArgs(list): pass
        ctx.args = DummyArgs([str(amount), sym, fiat])
        return await convert_cmd(update, ctx)

    # 2) "btc usd" / "eth idr"
    m = COIN_FIAT_PAT.match(text)
    if m:
        sym, fiat = m.groups()
        return await reply_price(update, sym, fiat.lower())

    # 3) "btc" saja => gunakan fiat default
    m = SINGLE_COIN.match(text)
    if m:
        sym = m.group(1)
        fiat = get_chat_fiat(update.effective_chat.id)
        return await reply_price(update, sym, fiat)

    # 4) fallback AI
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": text}],
                max_tokens=300, temperature=0.6
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer)
            return
        except Exception as e:
            log.warning("AI fallback error: %s", e)

    await update.message.reply_text("Perintah tidak dikenali. Ketik /help.")

# ---------- Menu callback ----------
async def on_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = ("Contoh:\n• /price btc usdt\n• btc usd\n• 0.25 eth idr\n"
               "• /prices btc,eth idr\n• /convert 2 sol usd")
    elif data == "menu_conv":
        txt = ("Convert:\n• /convert <amount> <coin> <fiat>\n"
               "• Contoh: /convert 0.1 btc idr")
    elif data == "menu_air":
        txt = ("Airdrop:\n• /airupdate (update daftar)\n"
               "• /airdrops (daftar + tombol Next/Prev)\n"
               "• /tugas <keyword> (lihat detail tugas)")
    elif data == "menu_ai":
        txt = "AI Chat: /ask <pertanyaan>"
    else:
        txt = "Pilih menu di bawah ini."
    await q.edit_message_text(txt)

# ---------- Runner ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

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
    app.add_handler(CallbackQueryHandler(air_cb, pattern=r"^air_(prev|next|refresh):"))

    # menu & teks bebas
    app.add_handler(CallbackQueryHandler(on_menu_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

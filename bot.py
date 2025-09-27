#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, math, time, logging, asyncio
from dataclasses import dataclass, field
from typing import Optional, List, Dict

import requests
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ============ KONFIGURASI ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# OpenAI client (opsional)
client = None
try:
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as _e:
    client = None  # biarkan tanpa AI jika lib/KEY belum tersedia

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("airdropcore.bot")

# ====== STATE SEDERHANA ======
# fiat per chat (default usd)
CHAT_FIAT: Dict[int, str] = {}

def get_chat_fiat(chat_id: int) -> str:
    return CHAT_FIAT.get(chat_id, "usd")

def set_chat_fiat(chat_id: int, fiat: str) -> None:
    CHAT_FIAT[chat_id] = fiat.lower()

# ====== UTIL KRIPTO ======
UA = {"User-Agent": "Mozilla/5.0 (compatible; AirdropCoreBot/1.0)"}

SYMBOL_MAP = {
    # top coins + beberapa populer
    "btc": "bitcoin", "xbt": "bitcoin",
    "eth": "ethereum", "bnb": "binancecoin",
    "usdt": "tether", "usdc": "usd-coin",
    "sol": "solana", "xrp": "ripple",
    "ada": "cardano", "doge": "dogecoin",
    "trx": "tron", "matic": "polygon",
    "ton": "the-open-network", "dot": "polkadot",
    "ltc": "litecoin", "bch": "bitcoin-cash",
    # contoh coin komunitas
    "pi": "pi-network",  # kalau belum ada di Coingecko, akan gagal dan bot jelaskan
}

FIATS = {"usd", "idr", "eur", "usdt"}

PAIR_FREE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s+([a-z0-9\-]+)\s+([a-z0-9\-]+)\s*$", re.I)
PAIR_WORD = re.compile(r"^\s*([a-z0-9\-]{2,15})\s+([a-z0-9\-]{2,10})\s*$", re.I)

def norm_symbol(sym: str) -> str:
    s = (sym or "").lower()
    return SYMBOL_MAP.get(s, s)

def coingecko_simple(ids: List[str], vs: List[str]) -> dict:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": ",".join(ids),
        "vs_currencies": ",".join(vs),
        "include_24hr_change": "true",
    }
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()

def fmt_price(val: float, fiat: str) -> str:
    if fiat.lower() in {"idr"}:
        return f"Rp {val:,.0f}".replace(",", ".")
    return f"{val:,.4f} {fiat.upper()}"

async def reply_price(update: Update, sym: str, fiat: str, amount: float = 1.0):
    sym_norm = norm_symbol(sym)
    fiat = fiat.lower()
    if fiat not in FIATS:
        await update.message.reply_text("❌ Fiat tidak valid. Gunakan: idr/usd/eur/usdt")
        return
    try:
        data = coingecko_simple([sym_norm], [fiat])
    except Exception as e:
        log.exception("coingecko error")
        await update.message.reply_text(f"❌ Gagal ambil harga: {e}")
        return

    if sym_norm not in data or fiat not in data.get(sym_norm, {}):
        # khusus Pi Network jelaskan alasannya
        if sym.lower() in {"pi", "pinetwork", "pi-network"}:
            await update.message.reply_text(
                "❗ PI belum punya feed harga resmi di CoinGecko. "
                "Harga di P2P/OTC tidak masuk API. Coba coin lain."
            )
            return
        await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan.")
        return

    price = data[sym_norm][fiat]
    chg = data[sym_norm].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {chg:+.2f}%)" if isinstance(chg, (int, float)) else ""
    total = price * amount
    if amount == 1.0:
        await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price, fiat)}{chg_txt}")
    else:
        await update.message.reply_text(
            f"💰 {amount:g} {sym.upper()} = {fmt_price(total, fiat)}\n"
            f"↳ 1 {sym.upper()} = {fmt_price(price, fiat)}{chg_txt}"
        )

# ====== FITUR AIRDROP ======
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: Optional[str] = None
    reward: Optional[str] = None
    ends: Optional[str] = None
    url: Optional[str] = None
    tasks: List[str] = field(default_factory=list)
    source: Optional[str] = None
    scraped_at: float = field(default_factory=time.time)

AIRDROPS: List[Airdrop] = []
AIRDROP_PAGE_SIZE = 10

def _clean_text(x: Optional[str]) -> Optional[str]:
    if not x: return None
    t = " ".join(x.split())
    return t or None

def scrape_airdrops_io() -> List[Airdrop]:
    url = "https://airdrops.io/latest/"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for card in soup.select(".airdrops-list .item"):
        title_el = card.select_one(".title, h3, h2")
        name = _clean_text(title_el.get_text() if title_el else None)
        if not name:
            continue
        href = card.select_one("a")
        reward = _clean_text((card.select_one(".reward, .prize, .subtitle") or {}).get_text() if card.select_one(".reward, .prize, .subtitle") else None)
        chain = _clean_text((card.select_one(".chain, .platform") or {}).get_text() if card.select_one(".chain, .platform") else None)
        url_item = href["href"] if href and href.has_attr("href") else url
        slug = name.lower().strip().replace(" ", "-")
        out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward, url=url_item, source="airdrops.io"))
    return out

def scrape_airdropking() -> List[Airdrop]:
    url = "https://airdropking.io/airdrops/"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: List[Airdrop] = []
    for row in soup.select("article, .airdrop-card, .card"):
        title_el = row.select_one("h2, h3, .title")
        name = _clean_text(title_el.get_text() if title_el else None)
        if not name:
            continue
        href = row.select_one("a")
        reward = _clean_text((row.select_one(".reward, .rewards, .badge") or {}).get_text() if row.select_one(".reward, .rewards, .badge") else None)
        chain = _clean_text((row.select_one(".chain, .network") or {}).get_text() if row.select_one(".chain, .network") else None)
        url_item = href["href"] if href and href.has_attr("href") else url
        slug = name.lower().strip().replace(" ", "-")
        out.append(Airdrop(slug=slug, name=name, chain=chain, reward=reward, url=url_item, source="airdropking.io"))
    return out

def scrape_airdrops_sync() -> List[Airdrop]:
    results: List[Airdrop] = []
    try:
        results.extend(scrape_airdrops_io())
    except Exception as e:
        log.warning(f"scrape_airdrops_io gagal: {e}")
    try:
        results.extend(scrape_airdropking())
    except Exception as e:
        log.warning(f"scrape_airdropking gagal: {e}")
    # Unikkan by slug
    mp: Dict[str, Airdrop] = {}
    for a in results:
        if a.slug not in mp or (a.reward and not mp[a.slug].reward):
            mp[a.slug] = a
    return list(mp.values())

async def run_airdrop_update() -> List[Airdrop]:
    loop = asyncio.get_event_loop()
    data: List[Airdrop] = await loop.run_in_executor(None, scrape_airdrops_sync)
    data.sort(key=lambda x: (x.scraped_at, x.name.lower()), reverse=True)
    global AIRDROPS
    AIRDROPS = data
    return data

def _page_kb(page: int, total: int) -> InlineKeyboardMarkup:
    last = max(0, math.ceil(total / AIRDROP_PAGE_SIZE) - 1)
    prev_p = max(0, page - 1)
    next_p = min(last, page + 1)
    rows = []
    if total > AIRDROP_PAGE_SIZE:
        rows.append([
            InlineKeyboardButton("« Prev", callback_data=f"air:page:{prev_p}"),
            InlineKeyboardButton(f"{page+1}/{last+1}", callback_data="air:noop"),
            InlineKeyboardButton("Next »", callback_data=f"air:page:{next_p}"),
        ])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])

def _render_page(page: int) -> str:
    if not AIRDROPS:
        return "Belum ada data airdrop. Jalankan /airupdate dulu."
    start = page * AIRDROP_PAGE_SIZE
    chunk = AIRDROPS[start:start + AIRDROP_PAGE_SIZE]
    lines = [f"Airdrop terdeteksi (Top {min(len(AIRDROPS), AIRDROP_PAGE_SIZE)} dari {len(AIRDROPS)}):"]
    for i, a in enumerate(chunk, start=1+start):
        meta = []
        if a.chain: meta.append(a.chain)
        if a.reward: meta.append(a.reward)
        meta_txt = f" — {' | '.join(meta)}" if meta else ""
        lines.append(f"{i}. {a.name}{meta_txt}")
    lines.append("\nGunakan /tugas <keyword> untuk lihat detail/tugas.")
    return "\n".join(lines)

def _find_airdrop(keyword: str) -> Optional[Airdrop]:
    s = keyword.lower().strip()
    for a in AIRDROPS:
        if s in a.slug or s in a.name.lower():
            return a
    return None

# ====== HANDLERS KRIPTO & AI ======
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_chat_fiat(chat_id, get_chat_fiat(chat_id))  # init
    msg = (
        "👋 Selamat datang di AirdropCore Bot!\n\n"
        "• Ketik bebas: `btc usd`, `0.25 eth idr`\n"
        "• /price <coin> [fiat] — contoh: `/price btc usdt`\n"
        "• /setfiat idr|usd|eur|usdt\n"
        "• /ask <pertanyaan> — AI (opsional)\n\n"
        "• /airupdate — update daftar airdrop (scraper)\n"
        "• /airdrops — lihat daftar + tombol Next/Prev\n"
        "• /tugas <keyword> — lihat tugas dari airdrop"
    )
    await update.message.reply_markdown(msg)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, ctx)

async def setfiat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text(
            f"FIAT saat ini: {get_chat_fiat(chat_id).upper()}\n"
            "Format: /setfiat idr|usd|eur|usdt"
        )
        return
    fiat = ctx.args[0].lower()
    if fiat not in FIATS:
        await update.message.reply_text("❌ Fiat tidak valid. Pilih: idr/usd/eur/usdt")
        return
    set_chat_fiat(chat_id, fiat)
    await update.message.reply_text(f"✅ FIAT diset ke {fiat.upper()}")

async def price_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]\ncontoh: /price btc usdt")
        return
    sym = ctx.args[0]
    fiat = (ctx.args[1] if len(ctx.args) > 1 else get_chat_fiat(update.effective_chat.id)).lower()
    await reply_price(update, sym, fiat)

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not client:
        await update.message.reply_text("❌ Fitur AI belum aktif (OPENAI_API_KEY kosong/invalid).")
        return
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("Format: /ask <pertanyaan>")
        return
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=400,
            temperature=0.4,
        )
        answer = resp.choices[0].message.content.strip()
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

# ====== HANDLERS AIRDROP ======
async def airupdate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Sedang update daftar airdrops…")
    try:
        data = await run_airdrop_update()
        await update.message.reply_text(
            f"✅ Scraper selesai. Terkumpul {len(data)} airdrop.\nKetik /airdrops untuk melihat daftar."
        )
    except Exception as e:
        log.exception("airupdate")
        await update.message.reply_text(f"❌ Gagal update: {e}")

async def airdrops_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    page = 0
    txt = _render_page(page)
    kb = _page_kb(page, len(AIRDROPS))
    await update.message.reply_text(txt, reply_markup=kb)

async def airdrop_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("air:page:"):
        page = int(data.split(":")[-1])
        page = max(0, page)
        txt = _render_page(page)
        kb = _page_kb(page, len(AIRDROPS))
        try:
            await q.edit_message_text(txt, reply_markup=kb)
        except:
            await q.message.reply_text(txt, reply_markup=kb)

async def tugas_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /tugas <keyword>\ncontoh: /tugas monad")
        return
    q = " ".join(ctx.args).strip()
    a = _find_airdrop(q)
    if not a:
        await update.message.reply_text(f"❌ Tidak ditemukan untuk '{q}'. Jalankan /airupdate dulu lalu coba lagi.")
        return

    detail_tasks: List[str] = []
    if a.url:
        try:
            r = requests.get(a.url, headers=UA, timeout=25)
            r.raise_for_status()
            s = BeautifulSoup(r.text, "html.parser")
            for li in s.select("li"):
                txt = _clean_text(li.get_text(" "))
                if not txt:
                    continue
                low = txt.lower()
                if any(k in low for k in ["follow", "join", "task", "quest", "retweet", "discord",
                                          "telegram", "wallet", "bridge", "trade", "mint",
                                          "galxe", "crew3", "zealy", "x.com", "twitter"]):
                    detail_tasks.append(txt)
            if detail_tasks:
                a.tasks = detail_tasks[:20]
        except Exception as e:
            log.warning(f"fetch tasks fail {a.url}: {e}")

    meta = []
    if a.chain: meta.append(a.chain)
    if a.reward: meta.append(a.reward)
    meta_txt = " | ".join(meta) if meta else "-"

    msg = [
        f"📦 <b>{a.name}</b>",
        f"🔗 Sumber: {a.source or '-'}",
        f"ℹ️ Info: {meta_txt}",
        f"🔗 Link: {a.url or '-'}",
    ]
    if a.tasks:
        msg.append("\n📝 <b>Perkiraan Tugas</b>:")
        for i, t in enumerate(a.tasks[:20], 1):
            msg.append(f"{i}. {t}")
    else:
        msg.append("\n(😅 Belum bisa mengekstrak tugas dari halaman. Buka link di atas untuk detail.)")

    await update.message.reply_html("\n".join(msg))

# ====== ROUTER TEKS BEBAS ======
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) Pola konversi jumlah: "0.25 btc idr"
    m = PAIR_FREE.match(text)
    if m:
        amount = float(m.group(1))
        sym = m.group(2)
        fiat = m.group(3)
        if fiat.lower() not in FIATS:
            fiat = get_chat_fiat(update.effective_chat.id)
        await reply_price(update, sym, fiat, amount=amount)
        return

    # 2) Pola pasangan: "btc usd"
    m2 = PAIR_WORD.match(text)
    if m2:
        sym = m2.group(1)
        fiat = m2.group(2)
        if fiat.lower() not in FIATS:
            fiat = get_chat_fiat(update.effective_chat.id)
        await reply_price(update, sym, fiat)
        return

    # 3) fallback AI (jika aktif)
    if client:
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

    # 4) jika semua gagal
    await update.message.reply_text("Maaf, aku tidak paham. Contoh: `0.5 btc idr` atau `/airdrops`.", parse_mode="Markdown")

# ====== MAIN ======
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN belum di-set di environment.")

    if client:
        log.info("OpenAI client aktif")
    else:
        log.info("OpenAI client nonaktif (lewati fitur /ask & fallback AI)")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("airupdate", airupdate_cmd))
    app.add_handler(CommandHandler("airdrops", airdrops_cmd))
    app.add_handler(CommandHandler("tugas", tugas_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(airdrop_page_cb, pattern=r"^air:"))

    # Free text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

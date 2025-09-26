# bot.py
import os, re, json, logging, requests
from typing import Dict, List, Tuple
from dataclasses import dataclass, field

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters
)

# --------- OpenAI opsional ----------
try:
    from openai import OpenAI
except Exception:  # modul tidak wajib
    OpenAI = None

# ---------- Konfigurasi ----------
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
FIAT_DEFAULT = os.getenv("FIAT_DEFAULT", "usd").lower()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("airdropcore.bot")

client = None
if OPENAI_API_KEY and OpenAI:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI client aktif")
    except Exception as e:
        log.warning("OpenAI init gagal: %s", e)

# ---------- Peta simbol -> id CoinGecko ----------
# Tambah di sini bila perlu.
SYMBOL_MAP: Dict[str, str] = {
    # Layer-1 / besar
    "btc":"bitcoin","eth":"ethereum","bnb":"binancecoin","sol":"solana","ada":"cardano",
    "xrp":"ripple","trx":"tron","dot":"polkadot","avax":"avalanche-2","ltc":"litecoin",
    "xlm":"stellar","near":"near","apt":"aptos","algo":"algorand","atom":"cosmos",
    "icp":"internet-computer","kas":"kaspa","fil":"filecoin","hbar":"hedera-hashgraph",
    "sei":"sei-network","sui":"sui","mina":"mina-protocol","matic":"polygon",
    # stables & majors
    "usdt":"tether","usdc":"usd-coin","dai":"dai","busd":"binance-usd","tusd":"true-usd",
    # meme / populer
    "doge":"dogecoin","shib":"shiba-inu","pepe":"pepe","wif":"dogwifcoin",
    # CEX/DeFi populer
    "uni":"uniswap","cake":"pancakeswap-token","link":"chainlink","aave":"aave",
    "crv":"curve-dao-token","snx":"synthetix-network-token","op":"optimism",
    "arb":"arbitrum","inj":"injective-protocol","rndr":"render-token",
    # tambah cepet:
    "bnbx":"bnbx","ethw":"ethereum-pow-iou","bonk":"bonk","jup":"jupiter-exchange-solana",
}

def norm_symbol(sym: str) -> str:
    s = (sym or "").lower().strip()
    return SYMBOL_MAP.get(s, s)

# ---------- Regex untuk router ----------
AMOUNT_PAIR   = re.compile(r"^\s*([\d\.,]+)\s+([a-z0-9\-]+)\s+([a-z]{2,6})\s*$", re.I)
CONVERT_TEXT  = re.compile(r"^(?:convert|konversi)\s+([\d\.,]+)\s+([a-z0-9\-]+)\s+([a-z]{2,6})$", re.I)
PRICE_TEXT    = re.compile(r"^(?:harga|price)\s+([a-z0-9,/\s]+?)(?:\s+([a-z]{2,6}))?$", re.I)
PAIR_ONLY     = re.compile(r"^\s*([a-z0-9\-]+)[/\s]+([a-z]{2,6})\s*$", re.I)
TICKER_ONLY   = re.compile(r"^[a-z0-9\-]{2,10}$", re.I)

# ---------- HTTP helpers ----------
CG_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"

def fetch_prices(ids: List[str], fiat: str) -> Dict:
    if not ids:
        return {}
    params = {
        "ids": ",".join(ids),
        "vs_currencies": fiat,
        "include_24hr_change": "true",
    }
    try:
        r = requests.get(CG_SIMPLE, params=params, timeout=25)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("fetch_prices error: %s", e)
        return {}

def fmt_price(val, fiat) -> str:
    try:
        return f"{float(val):,.4f} {fiat.upper()}"
    except Exception:
        return f"{val} {fiat.upper()}"

def to_float(s: str) -> float:
    # dukung "1.234,56" atau "1,234.56"
    s2 = s.replace(" ", "")
    if s2.count(",") == 1 and s2.count(".") > 1:
        s2 = s2.replace(".", "").replace(",", ".")
    elif s2.count(",") == 1 and "." not in s2:
        s2 = s2.replace(",", ".")
    else:
        s2 = s2.replace(",", "")
    return float(s2)

# ---------- AIRDROP DATA (contoh statis) ----------
@dataclass
class Airdrop:
    slug: str
    name: str
    chain: str
    reward: str
    link: str
    ends: str
    tasks: List[str] = field(default_factory=list)
    note: str = ""

AIRDROPS: List[Airdrop] = [
    Airdrop(
        slug="zkquest",
        name="ZK Quest",
        chain="zkSync",
        reward="Points → Potential Token",
        link="https://quest.zksync.io/",
        ends="TBA",
        tasks=[
            "Connect wallet & profile",
            "Selesaikan minimal 3 quest mingguan",
            "Join Discord & verifikasi",
            "Simpan bukti (screenshot/tx hash)",
        ],
        note="Program resmi quest; akumulasi poin.",
    ),
    Airdrop(
        slug="fuel",
        name="Fuel Network Beta",
        chain="Fuel",
        reward="Early User Allocation (rumor)",
        link="https://app.fuel.network/",
        ends="TBA",
        tasks=[
            "Gunakan testnet faucet",
            "Coba bridging official",
            "Interact dApp ekosistem",
        ],
    ),
    Airdrop(
        slug="monad",
        name="Monad Early",
        chain="Monad",
        reward="Potential Early User",
        link="https://www.monad.xyz/",
        ends="TBA",
        tasks=[
            "Ikuti test / quest komunitas",
            "Join Discord, ikuti update role",
        ],
    ),
]

# progress per user: {(user_id, slug): set(index task yang selesai)}
AIR_PROGRESS: Dict[Tuple[int, str], set] = {}

# ---------- Command Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💰 Harga", callback_data="menu_price"),
         InlineKeyboardButton("🔁 Convert", callback_data="menu_convert")],
        [InlineKeyboardButton("🎁 Airdrop", callback_data="menu_air"),
         InlineKeyboardButton("🤖 AI", callback_data="menu_ai")],
    ]
    await update.message.reply_text(
        "Selamat datang di **AirdropCore Bot**!\n"
        "Ketik: `harga btc`, `btc usdt`, `0.2 eth idr`, `convert 5 sol usdt`,\n"
        "`/airdrops`, `/air zkquest`.\n"
        "Gunakan tombol di bawah ini.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Perintah utama:\n"
        "• /price <coin> [fiat]\n"
        "• /prices <c1,c2,...> [fiat]\n"
        "• /convert <amount> <coin> <fiat>\n"
        "• /setfiat <idr|usd|usdt|eur>\n"
        "• /airdrops [filter]\n"
        "• /air <slug>\n"
        "• /ask <pertanyaan>\n"
        "Contoh teks tanpa slash juga didukung: `0.1 btc idr`, `harga btc usdt`.",
        parse_mode="Markdown"
    )

async def setfiat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FIAT_DEFAULT
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
    FIAT_DEFAULT = fiat
    await update.message.reply_text(f"✅ FIAT default diset ke {fiat.upper()}")

# ---------- Harga & Convert ----------
async def reply_price(update: Update, sym: str, fiat: str):
    cid = norm_symbol(sym)
    data = fetch_prices([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan.")
        return
    price = data[cid][fiat]
    chg = data[cid].get(f"{fiat}_24h_change")
    chg_txt = f" (24h: {float(chg):+.2f}%)" if isinstance(chg, (int, float)) else ""
    await update.message.reply_text(f"💰 {sym.upper()} = {fmt_price(price, fiat)}{chg_txt}")

async def reply_prices(update: Update, syms: List[str], fiat: str):
    ids = [norm_symbol(s) for s in syms]
    data = fetch_prices(ids, fiat)
    if not data:
        await update.message.reply_text("❌ Tidak ada data.")
        return
    lines = []
    for s in syms:
        cid = norm_symbol(s)
        if cid in data and fiat in data[cid]:
            price = data[cid][fiat]
            chg = data[cid].get(f"{fiat}_24h_change")
            chg_txt = f" ({float(chg):+.2f}%)" if isinstance(chg, (int, float)) else ""
            lines.append(f"• {s.upper():<6} {fmt_price(price, fiat)}{chg_txt}")
        else:
            lines.append(f"• {s.upper():<6} n/a")
    await update.message.reply_text("📊 Harga:\n" + "\n".join(lines))

async def reply_convert(update: Update, amount_s: str, sym: str, fiat: str):
    try:
        amount = to_float(amount_s)
    except Exception:
        await update.message.reply_text("Format angka salah.")
        return
    cid = norm_symbol(sym)
    data = fetch_prices([cid], fiat)
    if not data or cid not in data or fiat not in data[cid]:
        await update.message.reply_text(f"❌ {sym.upper()} atau {fiat.upper()} tidak ditemukan.")
        return
    price = float(data[cid][fiat])
    total = amount * price
    await update.message.reply_text(
        f"🔁 {amount} {sym.upper()} ≈ {fmt_price(total, fiat)}\n"
        f"(1 {sym.upper()} = {fmt_price(price, fiat)})"
    )

# ---------- Airdrop ----------
def find_airdrop(slug: str) -> Airdrop | None:
    s = slug.lower()
    for a in AIRDROPS:
        if a.slug == s or a.slug in s or a.name.lower() == s:
            return a
    return None

async def airdrops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).lower() if ctx.args else ""
    items = [a for a in AIRDROPS if (not q or q in a.chain.lower() or q in a.name.lower() or q in a.slug)]
    if not items:
        await update.message.reply_text("Tidak ada airdrop yang cocok.")
        return
    lines = [f"🎁 **Airdrops** (filter: {q or 'semua'}):"]
    for a in items:
        lines.append(f"• *{a.name}* ({a.chain}) — {a.reward} — `/air {a.slug}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

def _air_kb(a: Airdrop, uid: int) -> InlineKeyboardMarkup:
    prog = AIR_PROGRESS.get((uid, a.slug), set())
    done = f"{len(prog)}/{len(a.tasks)} done" if a.tasks else "No tasks"
    btns = [
        [InlineKeyboardButton("✅ Toggle Task 1", callback_data=f"air:toggle:{a.slug}:0")] if a.tasks else [],
        [InlineKeyboardButton("📋 Semua Task", callback_data=f"air:list:{a.slug}")],
        [InlineKeyboardButton("🌐 Link", url=a.link)],
    ]
    # ratakan dan buang empty
    btns = [row for row in btns if row]
    return InlineKeyboardMarkup(btns)

async def air_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /air <slug>  (contoh: /air zkquest)")
        return
    a = find_airdrop(ctx.args[0])
    if not a:
        await update.message.reply_text("Airdrop tidak ditemukan.")
        return
    uid = update.effective_user.id
    prog = AIR_PROGRESS.get((uid, a.slug), set())
    lines = [
        f"🎁 *{a.name}* — {a.chain}",
        f"Reward: {a.reward}",
        f"Berakhir: {a.ends}",
        f"Link: {a.link}",
        f"Tasks selesai: {len(prog)}/{len(a.tasks)}",
    ]
    if a.note:
        lines.append(f"Catatan: {a.note}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=_air_kb(a, uid))

async def air_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "").split(":")
    if len(data) < 2 or data[0] != "air":
        return
    action = data[1]
    if action == "list" and len(data) >= 3:
        slug = data[2]
        a = find_airdrop(slug)
        if not a:
            await q.edit_message_text("Airdrop tidak ditemukan.")
            return
        lines = [f"📋 Task *{a.name}*:"]
        for i, t in enumerate(a.tasks, 1):
            done = "☑️" if i-1 in AIR_PROGRESS.get((q.from_user.id, a.slug), set()) else "⬜️"
            lines.append(f"{done} {i}. {t}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=_air_kb(a, q.from_user.id))
    elif action == "toggle" and len(data) >= 4:
        slug, idx_s = data[2], data[3]
        a = find_airdrop(slug)
        if not a: 
            await q.edit_message_text("Airdrop tidak ditemukan."); 
            return
        try:
            idx = int(idx_s)
        except: 
            return
        key = (q.from_user.id, a.slug)
        s = AIR_PROGRESS.setdefault(key, set())
        if idx in s: s.remove(idx)
        else: s.add(idx)
        # tampilkan ringkas
        await q.edit_message_text(
            f"✅ Toggle task #{idx+1} untuk *{a.name}*.\n"
            f"Progres: {len(s)}/{len(a.tasks)}\n"
            f"Klik '📋 Semua Task' untuk lihat semua.",
            parse_mode="Markdown",
            reply_markup=_air_kb(a, q.from_user.id)
        )

# ---------- AI Ask ----------
async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /ask <pertanyaan>")
        return
    if not client:
        await update.message.reply_text("❌ OPENAI_API_KEY belum terpasang atau modul tidak tersedia.")
        return
    prompt = " ".join(ctx.args)
    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except Exception:
        pass
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content": prompt}],
            max_tokens=350, temperature=0.4,
        )
        txt = resp.choices[0].message.content.strip()
        await update.message.reply_text(txt)
    except Exception as e:
        log.exception("AI error")
        await update.message.reply_text(f"❌ Error AI: {e}")

# ---------- Router teks bebas ----------
async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) "0.1 btc idr"
    m = AMOUNT_PAIR.match(text)
    if m:
        amt, sym, fiat = m.groups()
        await reply_convert(update, amt, sym, fiat.lower()); return

    # 2) "convert 0.1 btc idr"
    m = CONVERT_TEXT.match(text)
    if m:
        amt, sym, fiat = m.groups()
        await reply_convert(update, amt, sym, fiat.lower()); return

    # 3) "harga btc usdt" / "prices btc,eth idr"
    m = PRICE_TEXT.match(text)
    if m:
        syms_part, fiat_opt = m.groups()
        fiat = (fiat_opt or FIAT_DEFAULT).lower()
        syms_part = syms_part.replace("/", " ").replace("  "," ")
        if "," in syms_part:
            syms = [s.strip() for s in syms_part.split(",") if s.strip()]
            await reply_prices(update, syms, fiat)
        else:
            sym = syms_part.split()[-1]
            await reply_price(update, sym, fiat)
        return

    # 4) "btc/usdt" atau "btc usdt"
    m = PAIR_ONLY.match(text)
    if m:
        sym, fiat = m.groups()
        await reply_price(update, sym, fiat.lower()); return

    # 5) "btc" saja
    if TICKER_ONLY.match(text):
        await reply_price(update, text, FIAT_DEFAULT); return

    # 6) fallback ke AI kalau ada
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content": text}],
                max_tokens=220, temperature=0.6
            )
            answer = resp.choices[0].message.content.strip()
            await update.message.reply_text(answer); return
        except Exception as e:
            log.warning("AI fallback error: %s", e)

    await update.message.reply_text("Maaf, tidak paham. Coba: `harga btc usdt` / `0.1 eth idr` / `/airdrops`.", parse_mode="Markdown")

# ---------- Menu Callback ----------
async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data or ""; await q.answer()
    if data == "menu_price":
        txt = ("Contoh:\n"
               "• harga btc usdt\n"
               "• prices btc,eth idr\n"
               "• btc/usdt")
    elif data == "menu_convert":
        txt = ("Contoh:\n"
               "• 0.1 btc idr\n"
               "• convert 2 sol usdt")
    elif data == "menu_air":
        txt = ("Airdrop:\n"
               "• /airdrops  (daftar)\n"
               "• /airdrops zksync  (filter)\n"
               "• /air zkquest  (detail & task)")
    else:
        txt = "Pakai /ask <pertanyaan> untuk AI."
    await q.edit_message_text(txt)

# ---------- Commands wrapper ----------
async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /price <symbol> [fiat]"); return
    sym = ctx.args[0]; fiat = (ctx.args[1] if len(ctx.args) > 1 else FIAT_DEFAULT).lower()
    await reply_price(update, sym, fiat)

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Format: /prices <c1,c2,...> [fiat]"); return
    syms = [s.strip() for s in ctx.args[0].split(",") if s.strip()]
    fiat = (ctx.args[1] if len(ctx.args)>1 else FIAT_DEFAULT).lower()
    await reply_prices(update, syms, fiat)

async def cmd_convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 3:
        await update.message.reply_text("Format: /convert <amount> <coin> <fiat>"); return
    await reply_convert(update, ctx.args[0], ctx.args[1], ctx.args[2].lower())

# ---------- Main ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setfiat", setfiat))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(CommandHandler("convert", cmd_convert))

    app.add_handler(CommandHandler("airdrops", airdrops))
    app.add_handler(CommandHandler("air", air_detail))
    app.add_handler(CallbackQueryHandler(air_cb, pattern=r"^air:"))
    app.add_handler(CallbackQueryHandler(menu_cb))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot polling started…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

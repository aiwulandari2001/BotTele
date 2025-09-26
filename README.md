# 🤖 Crypto Telegram Bot (Multi-Koin + AI Parser)

Bot Telegram untuk konversi **cryptocurrency ↔ fiat** maupun **crypto ↔ crypto**, mendukung **puluhan koin populer** (BTC, ETH, BNB, SOL, DOGE, SHIB, dll.) dan otomatis mengambil ribuan koin lain dari CoinGecko.

Bot ini juga mendukung **perintah tanpa slash** dan dapat memahami **bahasa natural** seperti:

- `tai 0.1 btc idr`
- `berapa 3 eth usdt`
- `0,25 sol ke rupiah`
- `harga 1 doge idr`
- `tolong hitung 2 bnb ke usd sekarang`

---

## ✨ Fitur Utama
- ✅ Konversi **multi-crypto** (BTC, ETH, BNB, SOL, dll.)
- ✅ Konversi ke **berbagai fiat** (IDR, USD, EUR, JPY, dll.)
- ✅ Auto-resolve ribuan koin lain dari **CoinGecko**
- ✅ Format otomatis untuk **Rupiah (Rp)**
- ✅ Parsing **tanpa slash command**
- ✅ **AI Fallback** (OpenAI GPT) untuk memahami bahasa natural (opsional)
- ✅ Cache harga (30 detik) agar tidak kena rate-limit CoinGecko
- ✅ Mudah dijalankan di VPS / server 24/7 (support `systemd`)

---

## 📦 Instalasi

### 1. Clone Repo
```bash
git clone https://github.com/aiwulandari2001/BotTele.git
cd crypto-bot```bash
TOKEN=$(grep ^BOT_TOKEN .env | cut -d= -f2-)
curl "https://api.telegram.org/bot$TOKEN/deleteWebhook?drop_pending_updates=true"
```

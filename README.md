# AirdropCore SUPER Bot (AI + Crypto)

Fitur besar:
- AI pintar (/ask) dengan prompt khusus crypto
- Harga: /price, /prices, /convert, auto-detect "harga btc", "0.1 btc ke idr"
- Market: /top, /dominance, /fear, /gas (Etherscan opsional)
- Alerts: /alert add|del & /alerts (cek tiap 60s)
- OHLC: /ohlc <sym> <fiat> [days]
- Chart PNG: /chart <sym> <fiat> [days]
- Portfolio: /addport, /delport, /portfolio, /clearport
- News: /news [query] [n] (ringkas pakai AI)
- Inline query: @YourBot "btc usd"
- FIAT per-chat + rate limit

## Setup (Termux/VPS)
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# edit .env jika perlu lalu jalankan:
python3 bot.py
```

Jika sebelumnya memakai webhook, hapus dulu:
```bash
TOKEN=$(grep ^BOT_TOKEN .env | cut -d= -f2-)
curl "https://api.telegram.org/bot$TOKEN/deleteWebhook?drop_pending_updates=true"
```

# AirdropCore SUPER Bot PLUS (AI + Crypto + Airdrop Hunter)

## Fitur
- AI pintar (/ask) — bahasa Indonesia
- Harga: /price, /prices, /convert + auto-detect "harga btc"
- Market: /top, /dominance, /fear, /gas
- OHLC & Chart PNG: /ohlc, /chart
- Alerts: /alert add|del & /alerts (cek setiap 60 detik)
- Portfolio: /addport, /delport, /portfolio, /clearport
- Airdrop Hunter: /airdrops [keyword], /hunt <keyword>
- Inline menu, Markdown rapi, typing indicator

## Cara jalanin (Termux/VPS)
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# jika sebelumnya pakai webhook:
TOKEN=$(grep ^BOT_TOKEN .env | cut -d= -f2-)
curl "https://api.telegram.org/bot$TOKEN/deleteWebhook?drop_pending_updates=true"

python3 bot.py
```

## Catatan Termux
Untuk fitur chart:
```bash
apt install -y freetype libpng
pip install matplotlib
```

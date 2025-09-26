
import os, logging
from typing import Optional

client = None

def init_openai(api_key: Optional[str] = None):
    global client
    key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        logging.info("OpenAI client initialized")
    except Exception as e:
        logging.error("OpenAI init failed: %s", e)
    return client

def chat(prompt: str, model: str = None, temp: float = 0.5, max_tokens: int = 450) -> str:
    if not client:
        return "⚠️ AI nonaktif (OPENAI_API_KEY belum diisi)."
    system = (
        "You are AIRA, an expert crypto & airdrop assistant for Telegram users. "
        "Respond in Indonesian. Singkat, to-the-point, bullet bila cocok. "
        "Jika bahas trading, beri disclaimer singkat: bukan saran finansial."
    )
    try:
        resp = client.chat.completions.create(
            model=model or os.getenv("OPENAI_MODEL","gpt-4o-mini"),
            messages=[
                {"role":"system","content":system},
                {"role":"user","content":prompt}
            ],
            temperature=temp, max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.exception("OpenAI chat error")
        return f"❌ Error AI: {e}"

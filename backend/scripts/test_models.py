import os
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

candidates = [
    'gemini-3.5-flash',
    'gemini-3.5-flash-lite',
    'gemini-3.1-flash-lite',
    'gemini-3.8-flash',
    'gemini-flash-latest',
]

for m in candidates:
    t0 = time.time()
    try:
        res = client.models.generate_content(
            model=m,
            contents='Return JSON {"ok": true}',
            config=types.GenerateContentConfig(
                response_mime_type='application/json',
                http_options=types.HttpOptions(timeout=8000),
            )
        )
        print(f"SUCCESS: {m} in {round(time.time() - t0, 2)}s -> {res.text.strip()}")
    except Exception as e:
        print(f"FAIL: {m} in {round(time.time() - t0, 2)}s -> {type(e).__name__}: {str(e)[:100]}")

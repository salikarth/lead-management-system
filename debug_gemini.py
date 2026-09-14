"""Debug helper: calls the Gemini API the same way app/source_extraction.py
does, but WITHOUT swallowing the exception -- prints exactly what goes
wrong (bad key, wrong model name, network block, unexpected response
shape, etc.) instead of silently falling back.

Run: python debug_gemini.py
(GEMINI_API_KEY must already be set in this terminal session)
"""

import json
import os
import urllib.request
import urllib.error

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-flash-latest"

if not API_KEY:
    print("GEMINI_API_KEY is not set in this terminal session.")
    print("Set it first: $env:GEMINI_API_KEY='your-key'")
    raise SystemExit(1)

print(f"Using API key of length: {len(API_KEY)}")
print(f"Calling model: {MODEL}")
print()

body = json.dumps({
    "contents": [{"parts": [{"text": "Say hello in one word."}]}],
}).encode()

url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
req = urllib.request.Request(
    url,
    data=body,
    headers={
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
    },
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        print("SUCCESS. Raw response:")
        print(raw.decode())
except urllib.error.HTTPError as e:
    print(f"HTTP ERROR {e.code}: {e.reason}")
    print("Response body from Google:")
    print(e.read().decode())
except urllib.error.URLError as e:
    print(f"NETWORK/URL ERROR: {e.reason}")
    print("This usually means a firewall, proxy, or DNS issue is blocking the request.")
except Exception as e:
    print(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")

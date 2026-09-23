#!/usr/bin/env python3
"""
DiagnoseGroq.py

Preliminary experiments to find out why RunLLMInferenceAll.py's Groq calls
fail (e.g. "404 model_not_found"). Makes at most three tiny API requests and
prints everything relevant, without ever printing the full API key:

  1. Environment    -- Python and groq SDK versions, a masked view of
                       GROQ_API_KEY (length, prefix, stray whitespace/quotes),
                       GROQ_BASE_URL (the groq SDK sends requests there
                       instead of api.groq.com when it is set), proxy variables.
  2. GET /models    -- which models this key can actually use, and whether
                       GROQ_MODEL is one of them.
  3. GET /models/<GROQ_MODEL>
                    -- what Groq says about that one model.
  4. POST /chat/completions with GROQ_MODEL and a tiny prompt
                    -- the exact request RunLLMInferenceAll.py makes, reduced
                       to a few tokens, with the full status, error body,
                       request id and rate-limit headers.
  5. If step 4 fails with 404 and step 2 listed other models: the same tiny
     request against ONE of those models, to show whether the key works for
     chat at all (so the problem is the model, not the key).

Uses only the Python standard library (urllib), so it works even if the
groq package itself is the problem, and shows the raw HTTP response.

Usage:
    python3 DiagnoseGroq.py
    GROQ_MODEL=<model id> python3 DiagnoseGroq.py   # check a different model

Reads GROQ_API_KEY, GROQ_MODEL (default: RunLLMInferenceAll.py's default)
and GROQ_BASE_URL (default: https://api.groq.com) from the environment.
"""
from __future__ import annotations

import os
import sys
import json
import platform
import urllib.error
import urllib.request

DEFAULT_MODEL = "llama-3.3-70b-versatile"
MODEL = os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com").rstrip("/")
API_ROOT = f"{BASE_URL}/openai/v1"
TIMEOUT = 60

INTERESTING_HEADERS = [
    "x-request-id",
    "x-groq-region",
    "retry-after",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
]


def section(title: str) -> None:
    print(f"\n{'─' * 60}\n{title}\n{'─' * 60}")


def mask(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def request(method: str, path: str, api_key: str, body: dict | None = None):
    """Returns (status, headers, parsed_json_or_text). status is None when the
    request never got an HTTP response (network/DNS/TLS/proxy error)."""
    url = f"{API_ROOT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "DiagnoseGroq/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, headers, raw = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        status, headers, raw = e.code, e.headers, e.read()
    except Exception as e:  # noqa: BLE001 -- report any transport failure
        return None, {}, f"{type(e).__name__}: {e}"
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, headers, json.loads(text)
    except ValueError:
        return status, headers, text


def print_response(status, headers, payload, show_body: bool = True) -> None:
    if status is None:
        print(f"  No HTTP response: {payload}")
        return
    print(f"  HTTP status : {status}")
    for name in INTERESTING_HEADERS:
        value = headers.get(name) if headers else None
        if value is not None:
            print(f"  {name:31}: {value}")
    if show_body:
        body = json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else payload
        print("  Body        :")
        for line in str(body)[:3000].splitlines():
            print(f"    {line}")


def check_environment() -> str | None:
    section("1. Environment")
    print(f"  Python          : {sys.version.split()[0]} ({platform.platform()})")
    try:
        import groq  # noqa: F401
        print(f"  groq SDK        : {getattr(groq, '__version__', 'unknown version')}")
    except ImportError:
        print("  groq SDK        : NOT INSTALLED (RunLLMInferenceAll.py needs it: pip install groq)")
    print(f"  GROQ_MODEL      : {MODEL}" + ("" if "GROQ_MODEL" in os.environ else "  (default)"))
    print(f"  API root        : {API_ROOT}")
    if "GROQ_BASE_URL" in os.environ:
        print("  !! GROQ_BASE_URL is set -- the groq SDK sends every request there instead of")
        print("     api.groq.com. If that server doesn't serve this model, it answers 404.")
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        if os.environ.get(var):
            print(f"  {var:15} : set (requests go through a proxy)")

    raw_key = os.environ.get("GROQ_API_KEY")
    if raw_key is None:
        print("  GROQ_API_KEY    : NOT SET")
        return None
    key = raw_key.strip().strip('"').strip("'")
    print(f"  GROQ_API_KEY    : {mask(key)}  (length {len(key)})")
    if key != raw_key:
        print("  !! GROQ_API_KEY has surrounding whitespace or quotes; using the trimmed value here,")
        print("     but the groq SDK sends it as-is.")
    if not key.startswith("gsk_"):
        print("  !! GROQ_API_KEY does not start with 'gsk_', the prefix of Groq API keys.")
    return key


def list_models(key: str) -> list[dict] | None:
    section("2. GET /models  (models this key can use)")
    status, headers, payload = request("GET", "/models", key)
    if status != 200 or not isinstance(payload, dict):
        print_response(status, headers, payload)
        if status == 401:
            print("\n  => The key itself is rejected (invalid or revoked).")
        return None
    print_response(status, headers, payload, show_body=False)
    models = sorted(payload.get("data", []), key=lambda m: m.get("id", ""))
    print(f"  {len(models)} model(s):")
    for m in models:
        flags = []
        if m.get("active") is False:
            flags.append("INACTIVE")
        if m.get("context_window"):
            flags.append(f"context {m['context_window']}")
        if m.get("owned_by"):
            flags.append(m["owned_by"])
        marker = "  <== GROQ_MODEL" if m.get("id") == MODEL else ""
        print(f"    - {m.get('id')}  ({', '.join(flags)}){marker}")
    ids = {m.get("id") for m in models}
    if MODEL in ids:
        print(f"\n  => '{MODEL}' IS available to this key.")
    else:
        print(f"\n  => '{MODEL}' is NOT in this key's model list.")
    return models


def get_model(key: str) -> None:
    section(f"3. GET /models/{MODEL}")
    print_response(*request("GET", f"/models/{MODEL}", key))


def chat(key: str, model: str, title: str) -> int | None:
    section(title)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "max_tokens": 5,
        "temperature": 0,
    }
    status, headers, payload = request("POST", "/chat/completions", key, body)
    if status == 200 and isinstance(payload, dict):
        print_response(status, headers, payload, show_body=False)
        try:
            reply = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            reply = payload
        print(f"  Reply       : {reply!r}")
        print(f"  Model used  : {payload.get('model')}")
    else:
        print_response(status, headers, payload)
    return status


def pick_alternative(models: list[dict]) -> str | None:
    candidates = [
        m["id"] for m in models
        if m.get("id") and m.get("id") != MODEL and m.get("active", True)
        and not any(t in m["id"] for t in ("whisper", "tts", "guard", "embed", "orpheus"))
    ]
    for c in candidates:
        if "llama" in c:
            return c
    return candidates[0] if candidates else None


def main() -> None:
    key = check_environment()
    if key is None:
        print("\nSet GROQ_API_KEY first.")
        sys.exit(1)

    models = list_models(key)
    get_model(key)
    status = chat(key, MODEL, f"4. POST /chat/completions with '{MODEL}'")

    alt_status, alt = None, None
    if status == 404 and models:
        alt = pick_alternative(models)
        if alt:
            alt_status = chat(key, alt, f"5. Same tiny request with a listed model: '{alt}'")

    section("Summary")
    if status == 200:
        print(f"  '{MODEL}' works with this key. If RunLLMInferenceAll.py still fails, compare")
        print("  its environment (same shell? same GROQ_API_KEY / GROQ_BASE_URL?) with this one.")
    elif status is None:
        print("  No HTTP response at all -- a network, proxy or TLS problem, not the model.")
    elif status == 401:
        print("  401: the API key is invalid or revoked. Create a new key in the Groq console.")
    elif status == 403:
        print("  403: the key is valid but not allowed to use this model or endpoint")
        print("  (organization/project permissions or account restrictions).")
    elif status == 404:
        if models is not None and MODEL not in {m.get('id') for m in models}:
            print(f"  404: '{MODEL}' is not available to this key -- it is not in the")
            print("  key's model list (retired/renamed, or disabled for this org/project).")
        else:
            print(f"  404 although '{MODEL}' appears in the model list -- the model may be")
            print("  blocked for this key's project, or GROQ_BASE_URL points elsewhere.")
        if alt_status == 200:
            print(f"  The key DOES work for chat with '{alt}', so the key is fine; switch")
            print(f"  models, e.g.:  GROQ_MODEL={alt} python3 RunLLMInferenceAll.py")
    elif status == 429:
        print("  429: rate/usage limit reached -- see the retry-after / x-ratelimit headers above.")
    else:
        print(f"  HTTP {status} -- see the response body above.")


if __name__ == "__main__":
    main()

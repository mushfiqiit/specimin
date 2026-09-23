#!/usr/bin/env python3
"""
DiagnoseGroq.py

Preliminary experiments to find out why RunLLMInferenceAll.py's LLM API calls
fail (e.g. "404 model_not_found"). Despite its name, it works for every
provider in llm_provider.py (LLM_PROVIDER=groq, nvidia, openai-compatible). Makes at most three tiny API requests and
prints everything relevant, without ever printing the full API key:

  1. Environment    -- Python and SDK versions, the provider and API root,
                       a masked view of the API key (length, prefix, stray
                       whitespace/quotes), proxy variables.
  2. GET /models    -- which models this key can actually use, and whether
                       the configured model is one of them.
  3. GET /models/<model>
                    -- what the API says about that one model.
  4. POST /chat/completions with the configured model and a tiny prompt
                    -- the exact request RunLLMInferenceAll.py makes, reduced
                       to a few tokens, with the full status, error body,
                       request id and rate-limit headers.
  5. If step 4 fails with 404 and step 2 listed other models: the same tiny
     request against ONE of those models, to show whether the key works for
     chat at all (so the problem is the model, not the key).

Uses only the Python standard library (urllib), plus certifi's CA bundle when
installed (the groq and openai SDKs depend on it), so it works even if the
SDK itself is the problem, and shows the raw HTTP response.

Usage:
    python3 DiagnoseGroq.py                          # Groq (default)
    LLM_PROVIDER=nvidia python3 DiagnoseGroq.py      # NVIDIA API catalog
    LLM_MODEL=<model id> python3 DiagnoseGroq.py     # check a different model

Reads the same settings as RunLLMInferenceAll.py -- see llm_provider.py.
"""
from __future__ import annotations

import os
import sys
import ssl
import json
import platform
import urllib.error
import urllib.request

import llm_provider

MODEL = llm_provider.MODEL
API_ROOT = llm_provider.api_root()
TIMEOUT = 60


def make_ssl_context() -> tuple[ssl.SSLContext, str]:
    """Verify HTTPS certificates against the same CA bundle the groq/openai
    SDKs use (certifi, via httpx) rather than Python's own default store: on
    macOS, python.org/conda Pythons often have an empty default store, which
    fails every request with CERTIFICATE_VERIFY_FAILED even though the SDKs
    themselves connect fine."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where()), f"certifi ({certifi.where()})"
    except ImportError:
        return ssl.create_default_context(), "Python default store (certifi not installed)"


SSL_CONTEXT, SSL_SOURCE = make_ssl_context()

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
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CONTEXT) as resp:
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
    sdk = "groq" if llm_provider.PROVIDER == "groq" else "openai"
    try:
        module = __import__(sdk)
        print(f"  {sdk + ' SDK':15} : {getattr(module, '__version__', 'unknown version')}")
    except ImportError:
        print(f"  {sdk + ' SDK':15} : NOT INSTALLED (RunLLMInferenceAll.py needs it: "
              f"pip install {sdk})")
    print(f"  Provider        : {llm_provider.PROVIDER} ({llm_provider.LABEL})")
    print(f"  Model           : {MODEL}")
    print(f"  API root        : {API_ROOT}")
    print(f"  CA certificates : {SSL_SOURCE}")
    for var in ("GROQ_BASE_URL", "LLM_BASE_URL"):
        if os.environ.get(var):
            print(f"  !! {var} is set -- requests go there instead of the provider's default")
            print("     endpoint. If that server doesn't serve this model, it answers 404.")
    for error in llm_provider.config_errors():
        print(f"  !! {error}")
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        if os.environ.get(var):
            print(f"  {var:15} : set (requests go through a proxy)")

    key_name = "LLM_API_KEY" if os.environ.get("LLM_API_KEY") else llm_provider.KEY_ENV
    raw_key = llm_provider.api_key_raw()
    if raw_key is None:
        if llm_provider.PROVIDER == "openai-compatible":
            print(f"  {key_name:15} : not set (fine for local servers such as Ollama/vLLM)")
            return "not-needed"
        print(f"  {key_name:15} : NOT SET")
        return None
    key = raw_key.strip().strip('"').strip("'")
    print(f"  {key_name:15} : {mask(key)}  (length {len(key)})")
    if key != raw_key:
        print(f"  !! {key_name} has surrounding whitespace or quotes; using the trimmed value here,")
        print("     but the SDK sends it as-is.")
    prefixes = llm_provider.KEY_PREFIX
    if prefixes and not key.startswith(prefixes):
        print(f"  !! {key_name} does not start with {' or '.join(repr(p) for p in prefixes)}, "
              f"the usual prefix of {llm_provider.LABEL} API keys.")
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
        marker = "  <== configured model" if m.get("id") == MODEL else ""
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
        # Room for reasoning models (e.g. openai/gpt-oss-*), which spend
        # tokens thinking before they answer; 5 tokens gave an empty reply.
        "max_tokens": 512,
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
        usage = payload.get("usage") or {}
        if usage:
            print(f"  Tokens      : {usage.get('prompt_tokens')} prompt + "
                  f"{usage.get('completion_tokens')} completion")
        print(f"  Model used  : {payload.get('model')}")
    else:
        print_response(status, headers, payload)
    return status


def pick_alternative(models: list[dict]) -> str | None:
    candidates = [
        m["id"] for m in models
        if m.get("id") and m.get("id") != MODEL and m.get("active", True)
        and not any(t in m["id"] for t in (
            "whisper", "tts", "guard", "embed", "orpheus", "safety", "reward",
            "vision", "vlm", "clip", "parse", "detector", "translate", "code"))
    ]
    for c in candidates:
        if "llama" in c and "instruct" in c:
            return c
    for c in candidates:
        if "llama" in c:
            return c
    return candidates[0] if candidates else None


def main() -> None:
    key = check_environment()
    if key is None:
        print(f"\nSet {llm_provider.KEY_ENV} first.")
        sys.exit(1)
    if not API_ROOT or not MODEL:
        print("\nFix the settings flagged with !! above first.")
        sys.exit(1)

    models = list_models(key)
    get_model(key)
    status = chat(key, MODEL, f"4. POST /chat/completions with '{MODEL}'")

    alt_status, alt = None, None
    if status in (404, 410) and models:
        alt = pick_alternative(models)
        if alt:
            alt_status = chat(key, alt, f"5. Same tiny request with a listed model: '{alt}'")

    section("Summary")
    if status == 200:
        print(f"  '{MODEL}' works with this key. If RunLLMInferenceAll.py still fails, compare")
        print("  its environment (same shell? same LLM_* / API key variables?) with this one.")
    elif status is None:
        print("  No HTTP response at all -- a network, proxy or TLS problem, not the model.")
        print("  If the error above is CERTIFICATE_VERIFY_FAILED, this script could not verify")
        print("  the server's certificate with the CA bundle shown under 'CA certificates'.")
        print("  Install certifi (pip install certifi) and re-run -- the SDKs use it too.")
    elif status == 401:
        print(f"  401: the API key is invalid or revoked (or not a {llm_provider.LABEL} key).")
        if models is not None:
            print("  (GET /models succeeded, but some providers -- e.g. NVIDIA -- answer it")
            print("  without checking the key, so that does not mean the key is valid.)")
        prefixes = llm_provider.KEY_PREFIX
        if prefixes and not key.startswith(prefixes):
            print(f"  The key does not start with {' or '.join(repr(p) for p in prefixes)} --"
                  " it is probably a different kind of key.")
        print("  Create a new key with the provider.")
    elif status == 403:
        print("  403: the key is valid but not allowed to use this model or endpoint")
        print("  (organization/project permissions or account restrictions).")
    elif status == 404:
        if models is not None and MODEL not in {m.get('id') for m in models}:
            print(f"  404: '{MODEL}' is not available to this key -- it is not in the")
            print("  key's model list (retired/renamed, or disabled for this org/project).")
        else:
            print(f"  404 although '{MODEL}' appears in the model list -- the model may be")
            print("  blocked for this key's project, or a *_BASE_URL variable points elsewhere.")
        if alt_status == 200:
            print(f"  The key DOES work for chat with '{alt}', so the key is fine; switch")
            print(f"  models, e.g.:  LLM_MODEL={alt} python3 RunLLMInferenceAll.py")
    elif status == 410:
        print(f"  410 Gone: '{MODEL}' has been retired by the provider (see 'detail' above")
        print("  for the end-of-life date). Pick a model from the list in step 2.")
        if alt_status == 200:
            print(f"  The key DOES work for chat with '{alt}', so the key is fine; e.g.:")
            print(f"    LLM_MODEL={alt} python3 RunLLMInferenceAll.py")
    elif status == 429:
        print("  429: rate/usage limit reached -- see the retry-after / x-ratelimit headers above.")
    else:
        print(f"  HTTP {status} -- see the response body above.")


if __name__ == "__main__":
    main()

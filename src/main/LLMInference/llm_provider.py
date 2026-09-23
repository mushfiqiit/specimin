"""
llm_provider.py

Which LLM API RunLLMInferenceAll.py and DiagnoseGroq.py talk to, chosen with
the LLM_PROVIDER environment variable:

  groq (default)      Groq. Key: GROQ_API_KEY. Model default:
                      llama-3.3-70b-versatile. Uses the groq SDK.
  nvidia              NVIDIA API catalog (build.nvidia.com), OpenAI-compatible
                      endpoint https://integrate.api.nvidia.com/v1.
                      Key: NVIDIA_API_KEY (an "nvapi-..." key from
                      build.nvidia.com). Model default:
                      meta/llama-3.3-70b-instruct. Uses the openai SDK.
  openai-compatible   Any OpenAI-compatible server: a model you host yourself
                      (vLLM / Ollama, e.g. on an NVIDIA Brev GPU machine or
                      your Mac), Cerebras, Mistral, OpenRouter, ...
                      Requires LLM_BASE_URL (e.g. http://localhost:11434/v1
                      for Ollama) and LLM_MODEL. Key: LLM_API_KEY (optional
                      for local servers). Uses the openai SDK.

Common overrides (any provider):
  LLM_MODEL     model id (GROQ_MODEL is still accepted for groq)
  LLM_API_KEY   API key, instead of the provider's own variable
  LLM_BASE_URL  API root ending in /v1 (groq: GROQ_BASE_URL, without /openai/v1)

Other LLM_* knobs used by RunLLMInferenceAll.py (LLM_MAX_RPM,
LLM_REASONING_EFFORT, LLM_MAX_RETRIES, LLM_MAX_RETRY_WAIT) also accept their
older GROQ_* names.
"""
from __future__ import annotations

import os
import sys

PRESETS = {
    "groq": {
        "label": "Groq",
        "key_env": "GROQ_API_KEY",
        "key_prefix": "gsk_",
        "default_model": "llama-3.3-70b-versatile",
    },
    "nvidia": {
        "label": "NVIDIA API catalog",
        "key_env": "NVIDIA_API_KEY",
        "key_prefix": "nvapi-",
        "default_model": "meta/llama-3.3-70b-instruct",
        "base_url": "https://integrate.api.nvidia.com/v1",
    },
    "openai-compatible": {
        "label": "OpenAI-compatible server",
        "key_env": "LLM_API_KEY",
        "key_prefix": None,
        "default_model": None,
        "base_url": None,
    },
}


def setting(name: str, default: str | None = None) -> str | None:
    """LLM_<name>, falling back to the older GROQ_<name>, then default."""
    for var in (f"LLM_{name}", f"GROQ_{name}"):
        value = os.environ.get(var)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


PROVIDER = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
if PROVIDER not in PRESETS:
    print(f"ERROR: LLM_PROVIDER={PROVIDER!r} is not one of: {', '.join(PRESETS)}.")
    sys.exit(1)
PRESET = PRESETS[PROVIDER]
LABEL = PRESET["label"]
KEY_ENV = PRESET["key_env"]
KEY_PREFIX = PRESET["key_prefix"]

MODEL = (os.environ.get("LLM_MODEL") or "").strip() or (
    (os.environ.get("GROQ_MODEL") or "").strip() if PROVIDER == "groq" else ""
) or PRESET["default_model"]


def api_root() -> str | None:
    """Base URL of the OpenAI-style API (the part before /chat/completions)."""
    if PROVIDER == "groq":
        base = os.environ.get("GROQ_BASE_URL", "https://api.groq.com").rstrip("/")
        return f"{base}/openai/v1"
    base = (os.environ.get("LLM_BASE_URL") or "").strip() or PRESET["base_url"]
    return base.rstrip("/") if base else None


def api_key_raw() -> str | None:
    """The key exactly as set in the environment (LLM_API_KEY wins)."""
    return os.environ.get("LLM_API_KEY") or os.environ.get(KEY_ENV)


def config_errors() -> list[str]:
    errors = []
    if not MODEL:
        errors.append("LLM_MODEL is not set (required for LLM_PROVIDER=openai-compatible).")
    if not api_root():
        errors.append("LLM_BASE_URL is not set (required for LLM_PROVIDER=openai-compatible),"
                      " e.g. http://localhost:11434/v1 for Ollama.")
    if PROVIDER != "openai-compatible" and not api_key_raw():
        errors.append(f"{KEY_ENV} is not set.")
    return errors


def describe() -> str:
    return f"{LABEL} ({api_root()}), model {MODEL}"


def make_client():
    """An SDK client with the SDK's own retries disabled (callers retry
    themselves, with logging). groq SDK for Groq, openai SDK otherwise."""
    errors = config_errors()
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        sys.exit(1)
    if PROVIDER == "groq":
        from groq import Groq
        return Groq(api_key=api_key_raw(), max_retries=0)
    try:
        from openai import OpenAI
    except ImportError:
        print(f"ERROR: LLM_PROVIDER={PROVIDER} needs the openai package: pip install openai")
        sys.exit(1)
    # Local servers (Ollama, vLLM) accept any key; the SDK just needs one.
    return OpenAI(api_key=api_key_raw() or "not-needed", base_url=api_root(), max_retries=0)

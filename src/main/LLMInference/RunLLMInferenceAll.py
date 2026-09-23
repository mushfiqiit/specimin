#!/usr/bin/env python3
"""
RunLLMInferenceAll.py

For every subdirectory in SPECIMIN_OUT (skipping *LLMInferenced folders):
  1. Collects all .java files
  2. Reads root-warning.txt -- the ONE warning from the slice's
     nullaway-warnings.txt that reproduces the original warning the slice
     was generated for (written by ExtractRootWarning.py). Slices without
     one (the original warning was not reproduced) are skipped.
  3. Sends prompt to the configured LLM (see llm_provider.py) to infer the @Nullable/@Nonnull
     annotations that fix THAT warning only
  4. Saves null-inference-report.txt inside the source folder
  5. Parses Section C and reconstructs a <folderName>LLMInferenced/ sibling directory

Usage:
    python3 RunLLMInferenceAll.py            # run all
    python3 RunLLMInferenceAll.py --dry-run  # print prompts only, no API calls
    LLM_MODEL=<model id> python3 RunLLMInferenceAll.py    # use another model
    LLM_PROVIDER=nvidia python3 RunLLMInferenceAll.py     # NVIDIA API catalog

Provider/model/key settings: see llm_provider.py (LLM_PROVIDER, LLM_MODEL,
LLM_API_KEY, LLM_BASE_URL; Groq is the default). Other knobs: LLM_MAX_RPM,
LLM_REASONING_EFFORT, LLM_MAX_RETRIES, LLM_MAX_RETRY_WAIT (older GROQ_* names
still work) and SPECIMIN_OUT -- see their comments below.

If API calls fail, run DiagnoseGroq.py (works for every provider) first.

SPECIMIN_OUT can be overridden with the environment variable of the same name
(default: the JUnit 4 slices written by
SpeciminPerformanceEvaluation/RunSpeciminAll.py, checked by RunCheckerAll.sh, with
root-warning.txt written by ExtractRootWarning.py).
"""
from __future__ import annotations

import os
import re
import sys
import time
import shutil
import pathlib
from AddNonnullImport import ensure_imports
import llm_provider
from llm_provider import MODEL, setting

# ── Paths ──────────────────────────────────────────────────────────────────────
SPECIMIN_OUT = pathlib.Path(os.environ.get(
    "SPECIMIN_OUT",
    "/Users/mushfiqurrahmanchowdhury/Documents/junit4/speciminout",
)).expanduser()

# ── Model ──────────────────────────────────────────────────────────────────────
# Provider, model and key come from llm_provider.py (LLM_PROVIDER, LLM_MODEL,
# ...). Run DiagnoseGroq.py to see which models your key can use.

# HTTP statuses that will fail identically for every remaining slice (bad key,
# no access, unknown model), so the run stops at the first one instead of
# repeating the same failing request for every folder.
FATAL_STATUSES = {401: "API key rejected", 403: "access denied",
                  404: "model not found / no access",
                  410: "model retired (end of life)"}

# Optional: "low", "medium" or "high" for reasoning models such as
# openai/gpt-oss-120b. Lower effort uses fewer tokens per slice, so more slices
# fit in the per-minute token limit. Unset = the model's default (sent only
# when set, because non-reasoning models reject the parameter).
REASONING_EFFORT = setting("REASONING_EFFORT", "")

# ── Retries ────────────────────────────────────────────────────────────────────
# APIs answer 429 when a per-minute request or TOKEN limit is hit (e.g. 8,000
# tokens/min for openai/gpt-oss-120b on Groq's free tier -- only ~2 slices/min),
# and 5xx on transient server errors. Such a slice is retried after the wait
# the API asks for (retry-after header, x-ratelimit-reset-* headers, or "try
# again in Xs" in the message), up to LLM_MAX_RETRIES times. A requested wait
# longer than LLM_MAX_RETRY_WAIT seconds (e.g. a daily quota is used up)
# stops the run instead of waiting it out.
MAX_RETRIES = int(setting("MAX_RETRIES", "8"))
MAX_RETRY_WAIT = float(setting("MAX_RETRY_WAIT", "300"))
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# ── Rate limiting ──────────────────────────────────────────────────────────────
# Groq's models allow 30 requests per minute. Requests are spaced so their
# START times are at least 60 / MAX_REQUESTS_PER_MINUTE seconds apart, which
# keeps any 60-second window at or below MAX_REQUESTS_PER_MINUTE requests.
# The default, 25, leaves a margin under the 30 RPM limit (2.4 s between
# requests). Override with LLM_MAX_RPM (or GROQ_MAX_RPM).
MAX_REQUESTS_PER_MINUTE = float(setting("MAX_RPM", "25"))
if MAX_REQUESTS_PER_MINUTE <= 0:
    print("ERROR: LLM_MAX_RPM must be a positive number.")
    sys.exit(1)
MIN_REQUEST_INTERVAL = 60.0 / MAX_REQUESTS_PER_MINUTE


class RequestThrottle:
    """Blocks until at least `interval` seconds have passed since the
    previous request started."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.last_start: float | None = None

    def wait(self) -> None:
        if self.last_start is not None:
            remaining = self.interval - (time.monotonic() - self.last_start)
            if remaining > 0:
                print(f"    Waiting {remaining:.1f}s (rate limit: "
                      f"{MAX_REQUESTS_PER_MINUTE:g} requests/min)")
                time.sleep(remaining)
        self.last_start = time.monotonic()

# ── API client ─────────────────────────────────────────────────────────────────

def make_client():
    # llm_provider disables the SDK's own silent 429/5xx retries;
    # call_llm_with_retries() retries instead, logging each wait.
    return llm_provider.make_client()


# ── Java file collection ───────────────────────────────────────────────────────

def collect_java_files(folder: pathlib.Path) -> dict[str, str]:
    files = {}
    for p in sorted(folder.rglob("*.java")):
        relative = p.relative_to(folder)
        files[str(relative)] = p.read_text(encoding="utf-8")
    return files


def read_usage_context(folder: pathlib.Path) -> str:
    """Read usage-context.txt, or '' if absent/empty. (The JUnit 4 pipeline's
    RunSpeciminAll.py does not write one, so this is normally ''.)"""
    ctx_file = folder / "usage-context.txt"
    if not ctx_file.exists():
        return ""
    return ctx_file.read_text(encoding="utf-8").strip()


def read_root_warning(folder: pathlib.Path) -> str | None:
    """
    Read root-warning.txt (written by ExtractRootWarning.py): the single
    warning in the slice's nullaway-warnings.txt that reproduces the one the
    slice was generated for. Returns None if absent or empty -- the original
    warning was not reproduced in this slice, or ExtractRootWarning.py has
    not been run.
    """
    warning_file = folder / "root-warning.txt"
    if not warning_file.exists():
        return None
    content = warning_file.read_text(encoding="utf-8").strip()
    return content or None


# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_prompt(java_files: dict[str, str], root_warning: str,
                 usage_context: str = "") -> str:
    sources = "\n\n".join(
        f"// === {name} ===\n{src}" for name, src in java_files.items()
    )
    usage_section = ""
    if usage_context:
        usage_section = f"""--- FIELD USAGE CONTEXT (read-only evidence; do NOT annotate these excerpts) ---

These excerpts come from the ORIGINAL program (outside the reduced slice). They show
how the relevant fields are dereferenced, guarded, and assigned elsewhere, which the
reduced slice omits. Use them as evidence when deciding annotations.

{usage_context}

--- END OF FIELD USAGE CONTEXT ---

"""
    return f"""You are a Java null-safety expert working with NullAway and JSpecify annotations.

The Java code below is a Specimin-reduced minimal reproduction of a method or field
from JUnit 4 for which NullAway reported the warning below. JUnit 4 has no
nullness annotations, so its nullability was previously unverified.

Your goal is to fix THIS ONE warning. The reduced code may produce other NullAway
warnings (for example, from stubs Specimin generated); ignore them and do not change
code only to address them.

--- NULLAWAY WARNING TO FIX ---

{root_warning}

--- END OF NULLAWAY WARNING ---

{usage_section}--- REDUCED SOURCE CODE ---

{sources}

--- END OF SOURCE CODE ---

Your task:

A) Annotation decisions
Decide which parameters, return types, and fields involved in the warning above
should be @Nullable or @Nonnull (unannotated/non-null by default under NullAway)
so that this warning is resolved. An expression that can really be null should be
@Nullable; everything that is guaranteed non-null should be @Nonnull or left
unannotated. Do not annotate locations unrelated to this warning.

Present your decisions as a table:
  location | inferred annotation | one-sentence reason

Use javax.annotation.Nullable and javax.annotation.Nonnull (jsr305).

B) Cause of the warning
Explain in a few sentences what causes the warning above (the expression and line
it points to, and why NullAway considers it unsafe), and how your annotations fix it.

C) Corrected annotated code
Produce the fully annotated version of each file. Rules:
  - Add @Nullable or @Nonnull annotations only — do NOT add if-statement null checks.
  - Only add/change the annotations needed to fix the warning above.
  - Use javax.annotation.Nullable and javax.annotation.Nonnull imports (jsr305).
  - Preserve all existing logic exactly; only add/change annotations.
  - Output each file preceded by its marker line: // === path/to/File.java ===
  - Wrap the entire output in a single ```java ... ``` code fence.
"""


# ── LLM API call ───────────────────────────────────────────────────────────────

def call_llm(client, prompt: str) -> str:
    extra = {"reasoning_effort": REASONING_EFFORT} if REASONING_EFFORT else {}
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        **extra,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("model returned an empty reply "
                           f"(finish_reason={response.choices[0].finish_reason!r})")
    usage = getattr(response, "usage", None)
    if usage is not None:
        print(f"    Tokens     : {getattr(usage, 'prompt_tokens', '?')} prompt + "
              f"{getattr(usage, 'completion_tokens', '?')} completion = "
              f"{getattr(usage, 'total_tokens', '?')}")
    return content


_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_TRY_AGAIN_RE = re.compile(r"try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.IGNORECASE)


def parse_duration(text: str) -> float | None:
    """Seconds in a rate-limit duration like "7.66s", "622ms", "1m26.4s" or a bare
    number of seconds ("12"); None if unparseable."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = _DURATION_PART_RE.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(n) * scale[u] for n, u in parts)


def retry_wait_seconds(e: Exception, attempt: int) -> float:
    """How long to wait before retrying a failed call: what the API asked for,
    plus a 1 s margin, or exponential backoff if it said nothing."""
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    candidates = []
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        value = headers.get(name)
        seconds = parse_duration(value) if value else None
        if seconds is not None:
            candidates.append(seconds)
            if name == "retry-after":
                break  # authoritative when present
    if not candidates:
        m = _TRY_AGAIN_RE.search(str(e))
        seconds = parse_duration(m.group(1)) if m else None
        if seconds is not None:
            candidates.append(seconds)
    if candidates:
        return candidates[0] + 1.0
    return min(5.0 * 2 ** attempt, 120.0)


class StopRun(Exception):
    """Raised when continuing the run is pointless (e.g. a daily quota)."""


def call_llm_with_retries(client, prompt: str, throttle: "RequestThrottle") -> str:
    attempt = 0
    while True:
        # Throttle EVERY request, including retries and ones whose predecessor
        # failed, so a run of errors can't burst past the limit.
        throttle.wait()
        try:
            return call_llm(client, prompt)
        except Exception as e:  # noqa: BLE001 -- inspect whatever the SDK raised
            status = getattr(e, "status_code", None)
            if status not in RETRYABLE_STATUSES or attempt >= MAX_RETRIES:
                raise
            wait = retry_wait_seconds(e, attempt)
            if wait > MAX_RETRY_WAIT:
                raise StopRun(f"The API asked to wait {wait:.0f}s (> LLM_MAX_RETRY_WAIT="
                              f"{MAX_RETRY_WAIT:g}s) -- probably a daily quota. "
                              f"Last error: {e}") from e
            attempt += 1
            print(f"    [RETRY {attempt}/{MAX_RETRIES}] HTTP {status} — waiting "
                  f"{wait:.1f}s before retrying ({str(e)[:160]})")
            time.sleep(wait)


def describe_error(e: Exception) -> tuple[int | None, list[str]]:
    """Returns (http_status_or_None, log lines) for a failed API call: the
    exception type, HTTP status, the request id (quote it to provider support)
    and any rate-limit headers, plus the error message."""
    status = getattr(e, "status_code", None)
    lines = [f"type        : {type(e).__module__}.{type(e).__name__}"]
    if status is not None:
        lines.append(f"HTTP status : {status}")
    response = getattr(e, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name in ("x-request-id", "retry-after", "x-ratelimit-remaining-requests",
                     "x-ratelimit-reset-requests", "x-ratelimit-remaining-tokens",
                     "x-ratelimit-reset-tokens"):
            value = headers.get(name)
            if value is not None:
                lines.append(f"{name:12}: {value}")
    request = getattr(response, "request", None) or getattr(e, "request", None)
    url = getattr(request, "url", None)
    if url is not None:
        lines.append(f"URL         : {url}")
    lines.append(f"message     : {e}")
    return status, lines


def check_model_available(client) -> None:
    """Before any slice is processed, ask the API which models this key can
    use and stop with a clear message if MODEL is not one of them."""
    print(f"Checking that model '{MODEL}' is available to this API key ...")
    try:
        listed = client.models.list()
    except Exception as e:  # noqa: BLE001 -- report whatever the SDK raised
        status, lines = describe_error(e)
        print("    [ERROR] Could not list models:")
        for line in lines:
            print(f"      {line}")
        if status in FATAL_STATUSES:
            print("    Run DiagnoseGroq.py for details.")
            sys.exit(1)
        print("    Continuing without the model check.")
        return
    ids = sorted(m.id for m in getattr(listed, "data", []) if getattr(m, "id", None))
    if MODEL in ids:
        print(f"    OK — '{MODEL}' is available.")
        return
    print(f"    [ERROR] '{MODEL}' is not available to this API key.")
    print(f"    Models this key can use ({len(ids)}):")
    for model_id in ids:
        print(f"      - {model_id}")
    print("    Pick one and re-run, e.g.:  LLM_MODEL=<model id> python3 RunLLMInferenceAll.py")
    print("    (DiagnoseGroq.py shows more detail.)")
    sys.exit(1)


# ── Section C parser / file reconstructor ─────────────────────────────────────

FILE_MARKER = re.compile(r"^// === (.+?) ===$", re.MULTILINE)


def extract_section_c(report: str) -> str:
    match = re.search(
        r"(?:##\s*C\)|###\s*Corrected\s+Annotated\s+Code).*?```java\s*(.*?)```",
        report, re.DOTALL | re.IGNORECASE
    )
    if not match:
        raise ValueError("Could not find Section C corrected code block in the report.")
    return match.group(1)


def split_into_files(code_block: str) -> dict[str, str]:
    markers = list(FILE_MARKER.finditer(code_block))
    if not markers:
        raise ValueError("No '// === FileName.java ===' markers found in Section C.")

    files = {}
    for i, marker in enumerate(markers):
        filename = marker.group(1).strip()
        content_start = marker.end()
        content_end = markers[i + 1].start() if i + 1 < len(markers) else len(code_block)
        content = code_block[content_start:content_end].strip()
        files[filename] = content
    return files


def reconstruct(source_folder: pathlib.Path, report: str) -> None:
    output_dir = source_folder.parent / (source_folder.name + "LLMInferenced")

    try:
        code_block = extract_section_c(report)
        patched_files = split_into_files(code_block)
    except ValueError as e:
        print(f"    [WARN] Could not reconstruct: {e}")
        return

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for relative_path, content in patched_files.items():
        dest = output_dir / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content + "\n", encoding="utf-8")
        dest.write_text(ensure_imports(content) + "\n", encoding="utf-8")
        print(f"    [patched]  {relative_path}")

    patched_set = set(patched_files.keys())
    for src_file in sorted(source_folder.rglob("*.java")):
        relative = str(src_file.relative_to(source_folder))
        if relative not in patched_set:
            dest = output_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)
            print(f"    [copied]   {relative}")

    print(f"    Reconstructed → {output_dir.name}/")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if not SPECIMIN_OUT.exists():
        print(f"ERROR: specimin-out directory not found: {SPECIMIN_OUT}")
        sys.exit(1)

    client = None if dry_run else make_client()

    folders = sorted(
        d for d in SPECIMIN_OUT.iterdir()
        if d.is_dir() and not d.name.endswith("LLMInferenced")
    )

    if not folders:
        print("No eligible folders found in specimin-out.")
        sys.exit(0)

    print(f"Found {len(folders)} folder(s) to process.")
    if dry_run:
        print("(dry-run mode — the LLM API will not be called)\n")

    successes, failures, skipped = 0, [], []
    throttle = RequestThrottle(MIN_REQUEST_INTERVAL)
    if not dry_run:
        print(f"LLM: {llm_provider.describe()}"
              + (f"  (reasoning_effort={REASONING_EFFORT})" if REASONING_EFFORT else ""))
        throttle.wait()  # the model check is a request too
        check_model_available(client)
        print(f"Rate limit: at most {MAX_REQUESTS_PER_MINUTE:g} requests/min "
              f"({MIN_REQUEST_INTERVAL:.1f}s between requests)")

    for i, folder in enumerate(folders, start=1):
        print(f"\n{'─' * 60}")
        print(f"[{i:02d}/{len(folders)}] {folder.name}")

        java_files = collect_java_files(folder)
        if not java_files:
            print("    [SKIP] No .java files found.")
            continue

        print(f"    Java files : {len(java_files)}")

        root_warning = read_root_warning(folder)
        if root_warning is None:
            print("    [SKIP] No root-warning.txt — original warning not reproduced in this")
            print("           slice, or ExtractRootWarning.py has not been run.")
            skipped.append(folder.name)
            continue
        print(f"    Warning    : {root_warning}")

        usage_context = read_usage_context(folder)
        if usage_context:
            print(f"    Usage ctx  : {len(usage_context.splitlines())} line(s)")

        prompt = build_prompt(java_files, root_warning, usage_context)
        print(f"    Prompt     : {len(prompt):,} chars → {llm_provider.LABEL} {MODEL}")

        if dry_run:
            print("    [dry-run — skipped]")
            continue

        try:
            result = call_llm_with_retries(client, prompt, throttle)
        except StopRun as e:
            print(f"    [ERROR] {e}")
            print("\n    Stopping the run. Slices finished so far keep their")
            print("    null-inference-report.txt; note that a re-run processes every slice again.")
            failures.append(folder.name)
            break
        except Exception as e:  # noqa: BLE001 -- log whatever the SDK raised
            status, lines = describe_error(e)
            print("    [ERROR] LLM call failed:")
            for line in lines:
                print(f"      {line}")
            failures.append(folder.name)
            if status in FATAL_STATUSES:
                print(f"\n    Stopping: HTTP {status} ({FATAL_STATUSES[status]}) would fail the same")
                print("    way for every remaining slice. Run DiagnoseGroq.py to investigate.")
                break
            continue

        report_path = folder / "null-inference-report.txt"
        report_path.write_text(result, encoding="utf-8")
        print(f"    Report saved → null-inference-report.txt")

        print("    Reconstructing LLMInferenced directory...")
        reconstruct(folder, result)

        successes += 1

    print(f"\n{'═' * 60}")
    print(f"Summary: {successes}/{len(folders)} folder(s) succeeded.")
    if skipped:
        print(f"Skipped (no root-warning.txt): {len(skipped)}")
        for name in skipped:
            print(f"  - {name}")
    if failures:
        print("Failed:")
        for name in failures:
            print(f"  - {name}")

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()

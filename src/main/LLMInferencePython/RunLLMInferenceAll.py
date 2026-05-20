#!/usr/bin/env python3
"""
RunLLMInferenceAll.py

For every subdirectory in SPECIMIN_OUT (skipping *LLMInferenced folders):
  1. Collects all .java files
  2. Reads nullaway-warnings.txt
  3. Sends prompt to Groq llama-3.3-70b to infer @Nullable/@Nonnull annotations
  4. Saves null-inference-report.txt inside the source folder
  5. Parses Section C and reconstructs a <folderName>LLMInferenced/ sibling directory

Usage:
    python3 RunLLMInferenceAll.py            # run all
    python3 RunLLMInferenceAll.py --dry-run  # print prompts only, no API calls
"""
from __future__ import annotations

import os
import re
import sys
import time
import shutil
import pathlib
from AddNonnullImport import ensure_imports
from groq import Groq

# ── Paths ──────────────────────────────────────────────────────────────────────
SPECIMIN_OUT = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out"
)

# Seconds to wait between Groq requests (free tier: ~30 req/min)
RATE_LIMIT_DELAY = 3

# ── Groq client ────────────────────────────────────────────────────────────────

def make_client() -> Groq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY environment variable is not set.")
        sys.exit(1)
    return Groq(api_key=api_key)


# ── Java file collection ───────────────────────────────────────────────────────

def collect_java_files(folder: pathlib.Path) -> dict[str, str]:
    files = {}
    for p in sorted(folder.rglob("*.java")):
        relative = p.relative_to(folder)
        files[str(relative)] = p.read_text(encoding="utf-8")
    return files


def read_nullaway_warnings(folder: pathlib.Path) -> str:
    warnings_file = folder / "nullaway-warnings.txt"
    if not warnings_file.exists():
        return "(nullaway-warnings.txt not found — run RunNullAwayAll.sh first)"
    content = warnings_file.read_text(encoding="utf-8").strip()
    if not content:
        return "(nullaway-warnings.txt is empty — no NullAway warnings detected)"
    return content


# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_prompt(java_files: dict[str, str], nullaway_warnings: str) -> str:
    sources = "\n\n".join(
        f"// === {name} ===\n{src}" for name, src in java_files.items()
    )
    return f"""You are a Java null-safety expert working with NullAway and JSpecify annotations.

The Java code below is a Specimin-reduced minimal reproduction of a method originally
annotated with @NullUnmarked, meaning its nullability was previously unverified.
The @NullUnmarked annotation has been removed so that NullAway now checks it.

--- NULLAWAY WARNINGS ---

{nullaway_warnings}

--- END OF NULLAWAY WARNINGS ---

--- REDUCED SOURCE CODE ---

{sources}

--- END OF SOURCE CODE ---

Your task:

A) Annotation decisions
For every parameter, return type, and field in the code, decide between @Nullable
and @Nonnull (unannotated/non-null by default under NullAway). Use the NullAway
warnings to guide decisions — an expression flagged by NullAway as potentially null
should be @Nullable; everything that is guaranteed non-null should be @Nonnull or
left unannotated.

Present your decisions as a table:
  location | inferred annotation | one-sentence reason

Use javax.annotation.Nullable and javax.annotation.Nonnull (jsr305).

B) Unsafe dereferences
List every dereference of a @Nullable value in the code that could throw a
NullPointerException, cross-referencing the NullAway warnings by line number.

C) Corrected annotated code
Produce the fully annotated version of each file. Rules:
  - Add @Nullable or @Nonnull annotations only — do NOT add if-statement null checks.
  - Use javax.annotation.Nullable and javax.annotation.Nonnull imports (jsr305).
  - Preserve all existing logic exactly; only add/change annotations.
  - Output each file preceded by its marker line: // === path/to/File.java ===
  - Wrap the entire output in a single ```java ... ``` code fence.
"""


# ── Groq API call ──────────────────────────────────────────────────────────────

def call_groq(client: Groq, prompt: str) -> str:
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


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
        print("(dry-run mode — Groq API will not be called)\n")

    successes, failures = 0, []

    for i, folder in enumerate(folders, start=1):
        print(f"\n{'─' * 60}")
        print(f"[{i:02d}/{len(folders)}] {folder.name}")

        java_files = collect_java_files(folder)
        if not java_files:
            print("    [SKIP] No .java files found.")
            continue

        print(f"    Java files : {len(java_files)}")

        warnings = read_nullaway_warnings(folder)
        print(f"    Warnings   : {len(warnings.splitlines())} line(s)")

        prompt = build_prompt(java_files, warnings)
        print(f"    Prompt     : {len(prompt):,} chars → Groq llama-3.3-70b-versatile")

        if dry_run:
            print("    [dry-run — skipped]")
            continue

        try:
            result = call_groq(client, prompt)
        except Exception as e:
            print(f"    [ERROR] Groq call failed: {e}")
            failures.append(folder.name)
            continue

        report_path = folder / "null-inference-report.txt"
        report_path.write_text(result, encoding="utf-8")
        print(f"    Report saved → null-inference-report.txt")

        print("    Reconstructing LLMInferenced directory...")
        reconstruct(folder, result)

        successes += 1

        if i < len(folders):
            time.sleep(RATE_LIMIT_DELAY)

    print(f"\n{'═' * 60}")
    print(f"Summary: {successes}/{len(folders)} folder(s) succeeded.")
    if failures:
        print("Failed:")
        for name in failures:
            print(f"  - {name}")

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()

import pathlib
import re
import shutil

REPORT_PATH = pathlib.Path.home() / "Documents/specimin-out/null-inference-report.txt"
SOURCE_DIR  = pathlib.Path.home() / "Documents/specimin-out/invokeSubscriber"
OUTPUT_DIR  = pathlib.Path.home() / "Documents/specimin-out/invokeSubscriberLLMInferenced"

FILE_MARKER = re.compile(r"^// === (.+?) ===$", re.MULTILINE)

def extract_section_c(report: str) -> str:
    match = re.search(r"##\s*C\).*?```java\s*(.*?)```", report, re.DOTALL | re.IGNORECASE)
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

def copy_unmodified_files(source: pathlib.Path, output: pathlib.Path, patched_paths: set[str]):
    for src_file in source.rglob("*.java"):
        relative = src_file.relative_to(source)
        if str(relative) not in patched_paths:
            dest = output / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)
            print(f"  [copied unchanged] {relative}")

def main():
    if not REPORT_PATH.exists():
        print(f"Report not found: {REPORT_PATH}")
        return
    if not SOURCE_DIR.exists():
        print(f"Source directory not found: {SOURCE_DIR}")
        return

    report = REPORT_PATH.read_text(encoding="utf-8")

    print("Extracting Section C corrected code...")
    code_block = extract_section_c(report)

    print("Splitting into individual files...")
    patched_files = split_into_files(code_block)
    print(f"  Found {len(patched_files)} patched file(s) in report:")
    for name in patched_files:
        print(f"    {name}")

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
        print(f"\nCleared existing output directory: {OUTPUT_DIR}")
    OUTPUT_DIR.mkdir(parents=True)

    print("\nWriting patched files...")
    for relative_path, content in patched_files.items():
        dest = OUTPUT_DIR / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content + "\n", encoding="utf-8")
        print(f"  [patched]  {relative_path}")

    print("\nCopying unmodified files from source...")
    copy_unmodified_files(SOURCE_DIR, OUTPUT_DIR, set(patched_files.keys()))

    print(f"\nDone. Reconstructed repository written to:\n  {OUTPUT_DIR}")
    print("\nFinal file tree:")
    for f in sorted(OUTPUT_DIR.rglob("*.java")):
        print(f"  {f.relative_to(OUTPUT_DIR)}")

if __name__ == "__main__":
    main()

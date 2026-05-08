import os
import pathlib
from groq import Groq

SPECIMIN_OUT = pathlib.Path.home() / "Documents/specimin-out/invokeSubscriber"
NULLAWAY_REPORT = pathlib.Path.home() / "Documents/specimin-out/invokeSubscriber/nullaway-report.txt"
REPORT_OUT = pathlib.Path.home() / "Documents/specimin-out/null-inference-report.txt"

def collect_java_files(root: pathlib.Path) -> dict[str, str]:
    files = {}
    for p in sorted(root.rglob("*.java")):
        relative = p.relative_to(root)
        files[str(relative)] = p.read_text(encoding="utf-8")
    return files

def read_nullaway_warnings(report_path: pathlib.Path) -> str:
    if not report_path.exists():
        return "(no nullaway-report.txt found — run: ./gradlew clean compileJava 2>&1 | tee nullaway-report.txt)"
    content = report_path.read_text(encoding="utf-8")
    warnings = [line for line in content.splitlines()
                if "NullAway" in line or "warning:" in line or "error:" in line]
    if not warnings:
        return "(nullaway-report.txt exists but contains no NullAway warnings — recompile with ./gradlew clean compileJava)"
    return "\n".join(warnings)

def build_prompt(java_files: dict[str, str], nullaway_warnings: str) -> str:
    sources = "\n\n".join(
        f"// === {name} ===\n{src}" for name, src in java_files.items()
    )
    return f"""You are a Java null-safety expert working with NullAway and JSpecify annotations.

The Java code below is a Specimin-reduced minimal reproduction of a method originally
annotated with @NullUnmarked, meaning its nullability was previously unverified.
The @NullUnmarked annotation has been removed so that NullAway now checks it.

--- NULLAWAY WARNINGS (produced by compiling this reduced code without @NullUnmarked) ---

{nullaway_warnings}

--- END OF NULLAWAY WARNINGS ---

These warnings are your starting point. They identify the exact expressions NullAway
cannot prove are non-null. Use them alongside the source code to make your inferences.

--- REDUCED SOURCE CODE ---

{sources}

--- END OF SOURCE CODE ---

Your task:
1. For every parameter, return type, and field in the code, infer whether it should
   be @Nullable or @NonNull (i.e., unannotated/non-null by default under NullAway).
   Pay special attention to the expressions flagged in the NullAway warnings above.
2. For each inference decision, give a one-sentence justification based on how the
   value is used in the method body.
3. Identify any dereferences that are unsafe if a value is @Nullable (i.e., spots
   that need a null check or precondition), cross-referencing the NullAway warnings.
4. Produce a corrected version of the code with all annotations applied and any
   necessary null checks added.

Provide your response in three sections:
A) Annotation decisions (table: location | inferred annotation | reason)
B) Unsafe dereferences (list with line references, linked to the NullAway warnings)
C) Corrected annotated code
"""

def main():
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    print("Reading Specimin-reduced Java files...")
    java_files = collect_java_files(SPECIMIN_OUT)
    if not java_files:
        print(f"No .java files found in {SPECIMIN_OUT}")
        return
    print(f"Found {len(java_files)} file(s): {', '.join(java_files)}")

    print("Reading NullAway warnings...")
    nullaway_warnings = read_nullaway_warnings(NULLAWAY_REPORT)
    print(f"Warnings:\n{nullaway_warnings}\n")

    prompt = build_prompt(java_files, nullaway_warnings)
    print(f"Sending {len(prompt):,} characters to Groq llama-3.3-70b...")

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.choices[0].message.content

    REPORT_OUT.write_text(result, encoding="utf-8")
    print(f"\nInference report saved to: {REPORT_OUT}")
    print("\n" + "=" * 60)
    print(result)

if __name__ == "__main__":
    main()

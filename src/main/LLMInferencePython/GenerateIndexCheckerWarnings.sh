#!/usr/bin/env bash
# GenerateIndexCheckerWarnings.sh
#
# Runs the Checker Framework's Index Checker (-processor index) on the Gson
# library sources and writes the warnings to index-checker-warnings.log. This
# mirrors GenerateNullAwayWarnings.sh, but targets Gson + the Index Checker
# instead of EventBus + NullAway.
#
# It does NOT modify your Gson checkout. It builds the Maven runtime classpath
# for the "gson" module, lists its Java sources, and invokes
# "$CF_HOME/checker/bin/javac" directly on them — the same command line you'd
# run by hand:
#
#   "$CF_HOME/checker/bin/javac" -processor index -classpath "$(cat cp.txt)" \
#     -d out -Xmaxwarns 100000 -Xmaxerrs 100000 @sources.txt \
#     > out.log 2> warnings.log
#
# Requirements:
#   - A built Checker Framework checkout, with CF_HOME pointing at its root
#     (i.e. "$CF_HOME/checker/bin/javac" must exist).
#   - Maven (mvn) on PATH, to resolve Gson's runtime classpath.
#
# Everything is configurable via environment variables; the defaults match the
# author's machine.
#
#   GSON_DIR           root of your gson checkout           (default: ~/Documents/gson)
#   GSON_MODULE_DIR     the "gson" library module (pom.xml + src) (default: $GSON_DIR/gson)
#   GSON_SRC_ROOT       Java source root to check             (default: $GSON_MODULE_DIR/src/main/java)
#   CF_HOME             Checker Framework checkout (required)
#   PROCESSOR           checker processor to run               (default: index)
#   OUT_DIR             where to write the log/warnings file   (default: $GSON_DIR)
#   MAXWARNS            -Xmaxwarns                             (default: 100000)
#   MAXERRS             -Xmaxerrs                              (default: 100000)
#   BUILD_DIR           scratch dir for classpath/sources/out  (default: $GSON_DIR/.index-checker-build)
#
# Usage:
#   CF_HOME=~/checker-framework ./GenerateIndexCheckerWarnings.sh
#   GSON_DIR=~/code/gson CF_HOME=~/checker-framework ./GenerateIndexCheckerWarnings.sh

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
GSON_DIR="${GSON_DIR:-$HOME/Documents/gson}"
GSON_MODULE_DIR="${GSON_MODULE_DIR:-$GSON_DIR/gson}"
GSON_SRC_ROOT="${GSON_SRC_ROOT:-$GSON_MODULE_DIR/src/main/java}"
CF_HOME="${CF_HOME:-}"
PROCESSOR="${PROCESSOR:-index}"
OUT_DIR="${OUT_DIR:-$GSON_DIR}"
MAXWARNS="${MAXWARNS:-100000}"
MAXERRS="${MAXERRS:-100000}"
BUILD_DIR="${BUILD_DIR:-$GSON_DIR/.index-checker-build}"

CP_FILE="$BUILD_DIR/gson-cp.txt"
SOURCES_FILE="$BUILD_DIR/gson-sources.txt"
CHECKER_OUT_DIR="$BUILD_DIR/classes"

OUT_LOG="$OUT_DIR/index-checker-out.log"
WARN_FILE="$OUT_DIR/index-checker-warnings.log"

DIVIDER="$(printf '─%.0s' {1..60})"

# ── Preflight ────────────────────────────────────────────────────────────────
if [[ -z "$CF_HOME" ]]; then
    echo "ERROR: CF_HOME is not set. Point it at your Checker Framework checkout" >&2
    echo "       (the one containing checker/bin/javac)." >&2
    exit 1
fi
CHECKER_JAVAC="$CF_HOME/checker/bin/javac"
if [[ ! -x "$CHECKER_JAVAC" ]]; then
    echo "ERROR: Checker Framework javac not found or not executable: $CHECKER_JAVAC" >&2
    exit 1
fi
if [[ ! -d "$GSON_SRC_ROOT" ]]; then
    echo "ERROR: Gson source root not found: $GSON_SRC_ROOT" >&2
    echo "       Set GSON_DIR, GSON_MODULE_DIR, or GSON_SRC_ROOT." >&2
    exit 1
fi
if [[ ! -f "$GSON_MODULE_DIR/pom.xml" ]]; then
    echo "ERROR: no pom.xml found in $GSON_MODULE_DIR." >&2
    exit 1
fi
if ! command -v mvn >/dev/null 2>&1; then
    echo "ERROR: mvn not found on PATH (needed to resolve Gson's runtime classpath)." >&2
    exit 1
fi

mkdir -p "$BUILD_DIR" "$CHECKER_OUT_DIR" "$OUT_DIR"

# Resolve to an absolute path so the checker prints absolute file locations.
GSON_SRC_ROOT="$(cd "$GSON_SRC_ROOT" && pwd)"

java_count=$(find "$GSON_SRC_ROOT" -name '*.java' | wc -l | tr -d ' ')
echo "$DIVIDER"
echo "Index Checker on Gson"
echo "  source root : $GSON_SRC_ROOT  ($java_count Java file(s))"
echo "  processor   : $PROCESSOR"
echo "  CF_HOME     : $CF_HOME"
echo "  build dir   : $BUILD_DIR"
echo "$DIVIDER"

# ── Resolve the runtime classpath ────────────────────────────────────────────
echo "Resolving Maven classpath for $GSON_MODULE_DIR ..."
( cd "$GSON_MODULE_DIR" && mvn -q -DskipTests dependency:build-classpath -Dmdep.outputFile="$CP_FILE" )
if [[ ! -s "$CP_FILE" ]]; then
    echo "ERROR: failed to resolve a classpath into $CP_FILE." >&2
    exit 1
fi

# ── List the sources to check ────────────────────────────────────────────────
# Exclude module-info.java: it declares "requires com.google.errorprone.annotations"
# etc., which only resolve against a --module-path. We compile via a plain
# -classpath (unnamed module), so a named module in the source set can't see
# those requires and javac fails with "module not found". The descriptor has
# no analyzable code anyway, so dropping it doesn't affect the Index Checker's
# findings.
find "$GSON_SRC_ROOT" -name '*.java' ! -name 'module-info.java' > "$SOURCES_FILE"

# ── Run the Index Checker ────────────────────────────────────────────────────
echo "Running the Index Checker (stdout -> $OUT_LOG, warnings -> $WARN_FILE) ..."
"$CHECKER_JAVAC" \
    -processor "$PROCESSOR" \
    -classpath "$(cat "$CP_FILE")" \
    -d "$CHECKER_OUT_DIR" \
    -Xmaxwarns "$MAXWARNS" \
    -Xmaxerrs "$MAXERRS" \
    @"$SOURCES_FILE" \
    > "$OUT_LOG" 2> "$WARN_FILE" || true

warn_count=$(grep -cE ': (warning|error): \[' "$WARN_FILE" 2>/dev/null || echo 0)

echo ""
echo "$(printf '═%.0s' {1..60})"
echo "Done."
echo "  stdout log  : $OUT_LOG"
echo "  Warnings    : $WARN_FILE  ($warn_count Index Checker finding(s))"
if [[ "$warn_count" -eq 0 ]]; then
    echo ""
    echo "  No '[...]' finding lines were found. Check $OUT_LOG and $WARN_FILE — the"
    echo "  most common causes are an unresolved classpath or a CF_HOME that doesn't"
    echo "  point at a built Checker Framework checkout."
fi

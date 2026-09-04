#!/usr/bin/env bash
# RunCheckerFrameworkAll.sh
#
# Sibling of RunCheckerAll.sh: for every Specimin slice folder in
# SPECIMIN_OUT, runs the Checker Framework (https://checkerframework.org/)
# on that slice instead of NullAway -- injects a throwaway Gradle project
# (settings.gradle + build.gradle) into the slice, pointing its source set
# at the slice itself, applies the `org.checkerframework` Gradle plugin, and
# runs `./gradlew clean compileJava` with the Nullness Checker enabled.
# Writes checker-report.txt (full build log) and checker-warnings.txt (just
# the Checker Framework diagnostic lines) inside the slice folder --
# findings specific to THAT slice.  The Gradle wrapper is copied from
# SPECIMIN_DIR, same as RunCheckerAll.sh.
#
# This script does NOT modify RunCheckerAll.sh or its output files --
# it is a separate tool writing separate output files (checker-report.txt /
# checker-warnings.txt, not nullaway-report.txt / nullaway-warnings.txt), so
# both scripts can be run over the same SPECIMIN_OUT without clobbering each
# other's results.
#
# The `org.checkerframework` plugin version and Checker Framework version
# default to the same ones already pinned in Specimin's own build.gradle
# (checkerFramework { version = "4.2.0" }, plugin 1.0.2), since those are
# known to work in this repo's toolchain.
#
# Run this AFTER RunSpeciminAll.py and FixSpeciminNullInits.py, on the same
# SPECIMIN_OUT.
#
# Requirements:
#   - Java 11+ active.
#   - Network access to Maven Central and the Gradle Plugin Portal the first
#     time you run it, to download the Checker Framework / checker-qual
#     jars (they're then cached in ~/.gradle).
#
# Everything is configurable via environment variables; the defaults match
# the rest of this pipeline and Specimin's own build.gradle.
#
#   SPECIMIN_OUT         slice folders to check            (default: ~/EventBus/specimin-out)
#   SPECIMIN_DIR          Specimin checkout (has gradlew)    (default: ~/specimin)
#   JAR_PATH              compile-time dependency jars       (default: ~/eventbus-deps)
#   CHECKERS               comma-separated checker classes    (default: org.checkerframework.checker.nullness.NullnessChecker)
#   CHECKER_SEVERITY      WARN or ERROR                      (default: WARN)
#   CF_VERSION             Checker Framework version           (default: 4.2.0)
#   CF_PLUGIN_VERSION     org.checkerframework plugin version (default: 1.0.2)
#   GRADLE_DIST_VERSION   Gradle distribution to force        (default: 8.7)
#
# Usage:
#   ./RunCheckerFrameworkAll.sh

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
SPECIMIN_OUT="${SPECIMIN_OUT:-$HOME/Documents/EventBus/specimin-out}"
SPECIMIN_DIR="${SPECIMIN_DIR:-$HOME/Documents/specimin}"
JAR_PATH="${JAR_PATH:-$HOME/eventbus-deps}"
CHECKERS="${CHECKERS:-org.checkerframework.checker.nullness.NullnessChecker}"
CHECKER_SEVERITY="${CHECKER_SEVERITY:-WARN}"
CF_VERSION="${CF_VERSION:-4.2.0}"
CF_PLUGIN_VERSION="${CF_PLUGIN_VERSION:-1.0.2}"
GRADLE_DIST_VERSION="${GRADLE_DIST_VERSION:-8.7}"

GRADLE_WRAPPER_SRC="$SPECIMIN_DIR"   # contains gradlew, gradlew.bat, gradle/

DIVIDER="$(printf '─%.0s' {1..60})"

# ── Preflight checks ───────────────────────────────────────────────────────────
for path in "$SPECIMIN_OUT" "$SPECIMIN_DIR" "$JAR_PATH"; do
    if [[ ! -d "$path" ]]; then
        echo "ERROR: required directory not found: $path"
        exit 1
    fi
done
if [[ ! -f "$SPECIMIN_DIR/gradlew" || ! -d "$SPECIMIN_DIR/gradle" ]]; then
    echo "ERROR: Gradle wrapper not found under $SPECIMIN_DIR (need gradlew + gradle/)." >&2
    exit 1
fi

# ── Helpers ────────────────────────────────────────────────────────────────────

# Turn a comma-separated CHECKERS value into a Groovy string-list literal,
# e.g. "a.B,c.D" -> "'a.B', 'c.D'"
checkers_groovy_list() {
    local IFS=','
    local -a parts=($CHECKERS)
    local out=""
    for p in "${parts[@]}"; do
        p="$(echo "$p" | xargs)"  # trim whitespace
        [[ -z "$p" ]] && continue
        if [[ -n "$out" ]]; then
            out="$out, '$p'"
        else
            out="'$p'"
        fi
    done
    echo "$out"
}

inject_gradle_files() {
    local dir="$1"
    local checkers_list
    checkers_list="$(checkers_groovy_list)"

    cat > "$dir/settings.gradle" <<'EOF'
rootProject.name = 'specimin-checker-framework-check'
EOF

    # build.gradle -- the Checker Framework's Nullness Checker via the
    # official `org.checkerframework` Gradle plugin, source set is the
    # slice itself. When CHECKER_SEVERITY=WARN we pass -Awarns so type
    # errors are demoted to warnings and don't fail compileJava, mirroring
    # NullAway's WARN severity in RunCheckerAll.sh; ERROR leaves them as
    # javac errors, which fails the build.
    local extra_args="'-Xmaxwarns', '10000'"
    if [[ "$CHECKER_SEVERITY" == "WARN" ]]; then
        extra_args="$extra_args, '-Awarns'"
    fi

    cat > "$dir/build.gradle" <<EOF
plugins {
    id 'java'
    id 'org.checkerframework' version '${CF_PLUGIN_VERSION}'
}

repositories {
    mavenCentral()
}

dependencies {
    compileOnly fileTree(dir: '${JAR_PATH}', include: '*.jar')
}

sourceSets {
    main {
        java {
            srcDirs = ['.']
            include 'org/**/*.java'
        }
    }
}

checkerFramework {
    checkers = [${checkers_list}]
    extraJavacArgs = [${extra_args}]
    version = '${CF_VERSION}'
}

tasks.withType(JavaCompile).configureEach {
    options.fork = true
    options.forkOptions.memoryMaximumSize = '4g'
}
EOF
}

copy_gradle_wrapper() {
    local dir="$1"
    if [[ ! -f "$dir/gradlew" ]]; then
        cp "$GRADLE_WRAPPER_SRC/gradlew"     "$dir/gradlew"
        cp "$GRADLE_WRAPPER_SRC/gradlew.bat" "$dir/gradlew.bat" 2>/dev/null || true
        cp -r "$GRADLE_WRAPPER_SRC/gradle"   "$dir/gradle"
        chmod +x "$dir/gradlew"
    fi

    # Force a Gradle version new enough for the Checker Framework plugin.
    mkdir -p "$dir/gradle/wrapper"
    cat > "$dir/gradle/wrapper/gradle-wrapper.properties" <<EOF
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-${GRADLE_DIST_VERSION}-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
EOF
}

run_checker_framework() {
    local dir="$1"
    local report="$dir/checker-report.txt"
    local warnings="$dir/checker-warnings.txt"

    echo "  Running Checker Framework (${CHECKERS})..."
    local gradle_status=0
    ./gradlew --no-daemon clean compileJava > "$report" 2>&1 || gradle_status=$?

    # Every Checker Framework diagnostic ends with a bracketed suppress-
    # warnings key, e.g. "Foo.java:10: error: [assignment] incompatible
    # types...", whether reported as a warning (-Awarns) or an error.
    grep -E ': (error|warning): \[' "$report" > "$warnings" || true

    local warn_count
    warn_count="$(wc -l < "$warnings" | tr -d ' ')"

    if [[ "$warn_count" -eq 0 ]]; then
        echo "  Checker Framework findings : none"
    else
        echo "  Checker Framework findings : $warn_count"
    fi
    echo "  Full report saved          : checker-report.txt"
    echo "  Findings saved              : checker-warnings.txt"

    return "$gradle_status"
}

# ── Main loop ──────────────────────────────────────────────────────────────────
total=0
passed=0
failed=0
failed_dirs=()

for dir in "$SPECIMIN_OUT"/*/; do
    [[ -d "$dir" ]] || continue
    java_count=$(find "$dir" -name "*.java" | wc -l | tr -d ' ')
    if [[ "$java_count" -eq 0 ]]; then
        echo "SKIP (no .java files): $(basename "$dir")"
        continue
    fi

    total=$(( total + 1 ))
    name=$(basename "$dir")
    echo ""
    echo "$DIVIDER"
    echo "[$total] $name  ($java_count Java file(s))"
    echo "$DIVIDER"

    inject_gradle_files "$dir"
    copy_gradle_wrapper "$dir"

    # compileJava's real result counts as pass/fail: with CHECKER_SEVERITY=WARN
    # (the default, via -Awarns) findings don't fail the build, so this mostly
    # tracks whether the slice compiles at all; with CHECKER_SEVERITY=ERROR it
    # also reflects whether the Checker Framework found anything.
    if ( cd "$dir" && run_checker_framework "$dir" ); then
        passed=$(( passed + 1 ))
    else
        failed=$(( failed + 1 ))
        failed_dirs+=("$name")
    fi
done

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "$(printf '═%.0s' {1..60})"
echo "Summary: $total folder(s) processed"
echo "  Gradle succeeded : $passed"
echo "  Gradle failed    : $failed"
if [[ ${#failed_dirs[@]} -gt 0 ]]; then
    echo "  Failed folders:"
    for d in "${failed_dirs[@]}"; do
        echo "    - $d"
    done
fi
echo ""
echo "Checker Framework findings per folder:"
for dir in "$SPECIMIN_OUT"/*/; do
    [[ -d "$dir" ]] || continue
    cfile="$dir/checker-warnings.txt"
    if [[ -f "$cfile" ]]; then
        ccount="$(wc -l < "$cfile" | tr -d ' ')"
        printf "  %-40s Checker Framework: %s\n" "$(basename "$dir")" "$ccount"
    fi
done

#!/usr/bin/env bash
# RunCFCheckerAll.sh
#
# Parallel version of RunCheckerAll.sh: for every Specimin slice folder in
# SPECIMIN_OUT, runs the Checker Framework's Nullness Checker on that slice
# instead of NullAway, and writes cf-report.txt (full build log) and
# cf-warnings.txt (just the Checker Framework findings) inside the slice
# folder -- findings specific to THAT slice.
#
# Unlike RunCheckerAll.sh, the throwaway Gradle project here is NOT injected
# directly into the slice folder itself (that would collide with
# RunCheckerAll.sh's own settings.gradle/build.gradle/gradlew if both
# scripts are run against the same SPECIMIN_OUT, which is the point of this
# being a "parallel" script). Instead, each slice gets a small nested
# "cf-project/" subdirectory holding its own Gradle project, whose source
# set points at '..' (the slice folder itself) -- the same
# separate-build-dir-pointing-at-real-sources pattern
# GenerateNullAwayWarnings.sh's PROJECT=eventbus path already uses for the
# same reason (keeping a throwaway build's own project files from mixing
# with the sources it's checking).
#
# The Checker Framework's own Gradle plugin (id 'org.checkerframework')
# handles the --add-exports/--add-opens JVM flags CF needs on JDK 16+
# automatically, the same convenience net.ltgt.gradle.errorprone provides
# for Error Prone in RunCheckerAll.sh -- no manual JVM args are needed here.
# Nor does the Checker Framework need any of RunCheckerAll.sh's Error-Prone-
# specific compilerArgs (-XDcompilePolicy=simple, --should-stop=ifError=FLOW,
# -XDaddTypeAnnotationsToSymbol=true): those exist only because Error Prone
# hooks into javac as a "-Xplugin"; the Checker Framework instead runs as an
# ordinary annotation processor (-processor), so none of that machinery
# applies.
#
# Run this AFTER RunSpeciminAll.py, on the same SPECIMIN_OUT. It can be run
# before, after, or interleaved with RunCheckerAll.sh against the same
# slices without interference, since the two scripts touch disjoint files
# (nullaway-*.txt / settings.gradle / build.gradle / gradlew directly in the
# slice folder, versus cf-*.txt directly in the slice folder plus a nested
# cf-project/ subdirectory).
#
# Requirements:
#   - A JDK the Checker Framework supports (11+; this pipeline already
#     expects Java 21+ active for RunCheckerAll.sh -- see that script's own
#     Requirements section -- and there is no known reason to switch JDKs
#     just for this script).
#
# Everything is configurable via environment variables; the defaults match
# the rest of this pipeline and Specimin's own build.gradle, which already
# runs the Checker Framework's Nullness Checker on itself with this exact
# plugin/CF version pair (see the `checkerFramework { ... }` block and the
# `id("org.checkerframework").version("1.0.2")` line in
# specimin/build.gradle) -- the one concretely-known-working CF setup
# available in this repository, and the safest starting point for a new one.
#
#   SPECIMIN_OUT                slice folders to check           (default: ~/Documents/junit4/speciminout)
#   SPECIMIN_DIR                 Specimin checkout (has gradlew)   (default: ~/Documents/specimin)
#   JAR_PATH                     compile-time dependency jars      (default: ~/junit-deps)
#   CF_CHECKER                   fully-qualified checker class     (default: org.checkerframework.checker.nullness.NullnessChecker)
#   CF_SEVERITY                  WARN or ERROR                     (default: WARN)
#   CF_VERSION                   Checker Framework version         (default: 4.2.0)
#   CHECKERFRAMEWORK_PLUGIN_VERSION  org.checkerframework Gradle plugin version (default: 1.0.2)
#   CF_MEMORY_MAX                 max heap for the forked compiler  (default: 2g)
#   GRADLE_DIST_VERSION           Gradle distribution to force       (default: 8.7)
#
# Usage:
#   ./RunCFCheckerAll.sh

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
SPECIMIN_OUT="${SPECIMIN_OUT:-$HOME/Documents/junit4/speciminout}"
SPECIMIN_DIR="${SPECIMIN_DIR:-$HOME/Documents/specimin}"
JAR_PATH="${JAR_PATH:-$HOME/junit-deps}"
CF_CHECKER="${CF_CHECKER:-org.checkerframework.checker.nullness.NullnessChecker}"
CF_SEVERITY="${CF_SEVERITY:-WARN}"
CF_VERSION="${CF_VERSION:-4.2.0}"
CHECKERFRAMEWORK_PLUGIN_VERSION="${CHECKERFRAMEWORK_PLUGIN_VERSION:-1.0.2}"
CF_MEMORY_MAX="${CF_MEMORY_MAX:-2g}"
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

inject_gradle_files() {
    local dir="$1"
    local project_dir="$dir/cf-project"

    mkdir -p "$project_dir"

    cat > "$project_dir/settings.gradle" <<'EOF'
rootProject.name = 'specimin-cf-checker-check'
EOF

    # CF_SEVERITY=WARN downgrades every Checker Framework error to a warning
    # (via -Awarns), the same non-fatal-findings behavior RunCheckerAll.sh
    # gets from NULLAWAY_SEVERITY=WARN, so a real finding doesn't turn into
    # a Gradle build failure by default.
    local cf_warns_line=""
    if [[ "$CF_SEVERITY" == "WARN" ]]; then
        cf_warns_line="    options.compilerArgs << '-Awarns'"
    fi

    # build.gradle -- the Checker Framework's Nullness Checker via the
    # org.checkerframework Gradle plugin, source set points at '..' (the
    # slice folder itself, one directory up from this throwaway project) so
    # nothing needs to be copied. The plugin adds the checker/checker-qual
    # dependencies and the JDK 16+ --add-exports/--add-opens JVM flags
    # automatically -- see this file's header comment.
    cat > "$project_dir/build.gradle" <<EOF
plugins {
    id 'java'
    id 'org.checkerframework' version '${CHECKERFRAMEWORK_PLUGIN_VERSION}'
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
            srcDirs = ['..']
            include '**/*.java'
        }
    }
}

checkerFramework {
    checkers = ['${CF_CHECKER}']
    version = '${CF_VERSION}'
}

tasks.withType(JavaCompile).configureEach {
    // The Checker Framework needs significantly more memory than the
    // typical Java compiler, and (like Specimin's own build.gradle) needs
    // to fork so the extra JVM args the plugin adds apply to the process
    // that actually runs the compiler.
    options.fork = true
    options.forkOptions.memoryMaximumSize = '${CF_MEMORY_MAX}'
${cf_warns_line}
    options.compilerArgs << '-Xmaxwarns' << '10000'
}
EOF
}

copy_gradle_wrapper() {
    local dir="$1"
    local project_dir="$dir/cf-project"

    if [[ ! -f "$project_dir/gradlew" ]]; then
        cp "$GRADLE_WRAPPER_SRC/gradlew"     "$project_dir/gradlew"
        cp "$GRADLE_WRAPPER_SRC/gradlew.bat" "$project_dir/gradlew.bat" 2>/dev/null || true
        cp -r "$GRADLE_WRAPPER_SRC/gradle"   "$project_dir/gradle"
        chmod +x "$project_dir/gradlew"
    fi

    # Force a Gradle version new enough for the Checker Framework + modern Java.
    mkdir -p "$project_dir/gradle/wrapper"
    cat > "$project_dir/gradle/wrapper/gradle-wrapper.properties" <<EOF
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-${GRADLE_DIST_VERSION}-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
EOF
}

run_cf() {
    local dir="$1"
    local report="$dir/cf-report.txt"
    local warnings="$dir/cf-warnings.txt"

    echo "  Running the Checker Framework ($CF_CHECKER)..."
    ( cd "$dir/cf-project" && ./gradlew --no-daemon clean compileJava ) > "$report" 2>&1 || true

    # Matches the classic javac diagnostic shape the Checker Framework emits
    # (forked javac, so no Maven-style non-forked "[line,col]" formatting to
    # worry about here -- see ExtractWarningMethods.py's _LOCATION_RE for
    # why that distinction matters elsewhere in this pipeline). Unlike
    # NullAway, whose findings always carry the literal tag "[NullAway]",
    # the Checker Framework uses a different bracketed message key per
    # finding kind (e.g. "[assignment.type.incompatible]",
    # "[dereference.of.nullable]", "[return.type.incompatible]"), so this
    # matches the diagnostic shape generically instead of one fixed tag.
    # Since only CF_CHECKER is enabled here, every such line is one of its
    # findings.
    grep -E ': (warning|error): \[' "$report" > "$warnings" || true

    local warn_count
    warn_count="$(wc -l < "$warnings" | tr -d ' ')"

    if [[ "$warn_count" -eq 0 ]]; then
        echo "  Checker Framework findings : none"
    else
        echo "  Checker Framework findings : $warn_count"
    fi
    echo "  Full report saved          : cf-report.txt"
    echo "  Findings saved             : cf-warnings.txt"
}

# ── Main loop ──────────────────────────────────────────────────────────────────
total=0
passed=0
failed=0
failed_dirs=()

for dir in "$SPECIMIN_OUT"/*/; do
    [[ -d "$dir" ]] || continue
    # cf-project/ (created below, on a later run of this script) never
    # contains .java files, so it doesn't need excluding here -- this
    # mirrors RunCheckerAll.sh's own unrestricted recursive count exactly.
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

    # The Checker Framework's real `compileJava` result counts as pass/fail,
    # since that reflects whether the slice actually compiles (CF findings
    # are warnings, not compile failures, unless CF_SEVERITY=ERROR).
    if ( run_cf "$dir" ); then
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
    cfile="$dir/cf-warnings.txt"
    if [[ -f "$cfile" ]]; then
        ccount="$(wc -l < "$cfile" | tr -d ' ')"
        printf "  %-40s Checker Framework: %s\n" "$(basename "$dir")" "$ccount"
    fi
done

#!/usr/bin/env bash
# RunCheckerAll.sh
#
# For every Specimin slice folder in SPECIMIN_OUT, runs NullAway on that
# slice: injects a throwaway Gradle project (settings.gradle + build.gradle)
# into the slice, pointing its source set at the slice itself, and runs
# `./gradlew clean compileJava` with NullAway enabled via Error Prone.
# Writes nullaway-report.txt (full build log) and nullaway-warnings.txt
# (just the NullAway findings, same shape as gson's own
# nullaway-warnings.txt, see GenerateNullAwayWarnings.sh) inside the slice
# folder -- warnings specific to THAT slice. The Gradle wrapper is copied
# from SPECIMIN_DIR.
#
# Unlike LLMInferencePython/RunCheckerAll.sh, this script runs ONLY
# NullAway (no Index Checker / CF_HOME dependency), and defaults to gson's
# real annotated package and a NullAway/Error Prone version pair known to
# work together (see GenerateNullAwayWarnings.sh's PROJECT=gson path).
#
# Run this AFTER RunSpeciminAll.py, on the same SPECIMIN_OUT.
#
# Requirements:
#   - Java 17+ active: export JAVA_HOME=$(/usr/libexec/java_home -v 17)
#
# Everything is configurable via environment variables; the defaults match
# the rest of this pipeline and gson's own -Pnullaway profile.
#
#   SPECIMIN_OUT        slice folders to check            (default: ~/Documents/gson/speciminout)
#   SPECIMIN_DIR         Specimin checkout (has gradlew)   (default: ~/Documents/specimin)
#   JAR_PATH             compile-time dependency jars      (default: ~/gson-deps)
#   ANNOTATED_PACKAGES   NullAway:AnnotatedPackages        (default: com.google.gson)
#   NULLAWAY_SEVERITY    WARN or ERROR                     (default: WARN)
#   ERRORPRONE_VERSION   Error Prone core version          (default: 2.50.0)
#   NULLAWAY_VERSION     NullAway version                  (default: 0.13.7)
#   GRADLE_DIST_VERSION  Gradle distribution to force       (default: 8.7)
#
# Usage:
#   ./RunCheckerAll.sh

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
SPECIMIN_OUT="${SPECIMIN_OUT:-$HOME/Documents/gson/speciminout}"
SPECIMIN_DIR="${SPECIMIN_DIR:-$HOME/Documents/specimin}"
JAR_PATH="${JAR_PATH:-$HOME/gson-deps}"
ANNOTATED_PACKAGES="${ANNOTATED_PACKAGES:-com.google.gson}"
NULLAWAY_SEVERITY="${NULLAWAY_SEVERITY:-WARN}"
ERRORPRONE_VERSION="${ERRORPRONE_VERSION:-2.50.0}"
NULLAWAY_VERSION="${NULLAWAY_VERSION:-0.13.7}"
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

    cat > "$dir/settings.gradle" <<'EOF'
rootProject.name = 'specimin-checker-check'
EOF

    # build.gradle -- NullAway via Error Prone, source set is the slice
    # itself. JSpecifyMode=false + RequireExplicitNullMarking OFF: gson has
    # no @NullMarked/@NullUnmarked annotations anywhere, so NullAway's
    # JSpecify mode (default since ~0.11, and NULLAWAY_VERSION here is newer
    # than that) would otherwise only emit a "please annotate"
    # RequireExplicitNullMarking advisory instead of actually analyzing the
    # slice -- see GenerateNullAwayWarnings.sh's PROJECT=gson path, which
    # applies the same two settings against gson's real sources.
    cat > "$dir/build.gradle" <<EOF
plugins {
    id 'java'
    id 'net.ltgt.errorprone' version '3.1.0'
}

repositories {
    mavenCentral()
}

dependencies {
    errorprone           'com.google.errorprone:error_prone_core:${ERRORPRONE_VERSION}'
    annotationProcessor  'com.uber.nullaway:nullaway:${NULLAWAY_VERSION}'
    compileOnly          'com.uber.nullaway:nullaway-annotations:${NULLAWAY_VERSION}'
    compileOnly          'com.google.code.findbugs:jsr305:3.0.2'
    compileOnly          'org.jspecify:jspecify:0.3.0'
    compileOnly          fileTree(dir: '${JAR_PATH}', include: '*.jar')
}

sourceSets {
    main {
        java {
            srcDirs = ['.']
            include '**/*.java'
        }
    }
}

tasks.withType(JavaCompile).configureEach {
    options.errorprone {
        check('NullAway', net.ltgt.gradle.errorprone.CheckSeverity.${NULLAWAY_SEVERITY})
        check('RequireExplicitNullMarking', net.ltgt.gradle.errorprone.CheckSeverity.OFF)
        option('NullAway:AnnotatedPackages', '${ANNOTATED_PACKAGES}')
        option('NullAway:JSpecifyMode', 'false')
    }
    // Required for Error Prone to run at all (see
    // https://errorprone.info/docs/installation#gradle): javac's default
    // --should-stop=ifError policy (INIT) stops compilation before the FLOW
    // phase Error Prone hooks into, so it refuses to run under it --
    // "The default --should-stop=ifError policy (INIT) is not supported by
    // Error Prone, pass --should-stop=ifError=FLOW instead". Unlike the
    // --add-exports/--add-opens JVM flags (which net.ltgt.gradle.errorprone
    // adds automatically on JDK 16+), these compilerArgs are not added for
    // you and must be set explicitly -- gson's own pom.xml sets the Maven
    // equivalent of both for the same reason.
    options.compilerArgs << '-XDcompilePolicy=simple' << '--should-stop=ifError=FLOW'
    options.compilerArgs << '-Xmaxwarns' << '10000'
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

    # Force a Gradle version new enough for Error Prone + modern Java.
    mkdir -p "$dir/gradle/wrapper"
    cat > "$dir/gradle/wrapper/gradle-wrapper.properties" <<EOF
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-${GRADLE_DIST_VERSION}-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
EOF
}

run_nullaway() {
    local dir="$1"
    local report="$dir/nullaway-report.txt"
    local warnings="$dir/nullaway-warnings.txt"

    echo "  Running NullAway..."
    ./gradlew --no-daemon clean compileJava > "$report" 2>&1 || true

    grep -E '\[NullAway\]' "$report" > "$warnings" || true

    local warn_count
    warn_count="$(wc -l < "$warnings" | tr -d ' ')"

    if [[ "$warn_count" -eq 0 ]]; then
        echo "  NullAway warnings      : none"
    else
        echo "  NullAway warnings      : $warn_count"
    fi
    echo "  Full report saved      : nullaway-report.txt"
    echo "  Warnings saved         : nullaway-warnings.txt"
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

    # NullAway's real `compileJava` result counts as pass/fail, since that
    # reflects whether the slice actually compiles (NullAway findings are
    # warnings, not compile failures, unless NULLAWAY_SEVERITY=ERROR).
    if ( cd "$dir" && run_nullaway "$dir" ); then
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
echo "NullAway warnings per folder:"
for dir in "$SPECIMIN_OUT"/*/; do
    [[ -d "$dir" ]] || continue
    nfile="$dir/nullaway-warnings.txt"
    if [[ -f "$nfile" ]]; then
        ncount="$(wc -l < "$nfile" | tr -d ' ')"
        printf "  %-40s NullAway: %s\n" "$(basename "$dir")" "$ncount"
    fi
done
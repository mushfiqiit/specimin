#!/usr/bin/env bash
# GenerateNullAwayWarnings.sh
#
# Runs *plain* NullAway on a project's original (un-sliced) sources and writes
# the warnings to nullaway-warnings.txt. This is the very first step of the
# LLM-inference pipeline: ExtractWarningMethods.py reads that file.
#
# Supports two projects, selected via PROJECT:
#
#   PROJECT=eventbus (default) - EventBus core. Builds a small self-contained
#     Gradle project (in $BUILD_DIR) whose source set points at the EventBus
#     core sources, applies the Error Prone + NullAway plugins, and compiles.
#     Does NOT touch your real multi-module EventBus build.
#
#   PROJECT=gson - Gson. Runs Gson's own Maven build with `-Pnullaway`, an
#     opt-in profile that adds NullAway as an extra Error Prone check (Gson
#     already runs Error Prone on every build). That profile must exist in
#     gson/pom.xml first — see the "Gson repo setup" section below.
#
# Requirements:
#   - PROJECT=eventbus: Java 11+ (Java 17 recommended) and network access the
#     first time (Gradle + the Error Prone / NullAway artifacts are downloaded).
#   - PROJECT=gson: Java 17+ (Gson's build requires it), Maven on PATH, and the
#     `nullaway` Maven profile set up in gson/pom.xml (one-time repo change).
#
# ── Gson repo setup (one-time, in the gson checkout) ─────────────────────────
# Gson's root pom.xml already runs Error Prone on every build (see the
# `-Xplugin:ErrorProne ...` arg in the maven-compiler-plugin's pluginManagement).
# To let NullAway opt in without touching the default build:
#   1. Add two properties (empty/default so the normal build is unaffected):
#        <nullaway.checks></nullaway.checks>
#        <nullaway.version>0.10.10</nullaway.version>
#   2. Add `${nullaway.checks}` as the last line inside the big
#      `-Xplugin:ErrorProne ...` <arg>, right before its closing `</arg>`.
#   3. Add a `nullaway` profile (alongside the existing `disable-error-prone`
#      profile) that sets `nullaway.checks` to
#      `-Xep:NullAway:WARN -XepOpt:NullAway:AnnotatedPackages=com.google.gson`
#      and appends a `com.uber.nullaway:nullaway:${nullaway.version}` entry to
#      the compiler plugin's `annotationProcessorPaths` (no `combine.self`
#      override, so it adds to — not replaces — the existing error_prone_core
#      path).
# This script's PROJECT=gson path assumes that profile is already in place.
#
# Everything is configurable via environment variables; the defaults match the
# rest of the pipeline.
#
# Common:
#   PROJECT             eventbus (default) or gson
#   OUT_DIR             where to write the warnings/report
#                         (default: $EVENTBUS_DIR for eventbus, $GSON_DIR for gson)
#   NULLAWAY_SEVERITY   WARN or ERROR                    (default: WARN)
#   NULLAWAY_VERSION    NullAway version                 (default: 0.10.10)
#   ANNOTATED_PACKAGES  NullAway:AnnotatedPackages
#                         (default: org.greenrobot.eventbus for eventbus, com.google.gson for gson)
#
# PROJECT=eventbus only:
#   EVENTBUS_DIR        root of your EventBus checkout
#   EVENTBUS_SRC_ROOT   the Java source root            (default: $EVENTBUS_DIR/EventBus/src)
#   ERRORPRONE_VERSION  Error Prone core version         (default: 2.26.1)
#   GRADLE_DIST_VERSION Gradle distribution to force     (default: 8.7)
#   BUILD_DIR           scratch build dir                (default: $EVENTBUS_DIR/.nullaway-build)
#
# PROJECT=gson only:
#   GSON_DIR            root of your gson checkout       (default: ~/Documents/gson)
#   GSON_MODULE_DIR     the "gson" library module (pom.xml + src) (default: $GSON_DIR/gson)
#
# Usage:
#   ./GenerateNullAwayWarnings.sh
#   EVENTBUS_DIR=~/code/EventBus ./GenerateNullAwayWarnings.sh
#   PROJECT=gson ./GenerateNullAwayWarnings.sh
#   PROJECT=gson GSON_DIR=~/code/gson ./GenerateNullAwayWarnings.sh

set -euo pipefail

PROJECT="${PROJECT:-eventbus}"
DIVIDER="$(printf '─%.0s' {1..60})"

if [[ "$PROJECT" == "gson" ]]; then
    # ── Config (Gson / Maven) ────────────────────────────────────────────────
    GSON_DIR="${GSON_DIR:-$HOME/Documents/gson}"
    GSON_MODULE_DIR="${GSON_MODULE_DIR:-$GSON_DIR/gson}"
    OUT_DIR="${OUT_DIR:-$GSON_DIR}"
    ANNOTATED_PACKAGES="${ANNOTATED_PACKAGES:-com.google.gson}"
    NULLAWAY_SEVERITY="${NULLAWAY_SEVERITY:-WARN}"
    NULLAWAY_VERSION="${NULLAWAY_VERSION:-0.10.10}"

    REPORT_FILE="$OUT_DIR/nullaway-report.txt"
    WARN_FILE="$OUT_DIR/nullaway-warnings.txt"

    # ── Preflight ─────────────────────────────────────────────────────────────
    if [[ ! -f "$GSON_MODULE_DIR/pom.xml" ]]; then
        echo "ERROR: no pom.xml found in $GSON_MODULE_DIR." >&2
        echo "       Set GSON_DIR or GSON_MODULE_DIR." >&2
        exit 1
    fi
    if ! command -v mvn >/dev/null 2>&1; then
        echo "ERROR: mvn not found on PATH." >&2
        exit 1
    fi

    echo "$DIVIDER"
    echo "NullAway on Gson (Maven, -Pnullaway profile)"
    echo "  module      : $GSON_MODULE_DIR"
    echo "  packages    : $ANNOTATED_PACKAGES"
    echo "  severity    : $NULLAWAY_SEVERITY"
    echo "  Java        : $(java -version 2>&1 | head -1)"
    echo "$DIVIDER"

    # ── Run ───────────────────────────────────────────────────────────────────
    # `clean` avoids Maven's incremental compiler skipping already-up-to-date
    # sources, which would otherwise silently produce zero NullAway warnings.
    mkdir -p "$OUT_DIR"
    echo "Compiling with NullAway (output -> $REPORT_FILE) ..."
    ( cd "$GSON_MODULE_DIR" && mvn -Pnullaway \
        -Dnullaway.version="$NULLAWAY_VERSION" \
        -Dnullaway.checks="-Xep:NullAway:$NULLAWAY_SEVERITY -XepOpt:NullAway:AnnotatedPackages=$ANNOTATED_PACKAGES" \
        clean compile ) > "$REPORT_FILE" 2>&1 || true

    # ── Extract warnings ──────────────────────────────────────────────────────
    grep -E '\[NullAway\]' "$REPORT_FILE" > "$WARN_FILE" || true
    warn_count="$(grep -c '\[NullAway\]' "$WARN_FILE" 2>/dev/null || true)"
    warn_count="${warn_count:-0}"

    echo ""
    echo "$(printf '═%.0s' {1..60})"
    echo "Done."
    echo "  Full report : $REPORT_FILE"
    echo "  Warnings    : $WARN_FILE  ($warn_count NullAway warning(s))"
    if [[ "$warn_count" -eq 0 ]]; then
        echo ""
        echo "  No [NullAway] lines found. Check $REPORT_FILE — the most common causes"
        echo "  are the 'nullaway' Maven profile not being set up in gson/pom.xml yet,"
        echo "  a Java version below 17, or a missing network connection for the first"
        echo "  download of the NullAway artifact."
    fi
    exit 0
fi

# ── Config (EventBus / Gradle) ────────────────────────────────────────────────
EVENTBUS_DIR="${EVENTBUS_DIR:-/Users/mushfiqurrahmanchowdhury/Documents/EventBus}"
EVENTBUS_SRC_ROOT="${EVENTBUS_SRC_ROOT:-$EVENTBUS_DIR/EventBus/src}"
OUT_DIR="${OUT_DIR:-$EVENTBUS_DIR}"
ANNOTATED_PACKAGES="${ANNOTATED_PACKAGES:-org.greenrobot.eventbus}"
NULLAWAY_SEVERITY="${NULLAWAY_SEVERITY:-WARN}"
ERRORPRONE_VERSION="${ERRORPRONE_VERSION:-2.26.1}"
NULLAWAY_VERSION="${NULLAWAY_VERSION:-0.10.10}"
GRADLE_DIST_VERSION="${GRADLE_DIST_VERSION:-8.7}"
BUILD_DIR="${BUILD_DIR:-$EVENTBUS_DIR/.nullaway-build}"

REPORT_FILE="$OUT_DIR/nullaway-report.txt"
WARN_FILE="$OUT_DIR/nullaway-warnings.txt"

# ── Preflight ────────────────────────────────────────────────────────────────
if [[ ! -d "$EVENTBUS_SRC_ROOT" ]]; then
    echo "ERROR: EventBus source root not found: $EVENTBUS_SRC_ROOT" >&2
    echo "       Set EVENTBUS_DIR or EVENTBUS_SRC_ROOT." >&2
    exit 1
fi
if [[ ! -f "$EVENTBUS_DIR/gradlew" || ! -d "$EVENTBUS_DIR/gradle" ]]; then
    echo "ERROR: Gradle wrapper not found under $EVENTBUS_DIR (need gradlew + gradle/)." >&2
    exit 1
fi

# Resolve to an absolute path so Error Prone prints absolute file locations
# (ExtractWarningMethods.py expects '/abs/path/File.java:LINE: ... [NullAway]').
EVENTBUS_SRC_ROOT="$(cd "$EVENTBUS_SRC_ROOT" && pwd)"

java_count=$(find "$EVENTBUS_SRC_ROOT" -name '*.java' | wc -l | tr -d ' ')
echo "$DIVIDER"
echo "Plain NullAway on EventBus"
echo "  source root : $EVENTBUS_SRC_ROOT  ($java_count Java file(s))"
echo "  packages    : $ANNOTATED_PACKAGES"
echo "  severity    : $NULLAWAY_SEVERITY"
echo "  build dir   : $BUILD_DIR"
echo "  Java        : $(java -version 2>&1 | head -1)"
echo "$DIVIDER"

# ── Build a self-contained NullAway project ──────────────────────────────────
mkdir -p "$BUILD_DIR/gradle/wrapper"

# Gradle wrapper (force a version new enough for Error Prone + modern Java; the
# stock EventBus wrapper is 6.8.3 which does not run on Java 17).
cp "$EVENTBUS_DIR/gradlew"     "$BUILD_DIR/gradlew"
cp "$EVENTBUS_DIR/gradlew.bat" "$BUILD_DIR/gradlew.bat" 2>/dev/null || true
cp -r "$EVENTBUS_DIR/gradle/wrapper/." "$BUILD_DIR/gradle/wrapper/" 2>/dev/null || true
chmod +x "$BUILD_DIR/gradlew"

cat > "$BUILD_DIR/gradle/wrapper/gradle-wrapper.properties" <<EOF
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-${GRADLE_DIST_VERSION}-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
EOF

cat > "$BUILD_DIR/settings.gradle" <<'EOF'
rootProject.name = 'eventbus-nullaway'
EOF

# The source set points at the real EventBus sources (absolute path), so nothing
# is copied and warnings reference the original files.
cat > "$BUILD_DIR/build.gradle" <<EOF
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
}

sourceSets {
    main {
        java {
            srcDirs = ['${EVENTBUS_SRC_ROOT}']
            include 'org/**/*.java'
        }
    }
}

// EventBus core targets Java 8 source level.
java {
    sourceCompatibility = JavaVersion.VERSION_1_8
    targetCompatibility = JavaVersion.VERSION_1_8
}

tasks.withType(JavaCompile).configureEach {
    options.errorprone {
        check('NullAway', net.ltgt.gradle.errorprone.CheckSeverity.${NULLAWAY_SEVERITY})
        option('NullAway:AnnotatedPackages', '${ANNOTATED_PACKAGES}')
    }
    // Don't let a flood of warnings get truncated, and don't fail the build on them.
    options.compilerArgs << '-Xmaxwarns' << '100000'
}
EOF

# ── Run ──────────────────────────────────────────────────────────────────────
echo "Compiling with NullAway (output -> $REPORT_FILE) ..."
mkdir -p "$OUT_DIR"
( cd "$BUILD_DIR" && ./gradlew --no-daemon clean compileJava ) 2>&1 | tee "$REPORT_FILE" || true

# ── Extract warnings ─────────────────────────────────────────────────────────
# Keep the location lines NullAway emits: '<file>:<line>: warning: [NullAway] ...'
grep -E '\[NullAway\]' "$REPORT_FILE" > "$WARN_FILE" || true

warn_count="$(grep -c '\[NullAway\]' "$WARN_FILE" 2>/dev/null || true)"
warn_count="${warn_count:-0}"

echo ""
echo "$(printf '═%.0s' {1..60})"
echo "Done."
echo "  Full report : $REPORT_FILE"
echo "  Warnings    : $WARN_FILE  ($warn_count NullAway warning(s))"
if [[ "$warn_count" -eq 0 ]]; then
    echo ""
    echo "  No [NullAway] lines found. Check $REPORT_FILE — the most common causes are"
    echo "  a Gradle/Java incompatibility or a missing network connection for the"
    echo "  first download. Try Java 17 and re-run."
fi

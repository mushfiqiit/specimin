#!/usr/bin/env bash
# GenerateNullAwayWarnings.sh
#
# Runs *plain* NullAway on a project's original (un-sliced) sources and writes
# the warnings to nullaway-warnings.txt. This is the very first step of the
# LLM-inference pipeline: ExtractWarningMethods.py reads that file.
#
# Supports three projects, selected via PROJECT (currently defaults to junit):
#
#   PROJECT=junit (default) - JUnit 4. Builds a small self-contained Gradle
#     project (in $BUILD_DIR) whose source set points at JUnit 4's main
#     sources ($JUNIT_SRC_ROOT), applies the Error Prone + NullAway plugins,
#     and compiles. JUnit 4 has no Error Prone setup of its own (and its own
#     pom.xml targets a very old Java level), so, like the EventBus path,
#     this does NOT touch your real JUnit build. The same Gradle project also
#     copies JUnit's compile-time dependencies (hamcrest-core) into JAR_PATH,
#     which RunSpeciminAll.py / RunCheckerAll.sh / RunCFCheckerAll.sh need.
#
#   PROJECT=gson - Gson. Runs Gson's own Maven build with
#     `-Pnullaway`, an opt-in profile that adds NullAway as an extra Error
#     Prone check (Gson already runs Error Prone on every build). That
#     profile must exist in gson/pom.xml first — see the "Gson repo setup"
#     section below.
#
#   PROJECT=eventbus - EventBus core. Builds a small self-contained Gradle
#     project (in $BUILD_DIR) whose source set points at the EventBus core
#     sources, applies the Error Prone + NullAway plugins, and compiles.
#     Does NOT touch your real multi-module EventBus build.
#
# Requirements:
#   - PROJECT=junit: Java 21+ (the default ERRORPRONE_VERSION, 2.50.0, only
#     loads on a JDK 21+ runtime -- see RunCheckerAll.sh), network access the
#     first time, and a Gradle wrapper in SPECIMIN_DIR (JUnit 4 itself is a
#     Maven project and has none).
#   - PROJECT=eventbus: Java 11+ (Java 17 recommended) and network access the
#     first time (Gradle + the Error Prone / NullAway artifacts are downloaded).
#   - PROJECT=gson: Java 21 through 25, Maven on PATH, and the `nullaway`
#     Maven profile set up in gson/pom.xml (one-time repo change). Java 21+
#     is NOT just "recommended" here: gson's own pom.xml has a
#     `disable-error-prone` profile that auto-activates for any JDK below 21
#     and, via `combine.self="override"`, wholesale replaces the
#     maven-compiler-plugin's compilerArgs and annotationProcessorPaths --
#     wiping out Error Prone (and therefore NullAway) entirely, even with
#     `-Pnullaway` passed. On a JDK in [17,21), gson's own enforcer plugin
#     still lets the build succeed, but the compile silently becomes a plain
#     `javac` pass with zero checks run, so nullaway-warnings.txt comes back
#     empty -- a false "no warnings", not a clean-code signal. Gson's
#     enforcer plugin caps the other end at Java < 26 (see the
#     RequireJavaVersion rule in pom.xml), so Java 21-25 is the only window
#     where this actually works.
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
#   PROJECT             junit (default), gson or eventbus
#   OUT_DIR             where to write the warnings/report
#                         (default: $JUNIT_DIR for junit, $EVENTBUS_DIR for eventbus,
#                         $GSON_DIR for gson)
#   NULLAWAY_SEVERITY   WARN or ERROR                    (default: WARN)
#   NULLAWAY_VERSION    NullAway version
#                         (default: 0.13.7 for junit and gson, 0.10.10 for eventbus —
#                         must stay compatible with that project's Error Prone version)
#   ANNOTATED_PACKAGES  NullAway:AnnotatedPackages
#                         (default: org.junit,junit for junit, org.greenrobot.eventbus
#                         for eventbus, com.google.gson for gson)
#
# PROJECT=junit only:
#   JUNIT_DIR           root of your junit4 checkout
#                         (default: /Users/mushfiqurrahmanchowdhury/Documents/junit4)
#   JUNIT_SRC_ROOT      the Java source root            (default: $JUNIT_DIR/src/main/java)
#   SPECIMIN_DIR        Specimin checkout (has gradlew)  (default: ~/Documents/specimin)
#   JAR_PATH            where JUnit's compile-time dependency jars are copied
#                         (default: ~/junit-deps)
#   ERRORPRONE_VERSION  Error Prone core version         (default: 2.50.0)
#   GRADLE_DIST_VERSION Gradle distribution to force     (default: 8.7)
#   BUILD_DIR           scratch build dir                (default: $JUNIT_DIR/.nullaway-build)
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
#   ./GenerateNullAwayWarnings.sh                        # junit, the current default
#   JUNIT_DIR=~/code/junit4 ./GenerateNullAwayWarnings.sh
#   PROJECT=gson ./GenerateNullAwayWarnings.sh
#   PROJECT=gson GSON_DIR=~/code/gson ./GenerateNullAwayWarnings.sh
#   PROJECT=eventbus ./GenerateNullAwayWarnings.sh
#   PROJECT=eventbus EVENTBUS_DIR=~/code/EventBus ./GenerateNullAwayWarnings.sh

set -euo pipefail

PROJECT="${PROJECT:-junit}"
DIVIDER="$(printf '─%.0s' {1..60})"

if [[ "$PROJECT" == "junit" ]]; then
    # ── Config (JUnit 4 / Gradle) ────────────────────────────────────────────
    JUNIT_DIR="${JUNIT_DIR:-/Users/mushfiqurrahmanchowdhury/Documents/junit4}"
    JUNIT_SRC_ROOT="${JUNIT_SRC_ROOT:-$JUNIT_DIR/src/main/java}"
    SPECIMIN_DIR="${SPECIMIN_DIR:-$HOME/Documents/specimin}"
    JAR_PATH="${JAR_PATH:-$HOME/junit-deps}"
    OUT_DIR="${OUT_DIR:-$JUNIT_DIR}"
    # JUnit 4's main sources live in two package trees: org.junit.* and the
    # legacy junit.* (junit.framework, junit.runner, junit.textui, ...).
    ANNOTATED_PACKAGES="${ANNOTATED_PACKAGES:-org.junit,junit}"
    NULLAWAY_SEVERITY="${NULLAWAY_SEVERITY:-WARN}"
    # Same Error Prone/NullAway pair RunCheckerAll.sh uses on the slices, so
    # the original and sliced runs are directly comparable.
    ERRORPRONE_VERSION="${ERRORPRONE_VERSION:-2.50.0}"
    NULLAWAY_VERSION="${NULLAWAY_VERSION:-0.13.7}"
    GRADLE_DIST_VERSION="${GRADLE_DIST_VERSION:-8.7}"
    BUILD_DIR="${BUILD_DIR:-$JUNIT_DIR/.nullaway-build}"

    REPORT_FILE="$OUT_DIR/nullaway-report.txt"
    WARN_FILE="$OUT_DIR/nullaway-warnings.txt"

    # ── Preflight ─────────────────────────────────────────────────────────────
    if [[ ! -d "$JUNIT_SRC_ROOT" ]]; then
        echo "ERROR: JUnit source root not found: $JUNIT_SRC_ROOT" >&2
        echo "       Set JUNIT_DIR or JUNIT_SRC_ROOT." >&2
        exit 1
    fi
    if [[ ! -f "$SPECIMIN_DIR/gradlew" || ! -d "$SPECIMIN_DIR/gradle" ]]; then
        echo "ERROR: Gradle wrapper not found under $SPECIMIN_DIR (need gradlew + gradle/)." >&2
        echo "       Set SPECIMIN_DIR." >&2
        exit 1
    fi
    # error_prone_core 2.50.0's class files need a JDK 21+ runtime to load,
    # so a lower JDK fails every time -- see RunCheckerAll.sh.
    java_major="$(java -version 2>&1 | head -1 | sed -E 's/.*"([0-9]+)\..*/\1/; s/.*"([0-9]+)"/\1/')"
    if [[ "$java_major" =~ ^[0-9]+$ ]] && [[ "$java_major" -lt 21 ]]; then
        echo "ERROR: active Java is $java_major, but ERRORPRONE_VERSION=$ERRORPRONE_VERSION" >&2
        echo "       needs a JDK 21+ runtime. Switch first, e.g.:" >&2
        echo "         export JAVA_HOME=\$(/usr/libexec/java_home -v 21)" >&2
        exit 1
    fi

    # Resolve to an absolute path so Error Prone prints absolute file locations
    # (ExtractWarningMethods.py expects '/abs/path/File.java:LINE: ... [NullAway]').
    JUNIT_SRC_ROOT="$(cd "$JUNIT_SRC_ROOT" && pwd)"
    mkdir -p "$JAR_PATH"
    JAR_PATH="$(cd "$JAR_PATH" && pwd)"

    java_count=$(find "$JUNIT_SRC_ROOT" -name '*.java' | wc -l | tr -d ' ')
    echo "$DIVIDER"
    echo "Plain NullAway on JUnit 4"
    echo "  source root : $JUNIT_SRC_ROOT  ($java_count Java file(s))"
    echo "  packages    : $ANNOTATED_PACKAGES"
    echo "  severity    : $NULLAWAY_SEVERITY"
    echo "  build dir   : $BUILD_DIR"
    echo "  deps jars   : $JAR_PATH"
    echo "  Java        : $(java -version 2>&1 | head -1)"
    echo "$DIVIDER"

    # ── Build a self-contained NullAway project ──────────────────────────────
    mkdir -p "$BUILD_DIR/gradle/wrapper"
    cp "$SPECIMIN_DIR/gradlew"     "$BUILD_DIR/gradlew"
    cp "$SPECIMIN_DIR/gradlew.bat" "$BUILD_DIR/gradlew.bat" 2>/dev/null || true
    cp -r "$SPECIMIN_DIR/gradle/wrapper/." "$BUILD_DIR/gradle/wrapper/" 2>/dev/null || true
    chmod +x "$BUILD_DIR/gradlew"

    cat > "$BUILD_DIR/gradle/wrapper/gradle-wrapper.properties" <<EOF
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-${GRADLE_DIST_VERSION}-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
EOF

    cat > "$BUILD_DIR/settings.gradle" <<'EOF'
rootProject.name = 'junit-nullaway'
EOF

    # The source set points at the real JUnit sources (absolute path), so
    # nothing is copied and warnings reference the original files.
    #
    # disableAllChecks: JUnit 4 was never built with Error Prone, so its
    # default ERROR-level checks could fail the compile on findings that have
    # nothing to do with nullness. Only NullAway (re-enabled explicitly
    # below) runs. JSpecifyMode=false + RequireExplicitNullMarking OFF: JUnit
    # has no @NullMarked annotations -- same reasoning as the gson path.
    cat > "$BUILD_DIR/build.gradle" <<EOF
plugins {
    id 'java'
    id 'net.ltgt.errorprone' version '3.1.0'
}

repositories {
    mavenCentral()
}

dependencies {
    // JUnit 4's only compile-time dependency (see junit4/pom.xml). No
    // nullness-annotation jars (nullaway-annotations/jsr305/jspecify) are on
    // the compile classpath: JUnit's sources don't use them, and
    // nullaway-annotations 0.13.x is published for Java 11+, which Gradle
    // refuses to resolve against a classpath it targets at Java 8 (derived
    // from options.release = 8 below) -- "No matching variant of
    // com.uber.nullaway:nullaway-annotations ... compatible with Java 11 and
    // the consumer needed a component, compatible with Java 8". This also
    // keeps copyDeps from copying them into JAR_PATH.
    implementation       'org.hamcrest:hamcrest-core:1.3'
    errorprone           'com.google.errorprone:error_prone_core:${ERRORPRONE_VERSION}'
    annotationProcessor  'com.uber.nullaway:nullaway:${NULLAWAY_VERSION}'
}

sourceSets {
    main {
        java {
            srcDirs = ['${JUNIT_SRC_ROOT}']
            include 'org/**/*.java'
            include 'junit/**/*.java'
        }
    }
}

tasks.withType(JavaCompile).configureEach {
    options.release = 8
    options.errorprone {
        disableAllChecks = true
        check('NullAway', net.ltgt.gradle.errorprone.CheckSeverity.${NULLAWAY_SEVERITY})
        check('RequireExplicitNullMarking', net.ltgt.gradle.errorprone.CheckSeverity.OFF)
        option('NullAway:AnnotatedPackages', '${ANNOTATED_PACKAGES}')
        option('NullAway:JSpecifyMode', 'false')
    }
    // Error Prone on JDK 21 requires these -- see RunCheckerAll.sh.
    options.compilerArgs << '-XDcompilePolicy=simple' << '--should-stop=ifError=FLOW'
    options.compilerArgs << '-XDaddTypeAnnotationsToSymbol=true'
    // Don't let a flood of warnings get truncated, and don't fail the build on them.
    options.compilerArgs << '-Xmaxwarns' << '100000'
}

// Copies JUnit's compile-time dependency jars (hamcrest-core) into JAR_PATH,
// for Specimin's --jarPath and the slice checks in RunCheckerAll.sh /
// RunCFCheckerAll.sh.
tasks.register('copyDeps', Copy) {
    from configurations.compileClasspath
    into '${JAR_PATH}'
}
EOF

    # ── Run ───────────────────────────────────────────────────────────────────
    echo "Copying compile-time dependencies into $JAR_PATH ..."
    ( cd "$BUILD_DIR" && ./gradlew --no-daemon -q copyDeps ) || {
        echo "ERROR: could not copy JUnit's dependency jars into $JAR_PATH." >&2
        exit 1
    }

    echo "Compiling with NullAway (output -> $REPORT_FILE) ..."
    mkdir -p "$OUT_DIR"
    ( cd "$BUILD_DIR" && ./gradlew --no-daemon clean compileJava ) > "$REPORT_FILE" 2>&1 || true

    # ── Extract warnings ──────────────────────────────────────────────────────
    grep -E '\[NullAway\]' "$REPORT_FILE" > "$WARN_FILE" || true
    warn_count="$(grep -c '\[NullAway\]' "$WARN_FILE" 2>/dev/null || true)"
    warn_count="${warn_count:-0}"

    echo ""
    echo "$(printf '═%.0s' {1..60})"
    echo "Done."
    echo "  Full report : $REPORT_FILE"
    echo "  Warnings    : $WARN_FILE  ($warn_count NullAway warning(s))"
    echo "  Deps jars   : $JAR_PATH"
    if [[ "$warn_count" -eq 0 ]]; then
        echo ""
        echo "  No [NullAway] lines found. Check $REPORT_FILE — the most common causes"
        echo "  are a compile error in the build (search the report for 'error:') or a"
        echo "  missing network connection for the first download."
    fi
    exit 0
fi

if [[ "$PROJECT" == "gson" ]]; then
    # ── Config (Gson / Maven) ────────────────────────────────────────────────
    GSON_DIR="${GSON_DIR:-$HOME/Documents/gson}"
    GSON_MODULE_DIR="${GSON_MODULE_DIR:-$GSON_DIR/gson}"
    OUT_DIR="${OUT_DIR:-$GSON_DIR}"
    ANNOTATED_PACKAGES="${ANNOTATED_PACKAGES:-com.google.gson}"
    NULLAWAY_SEVERITY="${NULLAWAY_SEVERITY:-WARN}"
    # 0.10.10 (used by the EventBus/Gradle path below) predates gson's much
    # newer error_prone_core (2.49.0) and reaches into internal Error Prone
    # classes that no longer exist there, causing NoClassDefFoundError.
    NULLAWAY_VERSION="${NULLAWAY_VERSION:-0.13.7}"

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

    # gson/pom.xml's own `disable-error-prone` profile auto-activates for any
    # JDK below 21 and wholesale overrides (combine.self="override") the
    # maven-compiler-plugin's compilerArgs/annotationProcessorPaths -- which
    # strips Error Prone (and NullAway with it) entirely, even with
    # -Pnullaway passed. The build still succeeds (a plain javac compile),
    # so nullaway-warnings.txt silently comes back empty instead of erroring.
    # Warn about this up front instead of letting it look like a clean run.
    java_major="$(java -version 2>&1 | head -1 | sed -E 's/.*"([0-9]+)\..*/\1/; s/.*"([0-9]+)"/\1/')"
    if [[ "$java_major" =~ ^[0-9]+$ ]] && [[ "$java_major" -lt 21 ]]; then
        echo "WARNING: active Java is $java_major, but gson's own pom.xml disables Error" >&2
        echo "         Prone (and therefore NullAway) below Java 21 via its" >&2
        echo "         'disable-error-prone' profile. The build below will likely succeed" >&2
        echo "         but run zero checks, making nullaway-warnings.txt empty regardless" >&2
        echo "         of what's actually in gson's sources." >&2
        echo "         Switch to Java 21-25 first, e.g.:" >&2
        echo "           export JAVA_HOME=\$(/usr/libexec/java_home -v 21)" >&2
        echo "$DIVIDER" >&2
    fi

    # ── Run ───────────────────────────────────────────────────────────────────
    # `clean` avoids Maven's incremental compiler skipping already-up-to-date
    # sources, which would otherwise silently produce zero NullAway warnings.
    mkdir -p "$OUT_DIR"
    echo "Compiling with NullAway (output -> $REPORT_FILE) ..."
    # JSpecifyMode=false + RequireExplicitNullMarking:OFF: gson has no @NullMarked
    # annotations anywhere, and NullAway's JSpecify mode (default since ~0.11) refuses
    # to analyze anything without them, only emitting a RequireExplicitNullMarking
    # advisory instead. Force the legacy AnnotatedPackages heuristic so NullAway
    # actually analyzes $ANNOTATED_PACKAGES for real nullability issues.
    ( cd "$GSON_MODULE_DIR" && mvn -Pnullaway \
        -Dnullaway.version="$NULLAWAY_VERSION" \
        -Dnullaway.checks="-Xep:NullAway:$NULLAWAY_SEVERITY -XepOpt:NullAway:AnnotatedPackages=$ANNOTATED_PACKAGES -XepOpt:NullAway:JSpecifyMode=false -Xep:RequireExplicitNullMarking:OFF" \
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
        echo "  are: a Java version below 21 (gson's own 'disable-error-prone' profile"
        echo "  silently strips Error Prone/NullAway below Java 21 -- see the warning"
        echo "  above if one was printed), the 'nullaway' Maven profile not being set up"
        echo "  in gson/pom.xml yet, or a missing network connection for the first"
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
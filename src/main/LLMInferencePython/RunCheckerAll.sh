#!/usr/bin/env bash
# RunCheckerAll.sh
#
# Replaces RunNullAwayAll.sh now that warningMethods.txt (and so the slices in
# SPECIMIN_OUT) can come from the Index Checker's warnings as well as
# NullAway's. For every Specimin slice folder in SPECIMIN_OUT, runs BOTH
# checkers on that slice:
#
#   NullAway:      injects a throwaway Gradle project (settings.gradle +
#                  build.gradle) into the slice, pointing its source set at
#                  the slice itself, and runs `./gradlew clean compileJava`
#                  with NullAway enabled via Error Prone. Writes
#                  nullaway-report.txt / nullaway-warnings.txt inside the
#                  slice folder. The Gradle wrapper is copied from
#                  SPECIMIN_DIR (gson itself is a Maven project and has no
#                  wrapper of its own to copy).
#   Index Checker: runs "$CF_HOME/checker/bin/javac -processor index"
#                  directly on the slice's .java files -- the same mechanism
#                  as GenerateIndexCheckerWarnings.sh. Writes
#                  index-checker-out.log / index-checker-warnings.log inside
#                  the slice folder.
#
# Requirements:
#   - Java 17+ active: export JAVA_HOME=$(/usr/libexec/java_home -v 17)
#   - CF_HOME pointing at a built Checker Framework checkout
#     (i.e. "$CF_HOME/checker/bin/javac" must exist)
#   - JAR_PATH populated with gson's compile-time dependency jars, e.g.:
#       mvn dependency:copy-dependencies -DincludeScope=compile -DoutputDirectory=~/gson-deps
#     (run from the gson module directory; see RunSpeciminAll.py's header)
#
# Everything is configurable via environment variables; the defaults match
# the rest of the pipeline.
#
#   SPECIMIN_OUT        slice folders to check          (default: $GSON_DIR/specimin-out)
#   GSON_DIR            root of your gson checkout       (default: ~/Documents/gson)
#   SPECIMIN_DIR        Specimin checkout (has gradlew)  (default: ~/Documents/specimin)
#   JAR_PATH            gson's compile-time dependency jars (default: ~/gson-deps)
#   CF_HOME             Checker Framework checkout (required)
#   ANNOTATED_PACKAGES  NullAway:AnnotatedPackages       (default: com.google.gson)
#   NULLAWAY_SEVERITY   WARN or ERROR                    (default: WARN)
#   ERRORPRONE_VERSION  Error Prone core version         (default: 2.49.0)
#   NULLAWAY_VERSION    NullAway version                 (default: 0.13.7)
#   GRADLE_DIST_VERSION Gradle distribution to force      (default: 8.7)
#   INDEX_MAXWARNS      Index Checker -Xmaxwarns          (default: 100000)
#   INDEX_MAXERRS       Index Checker -Xmaxerrs           (default: 100000)
#
# Usage:
#   CF_HOME=~/checker-framework ./RunCheckerAll.sh

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
GSON_DIR="${GSON_DIR:-$HOME/Documents/gson}"
SPECIMIN_OUT="${SPECIMIN_OUT:-$GSON_DIR/specimin-out}"
SPECIMIN_DIR="${SPECIMIN_DIR:-$HOME/Documents/specimin}"
JAR_PATH="${JAR_PATH:-$HOME/gson-deps}"
CF_HOME="${CF_HOME:-}"
ANNOTATED_PACKAGES="${ANNOTATED_PACKAGES:-com.google.gson}"
NULLAWAY_SEVERITY="${NULLAWAY_SEVERITY:-WARN}"
ERRORPRONE_VERSION="${ERRORPRONE_VERSION:-2.49.0}"
NULLAWAY_VERSION="${NULLAWAY_VERSION:-0.13.7}"
GRADLE_DIST_VERSION="${GRADLE_DIST_VERSION:-8.7}"
INDEX_MAXWARNS="${INDEX_MAXWARNS:-100000}"
INDEX_MAXERRS="${INDEX_MAXERRS:-100000}"

GRADLE_WRAPPER_SRC="$SPECIMIN_DIR"   # contains gradlew, gradlew.bat, gradle/

DIVIDER="$(printf '─%.0s' {1..60})"

# ── Preflight checks ───────────────────────────────────────────────────────────
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

    # build.gradle -- NullAway via Error Prone, source set is the slice itself.
    # JSpecifyMode=false + RequireExplicitNullMarking:OFF: gson has no
    # @NullMarked annotations anywhere, and NullAway's JSpecify mode (default
    # since ~0.11) would otherwise only emit a "please annotate" advisory
    # instead of actually analyzing the slice. See gson/pom.xml's `nullaway`
    # profile for the matching full-module configuration.
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
            include 'com/**/*.java'
        }
    }
}

tasks.withType(JavaCompile).configureEach {
    options.errorprone {
        check('NullAway', net.ltgt.gradle.errorprone.CheckSeverity.${NULLAWAY_SEVERITY})
        option('NullAway:AnnotatedPackages', '${ANNOTATED_PACKAGES}')
        option('NullAway:JSpecifyMode', 'false')
        check('RequireExplicitNullMarking', net.ltgt.gradle.errorprone.CheckSeverity.OFF)
    }
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

    grep -E "NullAway|warning:|error:" "$report" > "$warnings" || true

    local warn_count
    warn_count="$(grep -c "NullAway" "$warnings" 2>/dev/null || true)"
    warn_count="${warn_count:-0}"

    if [[ "$warn_count" -eq 0 ]]; then
        echo "  NullAway warnings      : none"
    else
        echo "  NullAway warnings      : $warn_count"
    fi
    echo "  Full report saved      : nullaway-report.txt"
    echo "  Warnings saved         : nullaway-warnings.txt"
}

run_index_checker() {
    local dir="$1"
    local out="$dir/index-checker-out.log"
    local warnings="$dir/index-checker-warnings.log"
    local classes_out
    classes_out="$(mktemp -d)"
    local sources_file
    sources_file="$(mktemp)"

    echo "  Running Index Checker..."
    find "$dir" -name '*.java' ! -name 'module-info.java' > "$sources_file"

    local cp=""
    if compgen -G "$JAR_PATH/*.jar" > /dev/null; then
        cp="$(find "$JAR_PATH" -name '*.jar' | paste -sd: -)"
    fi

    local javac_args=(
        -processor index
        -d "$classes_out"
        -Xmaxwarns "$INDEX_MAXWARNS"
        -Xmaxerrs "$INDEX_MAXERRS"
    )
    if [[ -n "$cp" ]]; then
        javac_args+=(-classpath "$cp")
    fi
    javac_args+=("@$sources_file")

    "$CHECKER_JAVAC" "${javac_args[@]}" > "$out" 2> "$warnings" || true
    rm -f "$sources_file"
    rm -rf "$classes_out"

    local warn_count
    warn_count="$(grep -cE ': (error|warning): \[' "$warnings" 2>/dev/null || true)"
    warn_count="${warn_count:-0}"

    if [[ "$warn_count" -eq 0 ]]; then
        echo "  Index Checker findings : none"
    else
        echo "  Index Checker findings : $warn_count"
    fi
    echo "  Full output saved      : index-checker-out.log"
    echo "  Findings saved         : index-checker-warnings.log"
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

    # The Index Checker run never fails the script (matches
    # GenerateIndexCheckerWarnings.sh: findings are expected, not a build
    # failure); only NullAway's real `compileJava` result counts as pass/fail,
    # since that reflects whether the slice actually compiles.
    if ( cd "$dir" && run_nullaway "$dir" ); then
        passed=$(( passed + 1 ))
    else
        failed=$(( failed + 1 ))
        failed_dirs+=("$name")
    fi
    run_index_checker "$dir"
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
echo "Warnings per folder:"
for dir in "$SPECIMIN_OUT"/*/; do
    [[ -d "$dir" ]] || continue
    nfile="$dir/nullaway-warnings.txt"
    ifile="$dir/index-checker-warnings.log"
    ncount=0
    icount=0
    if [[ -f "$nfile" ]]; then
        ncount="$(grep -c "NullAway" "$nfile" 2>/dev/null || true)"
        ncount="${ncount:-0}"
    fi
    if [[ -f "$ifile" ]]; then
        icount="$(grep -cE ': (error|warning): \[' "$ifile" 2>/dev/null || true)"
        icount="${icount:-0}"
    fi
    if [[ -f "$nfile" || -f "$ifile" ]]; then
        printf "  %-40s NullAway: %-4s  Index Checker: %s\n" "$(basename "$dir")" "$ncount" "$icount"
    fi
done

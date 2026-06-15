#!/usr/bin/env bash
# RunNullAwayAll.sh
#
# For every subdirectory in SPECIMIN_OUT:
#   1. Injects a settings.gradle and build.gradle configured for NullAway
#   2. Copies the Gradle wrapper from EventBus if not already present
#   3. Runs ./gradlew clean compileJava and saves the full output to
#      nullaway-report.txt inside the folder
#   4. Extracts only NullAway/compiler warnings to nullaway-warnings.txt

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────────
SPECIMIN_OUT="/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out"
EVENTBUS_DIR="/Users/mushfiqurrahmanchowdhury/Documents/EventBus"
JAR_PATH="$HOME/eventbus-deps"

GRADLE_WRAPPER_SRC="$EVENTBUS_DIR"   # contains gradlew, gradlew.bat, gradle/

DIVIDER="$(printf '─%.0s' {1..60})"

# ── Preflight checks ───────────────────────────────────────────────────────────
for path in "$SPECIMIN_OUT" "$EVENTBUS_DIR" "$JAR_PATH"; do
    if [[ ! -d "$path" ]]; then
        echo "ERROR: required directory not found: $path"
        exit 1
    fi
done

# ── Helpers ────────────────────────────────────────────────────────────────────

inject_gradle_files() {
    local dir="$1"

    # settings.gradle
    cat > "$dir/settings.gradle" <<'EOF'
rootProject.name = 'specimin-nullaway-check'
EOF

    # build.gradle — excludes javax/** (provided by jsr305 jar)
    cat > "$dir/build.gradle" <<'EOF'
plugins {
    id 'java'
    id 'net.ltgt.errorprone' version '3.1.0'
}

repositories {
    mavenCentral()
}

dependencies {
    errorprone 'com.google.errorprone:error_prone_core:2.26.1'
    annotationProcessor 'com.uber.nullaway:nullaway:0.10.10'
    compileOnly 'com.uber.nullaway:nullaway-annotations:0.10.10'
    compileOnly 'com.google.code.findbugs:jsr305:3.0.2'
    compileOnly 'org.jspecify:jspecify:0.3.0'
}

sourceSets {
    main {
        java {
            srcDirs = ['.']
            include 'org/**/*.java'
        }
    }
}

tasks.withType(JavaCompile).configureEach {
    options.errorprone {
        check('NullAway', net.ltgt.gradle.errorprone.CheckSeverity.WARN)
        option('NullAway:AnnotatedPackages', 'org.greenrobot.eventbus')
    }
    options.compilerArgs << '-Xmaxwarns' << '10000'
}
EOF
}

copy_gradle_wrapper() {
    local dir="$1"
    if [[ ! -f "$dir/gradlew" ]]; then
        cp "$GRADLE_WRAPPER_SRC/gradlew"     "$dir/gradlew"
        cp "$GRADLE_WRAPPER_SRC/gradlew.bat" "$dir/gradlew.bat"
        cp -r "$GRADLE_WRAPPER_SRC/gradle"   "$dir/gradle"
        chmod +x "$dir/gradlew"
    fi
}

run_nullaway() {
    local dir="$1"
    local report="$dir/nullaway-report.txt"
    local warnings="$dir/nullaway-warnings.txt"

    echo "  Running NullAway..."
    # Upgrade Gradle wrapper to 8.7 so it runs under Java 17
    WRAPPER_PROPS="$PROJECT_DIR/gradle/wrapper/gradle-wrapper.properties"
    if [ -f "$WRAPPER_PROPS" ]; then
        sed -i '' \
        's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-8.7-all.zip|' \
        "$WRAPPER_PROPS"
    fi
    # Capture full output; do NOT let a non-zero exit abort the script here
    ./gradlew clean compileJava 2>&1 | tee "$report" || true

    # Extract only relevant lines into the warnings file
    grep -E "NullAway|warning:|error:" "$report" > "$warnings" || true

    local warn_count
    warn_count=$(grep -c "NullAway" "$warnings" 2>/dev/null || echo 0)

    if [[ "$warn_count" -eq 0 ]]; then
        echo "  NullAway warnings : none"
    else
        echo "  NullAway warnings : $warn_count"
        cat "$warnings"
    fi

    echo "  Full report saved : nullaway-report.txt"
    echo "  Warnings saved    : nullaway-warnings.txt"
}

# ── Main loop ──────────────────────────────────────────────────────────────────
total=0
passed=0
failed=0
failed_dirs=()

for dir in "$SPECIMIN_OUT"/*/; do
    # Skip if not a real directory or contains no Java sources
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

    # Run NullAway from inside the directory
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
    wfile="$dir/nullaway-warnings.txt"
    if [[ -f "$wfile" ]]; then
        count=$(grep -c "NullAway" "$wfile" 2>/dev/null || echo 0)
        printf "  %-40s %s warning(s)\n" "$(basename "$dir")" "$count"
    fi
done

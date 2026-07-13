#!/usr/bin/env bash
# RunPipeline.sh
# Runs the full null-inference pipeline from the LLMInferencePython directory.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SPECIMIN_ROOT="$(cd "$DIR/../../.." && pwd)"

echo "═══════════════════════════════════════════════════════════"
echo "[1/8] Extracting warning methods from NullAway warnings..."
echo "═══════════════════════════════════════════════════════════"
( cd "$SPECIMIN_ROOT" && ./gradlew --no-daemon extractWarningMethods \
    -Psrc="${EVENTBUS_SRC_ROOT:-/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src}" \
    -PnullawayWarnings="${NULLAWAY_WARNINGS_FILE:-/Users/mushfiqurrahmanchowdhury/Documents/EventBus/nullaway-warnings.txt}" \
    -Poutput="${WARNING_METHODS_FILE:-/Users/mushfiqurrahmanchowdhury/Documents/EventBus/warningMethods.txt}" )

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[2/8] Running Specimin on all @NullUnmarked locations..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/RunSpeciminAll.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[3/8] Removing spurious Specimin '= null' field stubs..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/FixSpeciminNullInits.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[4/8] Removing @NullUnmarked annotations..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/RemoveNullUnmarked.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[5/8] Running NullAway on reduced programs..."
echo "═══════════════════════════════════════════════════════════"
bash "$DIR/RunNullAwayAll.sh"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[6/8] Running LLM null inference (Groq)..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/RunLLMInferenceAll.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[7/8] Injecting missing javax.annotation imports..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/AddNonnullImport.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[8/8] Re-running NullAway to verify final annotations..."
echo "═══════════════════════════════════════════════════════════"
bash "$DIR/RunNullAwayAll.sh"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "Pipeline complete."
echo "═══════════════════════════════════════════════════════════"
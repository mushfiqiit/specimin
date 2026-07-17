#!/usr/bin/env bash
# RunPipeline.sh
# Runs the full null-inference pipeline from the LLMInferencePython directory.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "═══════════════════════════════════════════════════════════"
echo "[1/8] Extracting warning methods from NullAway warnings..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/ExtractWarningMethods.py"

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
echo "[5/8] Running NullAway + Index Checker on reduced programs..."
echo "═══════════════════════════════════════════════════════════"
bash "$DIR/RunCheckerAll.sh"

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
echo "[8/8] Re-running NullAway + Index Checker to verify final annotations..."
echo "═══════════════════════════════════════════════════════════"
bash "$DIR/RunCheckerAll.sh"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "Pipeline complete."
echo "═══════════════════════════════════════════════════════════"
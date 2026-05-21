#!/usr/bin/env bash
# RunPipeline.sh
# Runs the full null-inference pipeline from the LLMInferencePython directory.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "═══════════════════════════════════════════════════════════"
echo "[1/7] Running Specimin on all @NullUnmarked locations..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/RunSpeciminAll.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[2/7] Removing spurious Specimin '= null' field stubs..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/FixSpeciminNullInits.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[3/7] Removing @NullUnmarked annotations..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/RemoveNullUnmarked.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[4/7] Running NullAway on reduced programs..."
echo "═══════════════════════════════════════════════════════════"
bash "$DIR/RunNullAwayAll.sh"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[5/7] Running LLM null inference (Groq)..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/RunLLMInferenceAll.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[6/7] Injecting missing javax.annotation imports..."
echo "═══════════════════════════════════════════════════════════"
python3 "$DIR/AddNonnullImport.py"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "[7/7] Re-running NullAway to verify final annotations..."
echo "═══════════════════════════════════════════════════════════"
bash "$DIR/RunNullAwayAll.sh"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "Pipeline complete."
echo "═══════════════════════════════════════════════════════════"

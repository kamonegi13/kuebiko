#!/bin/bash
# RunPod のサーバ上で動く入口 (2026-09-27)。runpod_ctl.py launch が起動時のコマンドとして渡す。
#
# 保証すること:
#   - 成功でも失敗でも、最後に **自分自身を削除する** (GPU の課金を止める)
#   - 上限時間 (MAX_HOURS) を超えたら強制終了して削除する
#   - 結果と完了の印 (DONE.json / FAILED.txt) を保存領域 (/workspace) に書く → 手元から S3 API で確認
#
# 環境変数 (runpod_ctl.py が渡す): RUN_ID, TRAIN_ARGS, MAX_HOURS (HF_TOKEN は任意),
#   RUNPOD_POD_ID / RUNPOD_API_KEY (RunPod が注入)
set -uo pipefail
RUN_DIR=/workspace/runs/$RUN_ID
OUT=$RUN_DIR/out
LOG=$RUN_DIR/pod.log
mkdir -p "$OUT"
exec > >(tee -a "$LOG") 2>&1
say() { echo "[$(date -u +%H:%M:%S)] $*"; }

terminate() {
  say "サーバを削除 (課金停止)"
  sync
  if command -v runpodctl >/dev/null 2>&1; then
    runpodctl remove pod "$RUNPOD_POD_ID" && return
  fi
  curl -s -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" \
    "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID" || true
}

fail() {
  say "失敗: $1"
  echo "$1" > "$RUN_DIR/FAILED.txt"
  terminate
  exit 1
}
trap 'fail "予期しない終了 (行 $LINENO)"' ERR

say "開始 run=$RUN_ID 上限 ${MAX_HOURS} 時間"
nvidia-smi --query-gpu=name,memory.total --format=csv || true
pip install -q "transformers==5.17.0" "peft==0.21.0" "accelerate" "safetensors" || fail "pip install"
export HF_HOME=/root/hf
# 確保済みで未使用の断片を減らす (試験走行で 5.6GB が断片化していた)
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
python -c "import torch; assert torch.cuda.is_available()" || fail "CUDA が使えない"

say "元のモデルを取得"
# Gemma 4 は Apache 2.0・アクセス制限なし (2026-09-27 確認) → トークン不要。設定されていれば使う
# CLI (huggingface-cli → hf) は名前が変わるので、Python の関数で取得する (2026-09-27 に 1 回目が失敗)
python - <<'PY' || fail "元のモデルの取得"
import os
from huggingface_hub import snapshot_download
tok = os.environ.get("HF_TOKEN") or None
if tok and "RUNPOD_SECRET" in tok:
    tok = None
snapshot_download("google/gemma-4-26B-A4B-it", ignore_patterns=["*.gguf"], token=tok)
PY

say "学習"
# shellcheck disable=SC2086
timeout "${MAX_HOURS}h" python "$RUN_DIR/code/train_lora.py" \
  --model google/gemma-4-26B-A4B-it --data "$RUN_DIR/data" --out "$OUT" $TRAIN_ARGS
rc=$?
if [ $rc -eq 124 ]; then fail "上限時間 ${MAX_HOURS} 時間を超えた"; fi
if [ $rc -ne 0 ]; then fail "学習が異常終了 (終了コード $rc)"; fi
cp "$OUT/DONE.json" "$RUN_DIR/DONE.json"
say "完了"
trap - ERR
terminate

#!/bin/bash
# RunPod のサーバ上で動く入口 (2026-09-27)。runpod_ctl.py launch が起動時のコマンドとして渡す。
#
# 保証すること:
#   - 成功でも失敗でも、最後に **自分自身を削除する** (GPU の課金を止める)
#   - 上限時間 (MAX_HOURS) を超えたら強制終了して削除する
#   - 結果と完了の印 (DONE.json / FAILED.txt) を保存領域 (/workspace) に書く → 手元から S3 API で確認
#   - ⚠ 学習の結果はサーバ内の一時ディスクに書き、**S3 の窓口経由で**保存領域へ書き出す (2026-09-29)。
#     保存領域へ直接書いた大きなファイルは、S3 の窓口が照合値の計算に 100 秒を超えて Cloudflare に
#     524 で打ち切られ、二度と読めなかった (n19b の 7.9GB)。窓口経由で書けば照合値が保存される
#
# 環境変数 (runpod_ctl.py が渡す): RUN_ID, TRAIN_ARGS, MAX_HOURS, S3_KEY, S3_SECRET, VOLUME_ID,
#   DATACENTER (HF_TOKEN は任意),
#   RUNPOD_POD_ID / RUNPOD_API_KEY (RunPod が注入)
set -uo pipefail
RUN_DIR=/workspace/runs/$RUN_ID
OUT=/root/out
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
say "学習 完了 → S3 の窓口経由で書き出し"
mkdir -p "/workspace/backup/$RUN_ID" && cp -r "$OUT/." "/workspace/backup/$RUN_ID/" || say "⚠ 予備の複製に失敗 (続行)"
upload() {
python - <<'PY'
import os, boto3
from boto3.s3.transfer import TransferConfig
dc = os.environ["DATACENTER"]
c = boto3.client("s3", endpoint_url=f"https://s3api-{dc.lower()}.runpod.io/", region_name=dc,
    aws_access_key_id=os.environ["S3_KEY"], aws_secret_access_key=os.environ["S3_SECRET"])
cfg = TransferConfig(max_concurrency=8, multipart_chunksize=64 * 1024 * 1024)
out, run = "/root/out", os.environ["RUN_ID"]
for root, _, files in os.walk(out):
    for f in files:
        path = os.path.join(root, f)
        rel = os.path.relpath(path, out)
        if rel.startswith("adapter-last/"):
            continue
        c.upload_file(path, os.environ["VOLUME_ID"], f"runs/{run}/out/{rel}", Config=cfg)
        print("書き出し", rel, flush=True)
# 完了の印は最後に書く (手元の watch はこれを見て取り出しを始める)
c.upload_file(f"{out}/DONE.json", os.environ["VOLUME_ID"], f"runs/{run}/DONE.json")
PY
}
pip install -q boto3 || true
ok=0
for i in 1 2 3; do upload && { ok=1; break; }; say "⚠ 書き出し失敗 ($i 回目)"; sleep 30; done
trap - ERR
if [ $ok -ne 1 ]; then
  echo "書き出し失敗 — 予備は /workspace/backup/$RUN_ID" > "$RUN_DIR/FAILED.txt"
  say "⚠ 書き出しに失敗。救出用に 1 時間サーバを残す"; sleep 3600
fi
say "完了"
terminate

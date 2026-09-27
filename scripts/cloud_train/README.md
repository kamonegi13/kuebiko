# クラウド GPU (RunPod) での LoRA 学習 (2026-09-27)

手元の MLX では 1 回の学習に約 22 時間かかる (s20: 1,410 万トークン)。RunPod の H200 で学習し、
**GPU の課金を学習の間だけに絞る** 経路。設計の背景は docs/research/llm_training/training_recipe_v2.md。

```
[手元]                                   [RunPod]
 upload  学習データとコード (S3 API) ─→  保存領域 (Network Volume)
 launch  サーバを起動 ─────────────────→  元のモデルを HF から取得 → 学習 → out/ に保存
                                          DONE.json を書いて **自分で削除** (課金停止)
 watch   完了の印を見たら確実に削除し、 ←─ 保存領域から S3 API で取り出す (サーバ不要)
         アダプタ (約 5GB) を取得
 merge   手元で元のモデルと統合 → ollama create --quantize int4
```

## 初回の準備 (利用者)

1. **Hugging Face**: 不要 (Gemma 4 は Apache 2.0・アクセス制限なし。2026-09-27 に未ログインで重みの
   取得を確認)
2. **RunPod** (Settings / Storage):
   - **Network Volume** を作る (20GB で十分。**S3 API 対応のデータセンター** を選ぶ:
     EU-CZ-1 / EU-RO-1 / EUR-IS-1 / EUR-NO-1 / US-CA-2 / US-GA-2 / US-IL-1 / US-KS-2 / US-MD-1 /
     US-MO-1 / US-MO-2 / US-NC-1 / US-NC-2 / US-NE-1 / US-WA-1。**H200 の在庫があるところ**)
   - **API Key** を作る (Settings → API Keys。権限は Read/Write)
   - **S3 API Key** を作る (Settings → S3 API Keys。`user_…` と `rps_…` は一度しか表示されない)
3. **.env** (git 対象外) に追記する — 値はここにだけ置く:
   ```
   RUNPOD_API_KEY=...
   RUNPOD_S3_ACCESS_KEY=user_...
   RUNPOD_S3_SECRET=rps_...
   RUNPOD_VOLUME_ID=...          # Network Volume の ID
   RUNPOD_DATACENTER=EU-RO-1     # Network Volume のデータセンター
   ```
4. 手元の環境 (一度だけ。作成済み): `data/cloud-venv` (arm64 Python 3.12 + torch / transformers 5.17 /
   peft 0.21 / boto3)、統合用の元のモデル `data/cloud-base-hf` (MLX bf16 のキャッシュから変換済み)

## 1 回の学習

```bash
PY=data/cloud-venv/bin/python; CTL=scripts/cloud_train/runpod_ctl.py; RUN=bench-01
$PY $CTL upload $RUN --data data/mlx/dataset_s20
# まず試験走行 (30 更新) で速度とメモリを実測する
$PY $CTL launch $RUN --gpu "NVIDIA H200" --max-hours 1 --train-args "--max-updates 30"
$PY $CTL watch $RUN        # 完了まで待ち、サーバを削除し、結果を data/cloud-runs/$RUN/ へ
```

本番の学習は `--train-args` に設定を渡す (既定は s20 と同じ: rank 32 / scale 10 / 30 層 / lr 3e-5 /
warmup 60 / accum 4 / 1 周)。v2 の設定例: `--lr 2e-5` (router は既定で対象外。s20 と揃えるなら `--router`)。

取り込み:
```bash
$PY scripts/cloud_train/merge_adapter.py --base data/cloud-base-hf \
    --adapter data/cloud-runs/$RUN/out/adapter --out data/cloud-merged/$RUN --check
printf 'FROM %s/data/cloud-merged/%s\nPARAMETER temperature 1\nPARAMETER top_k 64\nPARAMETER top_p 0.95\n' \
    "$PWD" "$RUN" >| data/cloud-merged/Modelfile.$RUN
ollama create kuebiko-sft:$RUN -f data/cloud-merged/Modelfile.$RUN --quantize int4
rm -rf data/cloud-merged/$RUN; data/mlx/venv/bin/python data/mlx/prune_ollama_orphans.py
```

## 安全装置

| 事故 | 防ぎ方 |
|---|---|
| 学習後もサーバが動き続ける | pod_run.sh が成功でも失敗でも自分を削除 + watch が完了の印を見たら削除 + `stop-all` |
| 無限ループ・停滞 | `--max-hours` を超えたら強制終了して削除 |
| 学習例 0 件のまま「完了」 | train_lora.py が 0 件なら異常終了する |
| 学習と本番の入力の形のずれ | train_lora.py は Ollama の `RENDERER gemma4` (空の思考欄なし) と同じ形で組み立てる |
| 持ち帰りに GPU の課金 | 持ち帰りは S3 API (サーバ不要)。途中で切れても残りだけ取り直す |

## 確認済み (手元の CPU・極小モデル)

- PEFT の `target_parameters` で専門家の 3 次元の重みに **専門家ごと** の LoRA がかかり、勾配が流れる
- 損失の配分 (例ごとの平均・均等な勾配蓄積) と学習率の形は MLX と同じ
- 統合前後の出力の差 6.5e-7 (統合は正しい)
- `data/cloud-base-hf` の 1,013 テンソルが Google の元のモデルと名前・形とも一致

## 未確認 (初回の実行で確かめる)

- H200 のメモリで足りるか (試験走行の `peak_mem_gb`)・実際の速度 (`tok_per_s`)
- RunPod のサーバ内で `runpodctl` / `RUNPOD_API_KEY` による自己削除が効くか (効かなくても watch が消す)
- `DEFAULT_IMAGE` の PyTorch イメージのタグが現存するか
- MLX との違い: HF の専門家は gate と up が 1 本なので LoRA を共有する (MLX は別々)。初回は s20 と同じ
  データ・設定で学習し、**基盤の違いの較正** として s20 と比べる

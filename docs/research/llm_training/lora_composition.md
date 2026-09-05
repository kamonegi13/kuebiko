# LoRA アダプタの分離・合成・切り替え (調査: 2026-09-05)

> ⚠ 出典は調査時に収集したもの。筆者が独立に再検証したものではない。

## ⭐⭐ 結論: ルータ方式は **この構成では採用できない**

MoLE / LoRAMoE / MoLoRA / AdaMoLE / MixLoRA / X-LoRA のいずれも、**mlx-lm + Ollama では
動かない**。理由は共通で、推論時に各層でゲート計算を行う必要があり、

- **llama.cpp / Ollama は静的な加算 LoRA しか理解しない**。`--lora` で複数ロードや
  `/lora-adapters` での hot-swap はできるが、**学習されたルータで per-token に混ぜる概念が無い**
  ([ollama#5788](https://github.com/ollama/ollama/issues/5788) /
  [gguf-my-lora](https://huggingface.co/blog/ngxson/gguf-my-lora))
- **mlx-lm は「1 アダプタ学習 → 1 アダプタ推論 → fuse」のみ**。多アダプタ切替もルーティングも
  ドキュメント上存在しない ([LORA.md](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md))
- 実装は軒並み `transformers` / `peft` の**フォーク改造**か、専用推論エンジン
  (m-LoRA / Mistral.rs) を前提とする。GGUF への出力経路が無い

⭐ 傍証として **ExpertWeave** ([arXiv:2508.17624](https://arxiv.org/abs/2508.17624)) が
「既存の多アダプタ配信は **MoE ベースのモデルとは非互換**」と明言し、CUDA 環境ですら
未解決問題として扱っている (コード未公開)。**CUDA で未解決のものが Apple Silicon +
GGUF で動く見込みは無い。**

## 手法ごとの要点 (採用しない前提での記録)

| 手法 | ルーティング粒度 | vs 素の LoRA | リポジトリの状態 |
|---|---|---|---|
| [MoLE](https://arxiv.org/abs/2404.13628) | 層単位・凍結 LoRA を事後融合 | +2〜3pt | 公式リポ無し |
| [LoRAMoE](https://arxiv.org/abs/2312.09979) | token 単位・同時学習 | 知識 QA で最大 +64% | 404★・2024-04 で停止 |
| [MoLoRA/MoV](https://arxiv.org/abs/2309.05444) | token 単位 | +5.7pt (単一 LoRA 比) | 公式リポ未確認 |
| [AdaMoLE](https://arxiv.org/abs/2405.00361) | token 単位・閾値で動的 | +2〜14pt | 38★・PEFT 互換を標榜 |
| [MixLoRA](https://arxiv.org/abs/2404.15159) | token 単位 top-2 | **多タスクで +9.8%** | 209★・2024-08 で停止 |
| [X-LoRA](https://arxiv.org/abs/2402.07148) | token × 層・密結合 | 領域事例のみ | 284★・**最も活発**だが Mistral.rs 前提 |

⚠ **手法間の直接比較はほぼ存在しない**。各論文は素の LoRA / DoRA / フル FT としか比べて
おらず、MoE-LoRA 同士の順位づけは信頼できる根拠が無い。

⚠ 遅延・パラメータ増の定量は **MixLoRA 以外ほぼ未報告** (AdaMoLE は著者自身が未解決と明記)。
MixLoRA は素の LoRA 比 **183〜218%** の順伝播時間を報告。

## ⭐ 収穫: 多タスク単一 LoRA の劣化は独立に再現されている

MixLoRA 論文の自前ベースラインで、**多タスク LoRA は単一タスク LoRA に対し -1.9% 劣化**する
一方、MoE ルータ版はこれを回避・反転している。**手元の v2 (混合で生成タスクが劣化) と同型の
現象が、独立した実験で観測されている。**

## ⭐⭐ 現実的に取れる選択肢

**課題ごとに独立のモデルを作り、上流で振り分ける**。これが唯一まともに支持される形。

- 各課題を mlx-lm で学習 → fuse → 課題別 GGUF を作る
- **どのモデルを呼ぶかは上流のオーケストレーション層が決める**
### ⭐ 基盤共有の可否 (追加調査で判明・前の記述を訂正)

- **llama.cpp は LoRA を計算グラフ上のデルタとして適用し、ベース重みへ恒久マージしない。**
  つまり**ベース重みは共有され、アダプタごとの複製が不要**。スケール 0.0-1.0 を再ロード
  無しで変更できる (PR [#8857](https://github.com/ggml-org/llama.cpp/pull/8857) /
  [#10994](https://github.com/ggml-org/llama.cpp/pull/10994) で確認)
- ⚠ **Ollama は真の hot-swap を未サポート** (2026-09 時点)。橋渡し PR
  [#14032](https://github.com/ollama/ollama/pull/14032) は**未マージ**、issue
  [#9548](https://github.com/ollama/ollama/issues/9548) も未クローズ。
  [#7667](https://github.com/ollama/ollama/pull/7667) はロード時の複数適用のみ
- Ollama で運用する場合、**ディスク上の blob は content-addressable store で共有される**が、
  実行時は各モデルが独立にロードされる (= メモリは共有されない)

### ⭐ 追加調査 (ソースコード水準で確認)

**llama.cpp の C API が決定的** — ベースは 1 度だけロードされ、アダプタは実行時のデルタ:
```c
llama_adapter_lora_init(model, path_lora)   // 既にロード済みの model に軽量に紐づく
llama_set_adapters_lora(ctx, adapters, n, scales)  // context 上で差し替えるだけ
```
([llama.h](https://github.com/ggml-org/llama.cpp/blob/master/include/llama.h))。
per-request のアダプタ指定も可能 (PR [#10994](https://github.com/ggml-org/llama.cpp/pull/10994))。
⚠ ただし公式に「**異なる LoRA 設定のリクエストはバッチ化されず性能が落ちる**」と明記。

**Ollama は実行時の差し替えを持たない (2026-09-05 時点)**。`llm/llama_server.go` は
モデルごとの subprocess 起動時に `--lora` を渡すだけで、実行時に変更する経路が無い。
FAQ は「同時ロードする各モデルは VRAM に**完全に収まる必要がある**」と記載
([faq.mdx](https://github.com/ollama/ollama/blob/main/docs/faq.mdx))。
→ **課題別モデルを同時に載せると VRAM は N 倍** (⚠ この帰結自体は明文化されておらず、
別プロセス = 別コンテキストという構造からの推論)。

**vLLM だけが真のマルチテナント LoRA** (Punica / S-LoRA)。
「GPU が事前学習モデルのコピーを 1 つだけ保持して複数 LoRA を提供できる」
([Punica, arXiv:2310.18547](https://arxiv.org/abs/2310.18547))、
CPU 側に数千のアダプタをプールして GPU へ出し入れする
([S-LoRA, arXiv:2311.03285](https://arxiv.org/abs/2311.03285))。
⚠ CUDA 中心で、Apple Silicon 版 (`vllm-metal`) は 2026 年に出たばかりで発展途上。

| 能力 | llama.cpp | **Ollama** | vLLM |
|---|---|---|---|
| 複数アダプタの同時ロード | ○ | ○ (起動時のみ) | ○ |
| 再起動なしの差し替え | ○ (`POST /lora-adapters`) | **✗ (PR #14032 未マージ)** | ○ |
| リクエスト単位の選択 | ○ | ✗ | ○ |
| 異なるアダプタの混在バッチ | **✗ (公式に非対応と明記)** | ✗ | ○ |

### ⚠ MoE 特有の変換障害リスク

`convert_lora_to_gguf.py` は **gate/up projection が結合された MoE で変換に失敗する既知バグ**
がある ([issue #21864](https://github.com/ggml-org/llama.cpp/issues/21864))。
Gemma 4 MoE でも同種の障害が起きうる (**未検証・推測**)。
また `mlx_lm.fuse --export-gguf` は **Mistral/Mixtral/Llama の fp16 に限定**と
[LORA.md](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md) に明記されており、
Gemma4 MoE は対象外の可能性が高い (推測)。→ **fuse 後に本体を変換する現行経路
(`scripts/mlx_fused_to_hf.py` → `convert_hf_to_gguf.py`) のほうが障害を踏まない。**

⭐ **この repo にはその振り分け層が既にある** — `src/tools/model_tiers.py` の
Step → tier → model 解決がそのままタスクルータとして機能する。MOELoRA の
「タスク条件つきゲート」を、transformer の内部ではなくオーケストレーション層で実現する形。
新しい概念を持ち込まずに済む。

## 手元の実測との対応

| 実測 | 対応 |
|---|---|
| v2 の混合で生成タスクが劣化 | MixLoRA の多タスク LoRA -1.9% と同型 |
| 「Ollama が基盤を共有するか」の問い | **ディスクは共有・実行時メモリは非共有**。llama.cpp 直なら真に共有できるが Ollama 経由では不可 |
| ルータ方式への期待 | ⚠ **この構成では実装不能**。CUDA でも未解決 |


## ⭐⭐ 追加調査: この構成でアダプタ単体の運用は**できない** (ソース確認)

`mlx_lm.fuse --export-gguf` は **llama / mixtral / mistral に hard-gate されている**
(`mlx_lm/fuse.py` の `if model_type not in [...]: raise ValueError`)。**Gemma は世代を問わず対象外。**

Ollama の `ADAPTER <safetensors ディレクトリ>` も、`convert/convert.go` の
`ConvertAdapter` が **`"llama"` と `"gemma2"` しか受け付けず**、gemma3/gemma4 は
`default: unsupported architecture` に落ちる。**ドキュメントの対応表はコードより古い。**

### 決定的な障害: MoE の expert LoRA は 3 次元

`mlx_lm/tuner/lora.py` の switch/expert 用 LoRA は
`mx.zeros(shape=(num_experts, output_dims, r))` と **先頭に num_experts 軸を持つ 3D**。
一方、MLX → PEFT の変換スクリプト
([mlx#1507](https://github.com/ml-explore/mlx/discussions/1507)) は
**2D の dense 層しか扱わず**、動作確認も dense な Gemma 2 のみ。

⚠ **mlx-lm で学習したアダプタを MoE モデル向けに GGUF 化した前例は、探した範囲で 1 件も無い。**
`llama-adapter.cpp` を直接読んでも MoE 固有の処理はゼロで、名前と形の一致だけで適用するため、
**変換がわずかに誤っていても大声で落ちずに静かに劣化する**。

### llama.cpp 側の Gemma 4 バグは修正済み

| 不具合 | 修正 |
|---|---|
| `LoraTorchTensor` に `split()` が無く gate+up 結合 expert を分割できない ([#21864](https://github.com/ggml-org/llama.cpp/issues/21864)) | [#22832](https://github.com/ggml-org/llama.cpp/pull/22832) 2026-05-12 |
| `model.language_model.*` の接頭辞を剥がせない ([#23047](https://github.com/ggml-org/llama.cpp/issues/23047)) | 2026-05-19 |
| `architectures` が `text_config` 配下でアーキ判定が落ちる | [#24621](https://github.com/ggml-org/llama.cpp/pull/24621) 2026-06-14 |

→ 2026-06 以降の checkout なら機械的な障害は越えられる。**しかし 3D の expert テンソルを
どう変形すべきかは誰も文書化しておらず、新規の未検証コードが必要。**

### ⭐ 結論: 現行経路が唯一の確認済みルート

**`mlx_lm.fuse` (`--export-gguf` なし) → 本体を HF 名へ remap → `convert_hf_to_gguf.py`**
という、いま使っている経路が de-risked な唯一の道。代償は**アダプタの hot-swap を諦め、
課題ごとに 16GB のモデルを持つこと**。

これは 9月4日の引き継ぎにあった「Ollama 配備が MoE 非対応で塞がれた」という記述の、
より正確な姿でもある — **本体の変換は可能** (実際に成功させた)、**塞がっているのはアダプタ単体の変換**。

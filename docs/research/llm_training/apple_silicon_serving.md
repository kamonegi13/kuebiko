# Apple Silicon の LLM 配信基盤 (調査: 2026-09-05)

> ⚠ 出典は調査時に収集したもの。筆者が独立に再検証したものではない。

## 各基盤の要点

| 基盤 | schema 制約 | 複数 LoRA | 常駐適性 |
|---|---|---|---|
| **llama.cpp 直** | ◎ GBNF + llguidance (成熟) | per-request 対応済みだが ⚠ **重大バグ** | プロセス管理は自前 |
| mlx_lm.server | ✗ ネイティブ対応なし | リクエスト毎の adapters 指定可 | ⚠ **公式が production 非推奨と明記** |
| vllm-metal (公式 plugin) | vLLM 本体は xgrammar 持ちだが **Metal 移植状況は未記載** | 同左 (未確認) | 発展が速い (v0.2.0 で TTFT 83 倍/自己比) |
| vllm-mlx (個人開発・別物) | ○ lm-format-enforcer | 明記なし | ⚠ 実質単独メンテ |
| LM Studio | ○ | ✗ (issue #51 が未実装のまま) | — |

⭐⭐ **llama.cpp の per-request LoRA には未修正の重大バグがある**:
[Issue #26207](https://github.com/ggml-org/llama.cpp/issues/26207) (2026-07 報告・open) —
**prompt cache が異なる LoRA 選択の間で再利用され、出力が静かに汚染される**。
回避は毎回 `cache_prompt:false` (全文再処理で速度大幅低下)。
共有 system prompt 構成の本番運用では要警戒。

## 性能の実測 (tokens/sec)

- decode: **MLX が生 llama.cpp (Metal) の約 1.4-1.8 倍** (M4 Pro / 30B MoE、
  [yage.ai](https://yage.ai/share/mlx-apple-silicon-en-20260331.html)・未査読)。
  ⚠ Ollama の Go ラッパー込み比較 (130 vs 43) は補正が要る
- prefill: **llama.cpp が優位**という同報告 (650 tok プロンプトで GGUF 20 vs MLX 13 tok/s)
- M5 の Neural Accelerators は MLX に一方的に有利 (M4→M5 で TTFT 4.06 倍・decode 1.19 倍の報告。
  ⚠ 一次ベンチ未確認)

→ **この repo のワークロード (13k 字プロンプト + 500-800 tok 出力) は prefill 比重が高く、
「MLX が速い」を鵜呑みにできない。**移行判断には自前実測が必須。

## ⭐⭐ "The Format Tax" — 制約デコードの劣化原因の切り分け

[arXiv:2604.03616](https://arxiv.org/html/2604.03616) (10 モデル × 4 フォーマット):

- **劣化の主因はデコーダ制約ではなくプロンプト側** — フォーマット指示のみで平均 **-3.9pt**、
  制約デコード追加の上乗せは **-1.6pt** に留まる
- 「自由記述 → 再フォーマット」の 2 ターンで 42/72 改善 (+6.8pt)
- ⭐ **claude-haiku 級ではフォーマット税がほぼゼロ = 訓練で緩和可能な問題**

→ 手元の未解決問題「bf16 制約なし vs Q4 制約ありの充足差は量子化か制約か」への示唆:
**Q8 実験で量子化説は棄却済み。残る差の説明として「制約そのもの (-1.6pt 級)」より
「スキーマ指示への応答の仕方」が本命であり、それは SFT で縮められる類のもの。**

## 推奨構成 (調査の結論)

**A. 現状維持 + 部分補強 (低コスト)** — schema/複数 LoRA が要る処理だけ llama-server を
別プロセスで。⚠ #26207 のため cache 無効化が必要

**B. vllm-metal の小規模 PoC (中コスト)** — MoE の MLX モデルを直接配備できる可能性。
⚠ LoRA / guided decoding の Metal 対応は情報不足で、**採用前に実機 PoC が必須**

**C. 用途別 fuse 済みモデル + 都度ロード** — 毎時バッチ主体の現運用と親和的

⭐ 総評: **複数 LoRA per-request を常駐で安定運用できる基盤は 2026-09 時点で存在しない**
(llama.cpp は KV 汚染バグ、MLX 系は薄い、vllm-metal は未確認)。
fuse 済み全量モデルの構成が引き続き最も堅い。

# LLM 学習に関する調査ノート

kuebiko のローカル LLM を SFT する過程で直面した具体的な問いに対して、文献を調査した
記録。**汎用の教科書ではなく、この repo の実測値に紐づけた調査**として書く。

## 使い方

- 各ノートは独立して読める。RAG の断片として引く前提で、主張ごとに出典 URL を持つ
- **実測値との対応**を各ノート末尾に置く。文献の主張と手元の観測が食い違う箇所は
  そう明記する (食い違いこそ次に測るべき点)
- 断定と推測を分ける。「よく言われているが実証が弱い」ものはそう書く

## 調査の出発点になった実測 (2026-09-04〜05)

| 観測 | 値 |
|---|---|
| 単一タスク SFT の副作用 | 学習で見ていないスキーマで縮退 (同一 ID の重複 56.8% / 空欄に文字列 `"null"` 72%) |
| 多タスク混合 (32:35:33) の効果 | 抽出・分類は改善、**生成タスクの慎重さは base より有意に悪化** (caveats 1.18→0.85 p=0.001) |
| 契約の学習速度 | 150 反復 (計画の 21%) で欠陥が消える |
| 能力の転移速度 | 700 反復でも教師の 39% (ATT&CK 抽出 5.45→2.12)。ただし IOC は教師超え (4.02→4.45) |
| 教師の選択 | 課題ごとに変わる。事象ニュースでは Opus > Sonnet だが、課題の軸が違えば結論も変わる |

## ノート一覧

| ファイル | 扱う問い |
|---|---|
| `multitask_interference.md` | 多タスク SFT の干渉と混合比 |
| `catastrophic_forgetting.md` | 逐次学習の忘却と緩和策 |
| `lora_composition.md` | アダプタの分離・合成・切り替え |
| `distillation_transfer.md` | 強い教師から何が転移するか |
| `preference_optimization.md` | SFT 後段の選好最適化 (DPO 系) |
| `evaluation_methodology.md` | 静かな劣化をどう検出するか |
| `sft_transfer_failure_diagnosis.md` | 継続学習の不動・seed 分散・尾部欄の欠落 — 学習経路 (lr・容量・露出) の診断と是正順序 (2026-09-11) |

## 関連する repo 内の資産

- 凍結評価セット: `data/eval/goldset.jsonl` (86) / `data/eval/triage_goldset.json` (150) /
  `data/mlx/eval_sft.json` (39・Opus 参照つき)
- 評価 CLI: `scripts/eval_goldset.py` (床を必須にする設計) / `scripts/eval_sft_ollama.py`
- 学習データ組み立て: `scripts/assemble_sft_dataset.py` (課題ごとの上限・実トークン数で除外)

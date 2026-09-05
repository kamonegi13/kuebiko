# 基盤モデル選定 — 非中国系・日本語・Apple Silicon (調査: 2026-09-05)

> ⚠ 出典は調査時に収集したもの。筆者が独立に再検証したものではない。

## 前提の確認

2026-09 の日本語リーダーボード上位は中国系が占めており、「オープンで日本語に強いモデルは
事実上中国産一択」という状況 ([Qualiteg 2026-09](https://blog.qualiteg.com/llm-ranking-2026/))。
本 repo は §4 で中国系を除外するため、**その制約下での最適**を選ぶ。

## 非中国系の主要候補 (2026-09)

| モデル | 構成 | ライセンス | 日本語実測 |
|---|---|---|---|
| **Gemma 4 31B** (dense) | dense | **Apache 2.0** (2026-04 に旧独自条項から変更) | **0.8077 = 非中国系オープン dense で最高** |
| Llama 3.1 **Swallow 8B** | dense (JP 継続学習済) | Llama Community 継承 | JP 特化。配備経路が最も枯れている |
| CyberAgent **CALM3-22B-Chat** | dense | Apache 2.0 | Nejumi3 で Llama-3-70B 相当 |
| **GPT-OSS-Swallow 20B** | MoE | Apache 2.0 | JP MT-Bench 0.916 (Swallow 自己評価) ⚠ ハーネス依存大 |
| llm-jp-4 8B | dense | Apache 2.0・**学習データまで公開** | やや劣るが供給網の透明性が最高 |
| Mistral Small 3 (24B) / OLMo 3 / Granite 4.x / Falcon 3 | dense 中心 | Apache 2.0 系 | JP 実測は上記に劣後 |

⚠ Cohere Command R+ は **CC-BY-NC (商用不可)** に注意。

## ⭐ dense vs MoE — 「dense なら変換問題が消える」はおおむね正しい

- **学習**は mlx-lm で MoE の expert 層にも LoRA を当てられる (issue #571 はメンテナが
  使い方の誤りと回答)
- **配備**が壊れる: `convert_lora_to_gguf.py` は Mixtral/GraniteMoE 系の expert で失敗歴、
  HF PEFT も MoE expert は `nn.Parameter` で通常の LoRA が乗らない
  ([peft #2527](https://github.com/huggingface/peft/issues/2527))
- **dense は Unsloth 公式手順で LoRA→GGUF→Ollama が枯れた経路**
  ([unsloth docs](https://unsloth.ai/docs/basics/inference-and-deployment/saving-to-ollama))

fine-tune 適性そのもの: MoE は事前学習効率で勝るが**少データのドメイン転移では dense に
劣後する傾向** ([arXiv:2405.15052](https://arxiv.org/pdf/2405.15052))。⚠ 一般則としては
「一概に言えない」が確認できる事実で、**配備エコシステムの成熟度を加味すると dense 優位**。

## 順位づけ (調査エージェントの結論)

1. **Llama 3.1 Swallow 8B (dense)** — JP 継続学習済・LoRA 反復が高速・配備経路が最も枯れる
2. **CALM3-22B-Chat (dense, Apache 2.0)** — 現行 26B と近い帯を dense で置換
3. **Gemma 4 31B (dense, Apache 2.0)** — 非中国系 dense の実測最高。**既にこの repo の
   narrative fallback として稼働中** = 追加の生態系リスクゼロ
4. GPT-OSS-Swallow 20B (MoE) — JP 品質は高いが配備制約が残る
5. llm-jp-4 8B — 透明性のヘッジ・評価ベースライン

## この repo の実情との突き合わせ

- **Gemma 4 31B dense は既に手元で動いている** (narrative の fallback)。ただし実測で
  spotlight 136 秒/件・per-article 40-100 秒と遅く、毎時バッチの主役には不向き
- **Swallow 8B は「勝てるタスク」(triage ~2,000 呼出/日・抽出) の主役候補**。8B なら
  LoRA の学習サイクルも 26B の数分の一
- 事象ニュース narrative に 8B で届くかは未知数 (ドメイン事例調査の「narrative は
  汎用大規模が強い」と整合的に、期待値は低め)

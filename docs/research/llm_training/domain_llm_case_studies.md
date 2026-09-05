# ドメイン特化 LLM の構築事例 (調査: 2026-09-05)

> ⚠ 出典は調査時に収集したもの。筆者が独立に再検証したものではない。

## セキュリティ / CTI の事例

| 事例 | 手法 | データ規模 | 結果 |
|---|---|---|---|
| **Foundation-Sec-8B** (Cisco) | Llama-3.1-8B + **CPT** | 生 4TiB → **5.1 億トークン** | CTIBench-RCM **+14.3pt** / MMLU **-2.4pt** (一般知能の代償)。Instruct 版は GPT-4o-mini とほぼ互角 ([arXiv:2504.21039](https://arxiv.org/pdf/2504.21039)) |
| Lily-Cybersecurity-7B | Mistral-7B + SFT のみ 22k 対 | — | ⚠ ベンチ数値の公表なし。「デモ止まり」型 ([HF](https://huggingface.co/segolilylabs/Lily-Cybersecurity-7B-v0.2)) |
| **TorchSight** (文書分類) | 27B を SFT (GPT-4 蒸留) | 78,358 件 | **ローカル 95.0% vs 商用 API 75.4-79.9%** — 品質で汎用 API に勝った実例 ([arXiv:2605.20368](https://arxiv.org/pdf/2605.20368)) ⚠ 基盤は Qwen (この repo では使用不可。知見のみ参照) |

⚠ 日本語 CTI 特化の公開モデルは**未発見** (不在の確証ではない)。

## ゼロから vs 適応

- BloombergGPT (50B ゼロから、53 日・約 $300 万) に対し、**FinGPT (LoRA、約 $300) が
  感情分析等で上回った** ([Unite.AI](https://www.unite.ai/generative-ai-in-finance-fingpt-bloomberggpt-beyond/))
- Meditron-70B: Llama-2 + 医療 CPT で公開最良比 +6%、GPT-4 に 5% 差まで
  ([arXiv:2311.16079](https://arxiv.org/abs/2311.16079))

## ⭐⭐ CPT の実績下限は数億トークン — 手持ち 28k 記事では見送りが妥当

- PFN 金融 CPT: **3.7 億トークン** (810 万文書) で総合 +9%
  ([PFN Tech Blog](https://preferred.jp/ja/blog/tech/qfin-llm-continual-pretraining/))
- Cisco: 5.1 億トークン
- ⚠ 4 億トークンでも壊滅的忘却はわずか、という報告もあり数十億は必須でない
  ([futureagi](https://futureagi.com/blog/continued-llm-pretraining/))
- **手元の 28,000 記事は数千万〜1 億トークン程度でこの一桁下** (推測)。CPT の副作用
  (指示追従の低下・MMLU 劣化) に見合う根拠が薄い
- CPT が壊すものの回復策: 一般データ 1-5% の replay / CPT 後の SFT 再適用

## ⭐⭐ 特化 SFT が勝てる条件 — 手元の実測と一致する

**LoRA Land** (310 モデル × 31 タスク): 4bit LoRA が GPT-4 を**平均 +10pt、31 中 25 タスクで
上回った**。負けたのは広く複雑な推論タスク ([arXiv:2405.00732](https://arxiv.org/pdf/2405.00732))。
金融関係抽出では 6.5%→73.4% の劇的改善 ([arXiv:2411.02476](https://arxiv.org/pdf/2411.02476))。

⭐⭐ **共通パターン: タクソノミー固定の抽出・分類は特化 SFT が勝ちやすく、
自由度の高い narrative 生成・複雑推論は汎用大規模モデルが依然強い。**

→ **手元の実測 (v2/v3: 分類・抽出は速く改善、事象ニュースの narrative が最難) は
このパターンの再現そのもの。** SFT が最も苦戦している課題は、文献上も特化 SFT が
勝ちにくい課題型だった。

## CTI 固有の含意 — 知識と技能の分離

「fine-tune はコスト高で新興脅威への追随に不向き、RAG は再学習なしで更新できる」が
CTI 系論文で一致 ([arXiv:2510.27080](https://arxiv.org/html/2510.27080))。

⭐ **揮発性の高い「何が起きているか」は辞書・RAG 側に置き、安定した「どう書く・どう抽出するか」
だけを SFT に持たせる** — この repo の actor_aliases / MITRE sync / rubric-in-DB の設計と整合。

## 手持ち規模 (28k 記事 + 2,300 教師対) での妥当な戦略

1. **CPT は見送り** (実績下限の一桁下)
2. **SFT は妥当な規模** — LIMA の 1,000 件と同桁。書式・rubric 遵守を教える用途には十分
3. **課題別に期待値を分ける**:
   - 重要度分類・要約 + 抽出 = 特化 SFT が汎用 API に**勝ちうる**候補
   - 事象ニュース narrative = 事例上、同規模データで Opus 級に届く保証は薄い。
     **当面は外部 API 併用のハイブリッドが安全**
4. 鮮度は SFT に持たせない (辞書・RAG に委ねる)
5. 凍結 goldset + ドリフト監視の維持が、放棄プロジェクトの典型 (評価基盤なき導入) を避ける鍵

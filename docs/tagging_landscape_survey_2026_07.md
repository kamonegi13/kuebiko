# タギング体系 実地調査 (商用 CTI / コミュニティ標準 / 学術文献) と実装ロードマップ

- 日付: 2026-07-03
- 調査方法: 3 並列 web 調査 — ①商用 5 社 (Recorded Future / Google TI・Mandiant / CrowdStrike /
  Microsoft MDTI / ThreatConnect) の公式 docs・API リファレンス ②OSS・コミュニティ標準
  (OpenCTI / MISP taxonomies・galaxies / STIX 2.1 open vocabulary / OTX / abuse.ch / VirusTotal)
  ③学術文献 (CyNER / TTPDrill / Extractor / AttacKG / LADDER / aCTIon / STIXnet / CASIE /
  ThreatKG / CTINexus / CTIBench / 各サーベイ)。出典 URL は各調査エージェントの報告に記録済み。
- 位置づけ: [[tag_landscape_audit]] (2026-06-20 内部棚卸し) の外部裏付け + 拡張。
  config/tag_registry.yaml (宣言的 SSoT) の将来更新の根拠文書。

---

## 1. 突合の総括 — 我々の体系の相対位置

**標準より厚い軸 (gap ではない・独自の強み):**
- 分析トレードクラフト系: 記事単位ソース信頼性 (Admiralty 相当 tier) / editorial_stance /
  ICD 203 確度 (synthesis 層・grounded ACH) / strategic intent (≒ attack-motivation-ov) / PMESII 8 軸 /
  PIR リンク (商用 5 社のどこにも per-article では無い)
- 暫定アクター (actor_provisional) は MDTI の Storm-#### (Groups in development) と同型の業界標準設計
- **Incident/Campaign の一級エンティティ**: OpenCTI/ThreatConnect の Incident に相当する構造は
  **Situation Ledger (docs/synthesis_situation_ledger_design.md) がまさに提供する** — 別タグ不要
- CTIBench の知見 (LLM 単独帰属は正解 52%) は「確定帰属は辞書ゲート+人承認」原則を文献的に裏付け

**共有・組織運用系は個人運用ゆえ不要 (意図的非対称):** TLP / PAP / workflow state / sighting 共有

## 2. 実装済み (2026-07-03、決定論・LLM プロンプト変更なし)

| タグ | 根拠 (調査での裏付け) | 実装 |
|---|---|---|
| `mentioned_country` | STIX location の言及次元。involved_country (当事国、LLM) の 42% 記入漏れを補完。当事と言及の type 分離は「mention ≠ 実作戦」原則 (cyber_geopolitical_correlation) と actor/actor_provisional の provenance 分離前例に従う | `src/cti/nation_gazetteer.py` (countries.yaml SSoT 走査) + `src/cti/mention_tagger.py` + ingest hook + backfill |
| `campaign` | STIX Campaign SDO。Mandiant は campaign を一級オブジェクト (UNC 併合履歴つき)、RF は Operation entity。全系統が保有し我々に無かった。Situation の強 identity anchor / same_campaign relation の基盤 | "Operation <Name>" 型のみ regex (精度優先、「〜作戦」型は一般語と弁別不能で不採用)。mention_tagger + ingest + backfill |

## 3. ロードマップ (優先順位つき、調査による価値の裏付けと抽出経路)

> **実施状況 (2026-07-04 更新)**: P1 = **CPE 逆引き実装+backfill 済** (affected_vendor 735 /
> affected_product 1187、CVE cache-miss 4,559 は NVD warming 後に `scripts/backfill_derived_tags.py`
> 再実行で追補 = 冪等。製品 gazetteer 側は未着手)。P2 = **実装+backfill 済** (malware_aliases.yaml
> type 列 32 family、malware_type 781 tags)。P3 = **観測ベースは既存実装が該当と判明**
> (actor detail の top_sectors/top_countries)。意図ベース辞書 curation は人手プロセスとして残置。
> ACH への標的 prior 注入は過剰帰属リスク (CTIBench 警告) により不採用。
> P4 = 未着手 (1 field = 1 検証サイクル原則、次の独立スライスで)。
> P5 = **現時点で対象なし** (abuse.ch 系ソース未取込。将来 feeds 追加時にパススルー実装)。
> P6 = 設計どおり据置 (効果測定してから)。

実装制約: **summarizer prompt (26B) への field 追加は脱落の前科があり、1 変更 = 1 検証サイクル**
(A/B dry-run で全 field 保持を確認) を厳守。同時多数追加は禁止。

### P1: affected_product / vendor (影響製品) — 全系統「全会一致」の最大 gap
- 裏付け: 商用 5 社全て (RF は Product/Technology が一級 entity) / 学術ほぼ全論文 (CyNER System,
  LADDER Application/OS, ThreatKG vulnerable software products, CASIE software/device) /
  2026-06-20 内部棚卸しの推奨とも一致。下流 = 資産照合・パッチ優先度 = CTI の最頻用途。
- 経路 (ハイブリッド): ①CVE あり記事 = NVD CPE 逆引き (既存 warming 中) を entity 永続化
  ②CVE なし記事 = 製品 gazetteer (自コーパスの CPE 頻度上位から lexicon を自己 bootstrap、
  Windows/Chrome 等の汎用語 stop 付き)。②は精度検証必須。
- Situation Ledger への効果: CVE 番号なし脆弱性記事 (Cisco Unified CM 型) が強 anchor を得る。

### P2: malware_type (RAT/stealer/loader/wiper/botnet…) — is_ransomware の一般化
- 裏付け: STIX malware-type-ov (22 値) / RF MalwareCategory / VT category / MISP が galaxy を
  種別で分割 / LADDER Malware Type。wiper (破壊) と stealer (窃取) は状況認識上の意味が別。
- 経路 (決定論): malware_aliases.yaml (curated 語彙、malware_normalizer) に type 列を追加し
  family→type を辞書導出。LLM プロンプト変更なし。未知 family は untyped のまま
  (Malpedia/MITRE Software との週次同期 = mitre_sync パターンは将来)。
- is_ransomware との整合: type='ransomware' ⊃ is_ransomware に片寄せ (boolean は当面併存)。

### P3: 標的プロファイル targeted_industries / targeted_regions (意図) — 被害 (実績) と区別
- 裏付け: 商用 5 社全てが actor とレポート両方に複数値で付与 (標的化 3 軸: industry/country/motivation)。
  OTX は targeted_countries が既定。我々は victim_* (観測された実績・単数) しか持たない。
- 経路: **記事タグでなく actor 辞書側の属性** (actor_aliases.yaml に targeting profile) が標準形。
  記事からの動的判定は過剰帰属リスク (CTIBench 警告) — 辞書 + 人承認の既存パターンで。
- 価値: synthesis の将来予測軸 (「この actor は日本の防衛産業を狙う傾向」) に直結。

### P4: 影響定量 + 対処 (VERIS attribute / CASIE 引数 / Course of Action) — LLM field、1 つずつ
- 裏付け: 内部棚卸し gap ①②と外部調査が完全一致 (ThreatConnect CoA 一級 / MDTI recommended
  actions / CASIE money・number-of-data・PatchVulnerability イベント / MISP veris)。
- 経路: summarizer への field 追加 (LLM 必須)。**26B 脱落リスクのため 1 field ずつ**、
  優先は remediation_available (bool+一文) → data_impact (種別/規模)。
- 効果: 「要対応」KPI の客観化、行動可能性 (読んで何をするか)。

### P5: IOC 役割 (infrastructure-type: C2/phishing/配布) — 供給源パススルー優先
- 裏付け: STIX infrastructure-type-ov / abuse.ch threat_type (取込ソースが既に配信) /
  AttacKG (フラット IOC は構造情報を失う) / CrowdStrike kill_chains on indicator。
- 経路: abuse.ch 系ソースの threat_type をパススルー保存 (決定論) → 足りなければ LLM 判別。

### P6 (研究的): realis (事実性) の抽出層タグ
- 裏付け: CASIE が event ごとに realis (実発生/仮定/一般論) を一級プロパティ化。
  grounded synthesis の「報告≠実発生」「再浮上」を抽出時点に前倒しする学術先行例。
- 経路: editorial_stance の拡張として検討 (LLM)。効果測定してから。

## 4. 却下 / 見送り (理由つき)

| 項目 | 理由 |
|---|---|
| TLP / PAP / workflow state | 再配布なし・単独運用 (§9 Out of Scope) で実益なし |
| システムレベル挙動抽出 (process/file/registry + 動詞、Extractor/AttacKG 型) | hunting 基盤 (EDR 照合) を持たない。ブリーフィング用途では過剰 (学術調査エージェントも見送り妥当と判断) |
| Sighting (検知回数/共有) | 裏取りクラスタリング (N=3) が実質同機能。共有前提の概念 |
| tactic / kill-chain の entity 化 | T-id から ATT&CK 静的マッピングで**読み時に決定論導出可能** — 保存は重複 |
| Grouping / IoC Collection | actor_provisional + 裏取りクラスタが機能代替。Incident 一級化 = Situation Ledger が先 |
| Area (地政学地帯) / 実世界 Event entity | 地域ブラッシング/synthesis 文脈で実質カバー。YAGNI |
| 「〜作戦」型 campaign 抽出 | 陽動作戦/特別軍事作戦 等の一般語と弁別不能 (精度優先で Operation 型のみ) |

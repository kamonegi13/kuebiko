# PIR 照合の全体設計と概念 PIR の LLM 主題判定層 (2026-07-23)

> **status**: **Phase A〜E 実装・deploy 済** (2026-07-23、commit 6ef9520c)。
> Phase A (決定論 2 件) A/B 済 → B (判定層基盤 + apt_leak PoC) → C (attribution /
> integrated_cyber_ops) → D (geopolitical_cyber 再定義、利用者承認済文言) → E
> (PIR_SIGNAL_FIRST / PIR_LLM_JUDGE 既定 ON) まで一括実装。verdict backfill と
> 検証結果は §10 に記録。**未了 (意図的 defer)**: authoring preview の
> 「LLM 未確定 n 件」UI 表示 (§4.5 — backfill 済み窓では preview に verdict が
> 効くため実害は新規 judge PIR 作成時のみ)。
> **前段の SSoT**: [pir_signal_first_matching_design.md](pir_signal_first_matching_design.md)
> (signal-first 14 PIR、2026-07-22〜23 完了)。本ドキュメントはその継続 — 決定論で
> 切り切れない残余を扱う。
> **関連**: [pir_system.md](pir_system.md) / CLAUDE.md §13 /
> [subject_actor_attribution_design.md](subject_actor_attribution_design.md)

---

## 0. PIR とは何か — 本ツールにおける位置付け (再確認)

**PIR (Priority Intelligence Requirements) = 指揮官 (=利用者) の意思決定に必要な
優先情報要求**。CTI doctrine の中心概念であり、本ツールでは「何を集め・何を重要とし・
何を語るか」を駆動する canonical intent である。

- **description (自然文) が正**。structured fields (keywords / match ツリー) は
  そこから compile された中間表現にすぎない (pir_system.md 原則 1)。
- **PIR ≠ 配信**。チャンネル決定は routing 専属 (R0 撤去済、2026-06-13)。
  PIR は triage の importance 基準を経由して配信に届く (背骨: PIR → importance → channel)。
- **PIR が直接駆動する層**: triage 判定基準 (title+description 注入) / synthesis
  pir_context / Spotlight (週次縦断 narrative) / Daily Focus (朝の PIR 別集約) /
  KPI・情報フロー画面 / entity タグ (`entity_type='pir'`、検索・地図の PIR 次元)。
- **article×PIR 照合の品質 = これら全ての品質の土台**。照合が過剰なら Daily Focus /
  Spotlight に無関係記事が混じり narrative が汚染され、KPI が水増しされる。
  過少なら「関心事が起きたのに語られない」。**照合品質は PIR システムの精度そのもの**。

### 照合が答えるべき問い

照合は「記事がキーワードを含むか」ではなく
**「この記事は、利用者がこの PIR で求めた関心事を主題として扱っているか」**
に答えなければならない。signal-first 再設計 (前段 SSoT) はこの問いの大部分を
取込 LLM の意味メタデータへの述語で解いた。本ドキュメントは、述語でも
キーワードでも表現できない残余 (概念判定・主題判定) を扱う。

---

## 1. これまでの改善の完全評価 (2026-07-23 時点)

### 1.1 何が解決したか

根本原因「**二重分類器**」(取込 LLM の強い意味メタデータを捨て、生キーワード
substring OR で再分類) を、routing と同じ述語ツリー (signal-first) への統合で是正した。
14/21 PIR を変換、entity links 22,472→10,308。多義語 FP (脆弱性/制裁/leak/
サプライチェーン/緊急/重要インフラ) と「地政学コーパス 54% の漏れ」を
`NOT category=geopolitical` 等で構造的に排除した。

| PIR | 30d A/B | 手法 | 90d entity (現在) | 評価 |
|---|---|---|---|---|
| new_poc_vuln | 1663→666 | category=vulnerability | 1,368 | 健全 |
| agency_alert | 1751→52 | feed contains_any AND advisory | 75 | 健全 |
| state_ransomware | 996→26 | is_ransomware AND sector | 50 | 健全 |
| jp_company_breach | 1647→318 | victim=jp AND (ransom OR breach) | 485 | 健全 |
| ally_cyber_event | 886→360 | victim∈5ヶ国 AND not geo | 521 | 健全 |
| critical_infra | 821→430 | sector∈6 OR kw{ICS,OT,SCADA} | 794 | 健全 |
| supply_chain 3種 | 655/728/64→289/334/64 | not geo AND kw | 733/851/232 | 健全 |
| attribution | 627→258 | kw{帰属,起訴,…} OR feed | 506 | **残 FP あり (§2)** |
| disinfo | 239→307 | intent=influence | 582 | 健全 |
| emergency | 225→1 | kw{Emergency Directive,…} | 4 | 健全 (真にレア) |
| minor_vendor_breach | 195→95 | kw{security vendor,…} | 168 | 健全 |
| apt_leak | 204→2 | kw{i-Soon,Vulkan,…} | 4 | **残 2 件とも FP (§2)** |
| china/dprk/russia APT | 不変 | keyword (具体名最適) | 109/108/125 | 健全 |

data-driven で回避したコスト: actor_nation 述語 (subject 帰属疎 27件で recall 崩壊)、
Phase 2b boolean 列スキーマ移行 (kev/japan_critical は照合 intent に不要) —
いずれも A/B が「やらない」判断を確定させた。

### 1.2 何が残っているか (2026-07-23 実測、30d posted 7,787 件)

| PIR | 現状 | 実測 | 問題の型 |
|---|---|---|---|
| **pir_jp_targeted** | keyword+countries (未変換、**前段 doc の per-PIR 表から漏れていた**) | 391 件。via countries=350 / keywords=53。keyword 側が中露艦艇 EEZ 実弾演習・再軍備等の geopolitical 33 件を混入 | 決定論で可 → **Phase A で変換済** |
| **pir_integrated_cyber_ops** | keyword 4 語 | 15 件中 research 7 (学術サイドチャネル論文)。`NOT research` で 15→8。残 8 にも市場予測等の言及系 2-3 件 | 決定論で大半解消 → **Phase A**。残余は概念判定 (§2-②) |
| **pir_apt_leak** | 変換済 (kw{i-Soon,Vulkan,…}) | **残 2 件が両方 FP**: FSB 第16センター制裁 / PLA 調達制限 — i-Soon を例示言及するだけの記事 | **言及≠主題** (§2-①)。具体固有名詞でも起きる |
| **pir_apt_attribution** | 変換済 (kw OR feed) | 258 件。`起訴`→地震学者スパイ起訴/殺人起訴、`attribution`→OSINT/研究論文、`indictment`→Nord Stream。`NOT geopolitical` は FSB 制裁級の真陽性も殺すため不可 | **概念判定** (§2-②):「国家による APT 帰属の公表」 |
| **pir_geopolitical_cyber** | keyword+8ヶ国 victim OR | 1,558 件 (全 PIR 最大)。via countries=956 が主犯 — 8ヶ国の被害記事なら何でも match (intent=financial 269 = 単なる犯罪 breach)。category ∈ geopolitical 818 | **PIR 定義の問題** (§2-④) + genre 判定 (§2-②) |
| pir_known_threat_followup | disabled・signals 空 | 0 | **時間関係判定** (§2-③)。スコープ外 |

---

## 2. 残る問題の構造分類 — 決定論では切れない 4 型

signal-first の述語 (L1) と語境界 keyword (L2) で解けない残余は、次の 4 型に分類できる。
**型が違えば対処も違う** — 全部を LLM に投げるのでも、全部を config でいじるのでもない。

- **① 言及 ≠ 主題**: 具体固有名詞 (i-Soon, Vulkan) ですら「例として言及しているだけ」の
  記事を拾う。keyword は出現を検出するが主題性を判定できない。
  → **LLM 主題判定** (§4)。実例: apt_leak の残 2 件は両方これ。
- **② 概念 / ジャンル判定**: 「統合作戦」「帰属の公表」「戦略分析」は語の集合でなく
  **記事の性格**であり、どの語彙・述語でも十分条件を書けない
  (`起訴` ⊅ 「APT への起訴」、`電磁波` ⊅ 「電磁波の統合作戦利用」)。
  → 決定論で候補を絞り (research 除外等)、**LLM が概念適合を確定** (§4)。
- **③ 時間・関係判定**: 「既知脅威の**続報**」は記事単体でなく過去報道との関係で決まる。
  記事×PIR の述語照合の外にある (corroboration / dedup cluster の情報が要る)。
  → **本設計のスコープ外**。将来 corroboration 層と接続する別設計 (§8)。
- **④ PIR 定義そのものの問題**: geopolitical_cyber の「8ヶ国 victim OR」は定義が
  関心事を表現していない (何でも入る)。技術でなく**利用者の意図の再確認**が要る。
  → **PIR 再定義** (§7、Phase D)。再定義後に②の手法を適用。

---

## 3. 照合アーキテクチャの終着形 — 3 層モデル

```
L1  意味プロパティ述語 (決定論・高速)      ← 全 PIR の背骨。取込 LLM の分類を信頼する
    category / intent / victim_* / is_ransomware / feed_title への条件ツリー
L2  keyword_any (語境界・決定論)           ← 具体名 PIR (APT/log4j) と L3 の候補ゲート
    意味 clause と AND する弱補強。単独 sole-match は原則却下
L3  LLM 主題判定 (概念 PIR 限定・永続 verdict) ← ①②型の残余のみ。候補ゲート通過分に
    のみ走る focused judge。判定は事実として永続化され、評価自体は決定論に戻る
```

- **判定原則**: まず L1 で表現できないか。次に L2 の具体名で表現できないか。
  それでも①②型が残る PIR だけが L3 を有効化する。**L3 は L1+L2 の候補ゲートを
  必ず伴う** (LLM 単独で全記事を舐めない = wholesale D 案の却下は不変)。
- **決定論との関係**: LLM verdict は summarizer の category/intent と同格の
  「**取込時に確定する意味的事実**」として扱う。一度永続化された verdict を
  述語として読む評価 (rebuild / KPI / preview) は完全に決定論のまま —
  非決定論なのは事実の産出だけで、事実の消費ではない (category と同じ性質)。

---

## 4. LLM 主題判定層の設計 (Phase B)

### 4.1 ホスト = focused judge (triage 拡張ではない)

当初案は「triage LLM は既に PIR title+description を読んでいるので出力を拡張する」
だったが、**focused judge (独立した小さな判定呼出) を採用**する。根拠:

1. **summarizer 過負荷の教訓** (監査 2026-07-13): 取込 LLM に責務を足すと既存の
   出力品質が落ちる。intent 修復は summarizer 拡張では不発で、focused 分析軸
   分類器への再設計で成功した — 同じ轍を踏まない。triage は importance 専任のまま。
2. **候補ゲートで対象が少数** (~10-30 件/日) なので専用呼出のコストは無視できる。
   triage 拡張だと全記事 (~260/日) の prompt/schema が変わり、影響が非対称に大きい。
3. **バッチ実行なら backfill と forward が同一コード**になり、ingest レイテンシ影響ゼロ。
   judge は DB の全列 (title/summary/body/category/actors) を読めるため、triage の
   1500 字 preview より判定材料も豊富。

### 4.2 実行形態 = pir-entity-rebuild の前段バッチ

毎日 01:30 の `pir-entity-rebuild` job の前段として incremental judge を走らせる:

```
judge バッチ (毎日、rebuild の直前):
  for pir in (llm_judge.enabled な PIR):
      candidates = 窓内 posted で pir.match (候補ゲート) を満たす記事
      targets    = candidates のうち verdict 未保存 or pir_rev が stale のもの
      for article in targets:            # 定常運転では 1 日数件〜数十件
          verdict = LLM(judge prompt)    # matched: bool + reason
          upsert pir_llm_judgments
→ 直後の rebuild_pir_entities が verdict を述語として消費
```

- **incremental**: verdict は正負とも保存するので、同じ記事を再判定しない。
  定常コストは「新規記事のうち候補ゲート通過分」のみ。
- **backfill = 同じバッチを初回に窓いっぱい回すだけ** (30d 実測: apt_leak 90 /
  attribution 258 / integrated 8 ≒ 360 件 ≒ 1-2 時間の深夜バッチ 1 回)。

**毎時 incremental (`pir-judge-hourly`、2026-07-23 追加)**: 夜間バッチだけだと
日中取込分の judge PIR 適合が最大 24h 未確定になり、PIR 画面ライブ評価 / News facet /
夕方 synthesis が古いままになる。判定コスト実測 ~1.3 秒/件・平常時毎時 1-3 件 =
実質フリーのため、`judge_hourly()` (毎時 :45、clock-aligned interval) を追加:

- **新着のみ** (`include_stale=False`): stale 再判定 (PIR 編集起因、最大 ~1000 件) は
  日中の Ollama に流さず夜間の大 cap に残す。
- **小 cap** (`HOURLY_MAX_JUDGMENTS=20`): 多発日でも 1 run ~30 秒に抑える。
- **entity タグ即時反映** (`sync_entities=True`): 候補ゲート通過 ∧ verdict 適合 = match
  なので、matched=true はその場で (pir, pir_id) を upsert して良い (add-only)。
  削除側の整合 (stale flip 等) は従来どおり夜間 full rebuild が担う。

### 4.3 データモデル — `pir_llm_judgments` (新テーブル)

```sql
CREATE TABLE pir_llm_judgments (
    article_id  TEXT NOT NULL,
    pir_id      TEXT NOT NULL,
    matched     SMALLINT NOT NULL,          -- 1/0。負の verdict も保存 (再判定防止)
    reason      TEXT,                       -- 30 字程度の判定理由 (UI 透明性)
    pir_rev     TEXT NOT NULL,              -- PIR description+question のハッシュ
    judged_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (article_id, pir_id)
);
```

- **負の verdict を保存する**のが要 — さもないと不適合候補を毎日再判定する。
- **pir_rev** = description + judge question のハッシュ。PIR を編集すると自動的に
  stale になり、次回バッチが再判定する (describe-then-compile 原則と整合:
  description 編集が判定基準の編集)。
- 新テーブルなので SQLite `_SCHEMA` と `pg_schema.py` 双方に追加
  ([synthesis_situation_ledger] の掟)。列追加でないため index 順序 gotcha は非該当。
- **entity (`entity_type='pir'`) には手を入れない**: verdict は rebuild が消費する
  中間事実で、成果物は従来どおり pir entity に一元化される。

### 4.4 モデル拡張 — `Pir.llm_judge`

```yaml
# Pir schema への追加 (models.py)
llm_judge:
  enabled: true            # 既定 false。true で match (候補ゲート) AND LLM 確定の合成
  question: null           # null = title+description から生成。文言を上書きしたい時だけ設定
```

- **match ツリーは従来どおり「候補ゲート」を書く** (決定論・preview 可能)。
  llm_judge.enabled のとき evaluator が `match AND verdict` を合成する。
  ツリー内に自己参照 leaf を書かせない (authoring を単純に保つ)。
- matched_via には `"llm:subject"` を追記 (provenance 透明性、既存の
  `subject:llm` パターンと同型)。

### 4.5 evaluator / persist の合成と修正

- `pir_match_signals`: match tree 通過後、`llm_judge.enabled` かつ
  `PIR_LLM_JUDGE=1` なら verdict map (rows と同時に prefetch、actor_map と同型) を
  引く。verdict 無し (未判定) は **不適合扱い** (判定待ちの記事が翌日 rebuild で
  昇格する eventual consistency — persist の既存性質と同じ)。
- **`has_strong_signals` ゲートの潜在バグを同時修正**: persist / evaluator 4 箇所が
  旧 strong_signals しか見ておらず、match ツリーのみの PIR が rebuild から漏れる。
  `has_match_criteria(pir) = has_strong_signals(pir) or pir.match is not None` に置換。
  (現 deploy は全 PIR が keyword 残置で顕在化していないが、strong_signals 降格の
  完了 (§8) に必須の前提。)
- **preview (authoring UI)**: 判定済み記事は実 verdict を反映。未判定分は
  候補ゲート結果のまま「うち LLM 未確定 n 件」を併記する (正直な限界の明示 —
  隠さない。前段 SSoT 設計原則 5)。

### 4.6 judge プロンプト

- ペルソナは `prompts/_persona.j2` を include (2026-07-22 共通化に整合)。
- 入力: PIR title + description (+question override)、記事 title / summary /
  body 抜粋 (2,000 字程度)。
- 出力: `{matched: bool, reason: str}` (generate_structured、think=False)。
- 判定指示の核心 (①型の是正):
  **「記事の主題がこの PIR の関心事か。過去事例としての例示・比較・背景言及だけなら
  不適合」** を明示する (subject_actor_attribution の「言及≠主題」原則を PIR に一般化)。
- モデル = `OLLAMA_EXTRACT_MODEL` (26B MoE, 5-15s/件)。中華系 denylist・外部 LLM の
  明示割当原則は不変 (§4 CLAUDE.md)。

### 4.7 rollback / 検証 / 規約

- **rollback**: env `PIR_LLM_JUDGE=0` → 合成をスキップし候補ゲートのみで match
  (= 現状の挙動に degrade、recall 側に倒れる)。judge バッチも skip。
  PIR 単位では llm_judge.enabled=false (DB 版 revert 可)。
- **A/B 基準 (apt_leak PoC)**: 現行の FP 2 件 (FSB 制裁 / PLA 調達) が不適合と
  判定されること。30d 候補 90 件のサンプル目視で「適合=真に APT 実態暴露が主題」
  のみ残ること。**安定性**: 同一記事の再判定一致率 ≥ 95% (10 件×3 回で確認)。
- **規約 3 点セット** (有機的結合監査): ①消費者 = rebuild/KPI/preview/Daily Focus
  (既存)。②fill-rate 週次監査に judge coverage (候補のうち判定済み %) を登録。
  ③ラベル SSoT = pir_id は pir config を参照 (複製辞書なし)。

---

## 5. 段階計画

| Phase | 内容 | 状態 |
|---|---|---|
| **A** | 決定論の即効修正 (config のみ・A/B 済): integrated_cyber_ops `+NOT research` (15→8) / jp_targeted `victim=jp AND NOT geopolitical` (391→338、geopolitical 混入 33 件と keyword 残ノイズを除去) | **deploy 済 2026-07-23** |
| **B** | LLM 主題判定層の基盤 (§4): テーブル + Pir.llm_judge + judge バッチ + evaluator 合成 + has_match_criteria 修正 + **apt_leak で PoC** (候補ゲートを `kw{内部漏洩, リーク, i-Soon, Vulkan, Conti chat, leaked documents, data dump}` に広げ recall を回復しつつ LLM が主題を絞る) | 設計確定・実装待ち |
| **C** | 概念 PIR へ順次適用: **attribution** (候補=現行ツリー 258/30d、「国家による APT 帰属の公表が主題か」) → **integrated_cyber_ops** (候補=Phase A ツリー 8/30d、「統合作戦・軍事先端技術の文脈か」) | B 検証後 |
| **D** | **geopolitical_cyber 再定義** (§7、唯一利用者の文言判断が要る) → 再定義 + 候補ゲート + judge | 利用者判断待ち |
| **E** | 整理: 旧 strong_signals keyword 経路の撤去 (前段 SSoT Phase 3)、PIR_SIGNAL_FIRST 既定 ON 化、fill-rate/KPI の平常運用移行 | C/D 安定後 |

順序の根拠: B の PoC (apt_leak) は候補が少なく (3/日) 正解が自明 (現 2 件が FP) なので
検証コストが最小。C は同型の水平展開。D だけは技術でなく定義の問題なので分離。

---

## 6. Phase A の実測記録 (2026-07-23 deploy)

- **pir_integrated_cyber_ops**: `all[kw{AI攻撃,サイバー物理,宇宙サイバー,電磁波}, NOT category=research]`
  30d 15→8。落ちた 7 件は全て学術論文 (HQC 鍵復元 / Bit2Watt / Fuzz'EMup / CPS 復旧 /
  連合学習サーベイ / PQC 実装 / マルウェア拡散シミュレーション) = 期待どおり。
  `NOT geopolitical` まで足すと 2 件になり真陽性 (中国軍事 AI 物流の脆弱性 / IAI 電子攻撃
  プラットフォーム) を殺すため**不採用** — この PIR はサイバー×軍事の交差が本質で、
  geopolitical category は真陽性側に分布する。残余の言及系 2-3 件は Phase C で LLM 判定。
- **pir_jp_targeted**: `all[victim_country=jp, NOT category=geopolitical]` 30d 391→338。
  落ちた 53 件 = 中露艦艇 EEZ 実弾演習・日越揚陸艇・再軍備等の非サイバー (keyword
  日本標的/日本企業 の body 言及)。jp_company_breach (318) との関係 = 完全上位集合
  (jp_targeted は breach 系に加え apt/policy/advisory/malware の victim=jp を含む)。
  victim_country 未設定のグローバル記事 (日本にも言及する malware 警告等) は落ちるが、
  これは ally_cyber と同じ意図的トレードオフ — recall gap が観測されたら
  `any[victim=jp, all[kw弱, NOT geo]]` の弱補強枝を検討。

---

## 7. Phase D — pir_geopolitical_cyber の再定義 (利用者判断が必要)

実測が示す問題: 1,558 件 (全 PIR 最大、90d entity 2,905)。via countries=956 —
「8ヶ国 victim OR」は *日米英豪韓中朝露のどこかが被害国* なら何でも match する
定義で、intent=financial 269 件 (単なる犯罪 breach) まで混入。**定義が関心事を
表現していない** (④型)。

決定論の限界も実測済み: `category∈{geopolitical,policy} AND cyber語` = 560 件、
戦略語で更に絞っても 409 件で、Counter-OSINT 論・指揮統制論など非サイバー戦略記事が
残る一方、CISA 2015 延長・Cyber Shield 演習など真に関心のありそうな記事が落ちる。
**genre は述語で切れない** — 再定義 + LLM judge の二段が必要。

**提案 (利用者が文言を確定すること)**:

- description 案: 「国家 (特に中朝露・米・日) の**サイバー戦略・ドクトリン・能力・
  作戦・法制度**に関する分析・政策動向。単なる被害事案や一般地政学 (軍事・外交・
  経済) は含まない」
- 候補ゲート案: `all[category∈{geopolitical, policy}, kw{サイバー, cyber, ハッキング, hacking}]`
  (560/30d ≒ 19/日) + llm_judge「国家のサイバー戦略・能力・政策の分析が主題か」
- 代替: この PIR を分割する (例:「敵性国家のサイバー戦略」と「日米同盟のサイバー政策」)
  — Spotlight 対象 (pir_geopolitical_cyber) なので分割は Spotlight 構成にも波及する。

---

## 8. スコープ外と将来

- **pir_known_threat_followup (③型)**: 「続報」は記事単体で決まらない。dedup /
  corroboration cluster (R8 48h 窓) の集約情報と接続する別設計が必要。disabled のまま。
- **routing catalog 一元化 (前段 SSoT Phase 3 の ArticleFacts 統合)**: routing 挙動を
  巻き込むため、LLM 層とは独立に安全な窓で実施。急がない。
- **LLM judge の適用拡大の抑制**: L3 は①②型に**限定**する。L1/L2 で表現できる PIR に
  judge を足さない (決定論 preview・ゼロコスト・監査容易性が上位価値。
  wholesale LLM 化の却下は不変)。judge 対象は当面 4 PIR (apt_leak / attribution /
  integrated_cyber_ops / geopolitical_cyber) を上限の目安とする。

---

## 9. 検証まとめ

| 項目 | 基準 |
|---|---|
| Phase A | A/B 件数 + 落ちた記事の全数目視 (実施済、§6) |
| B PoC (apt_leak) | 既知 FP 2 件が不適合判定 / 候補 90 件のサンプル目視 / 再判定一致 ≥95% |
| C (attribution) | `起訴`系 FP (地震学者/殺人/レアアース) が不適合 / FSB 制裁級の真陽性が適合 |
| 運用 | fill-rate 監査に judge coverage 登録 / ops へ judge バッチ失敗通知 (silent 失敗禁止) |
| rollback | `PIR_LLM_JUDGE=0` (候補ゲートに degrade) / PIR 単位 llm_judge.enabled=false / DB 版 revert |

---

## 10. 実機検証記録 (2026-07-23 backfill + 検証、全基準クリア)

**backfill**: 90d 候補 2,363 件を 57 分で判定 (gemma4:26b think=False temp=0.0、
concurrency 2、~1.5-2.5s/件、**エラー 0**)。entity rebuild 後の 90d リンク総数
10,187 → **7,243** (keyword 時代 22,472 から通算 -68%)。

| PIR | 候補 (90d) | LLM 適合 | 検証所見 |
|---|---:|---:|---|
| apt_leak | 680 | **9** | **既知 FP 2 件とも不適合** (理由「FSB の制裁が主題であり、i-Soon は比較対象の例示に留まる」「主題は中国企業の軍事調達制限であり、漏洩情報は背景説明」= 言及≠主題の区別が機能)。適合 9 = KnowSec 内部文書リーク (i-Soon 級の真事案!)・The Gentlemen RaaS 内部流出 (×6 重複変種)・GRU 士官学校 (流出文書ベース)・LockBit インフラ流出 — 全て真陽性 |
| apt_attribution | 508 | **14** | 適合は Five Eyes 合同警告・DOJ 起訴 (Void Blizzard)・国務省懸賞金 (UNC5792)・台湾起訴・EU/英 FSB 制裁・18 機関合同 advisory — 全て「国家による帰属公表」。地震学者スパイ起訴・殺人起訴・レアアース輸出等の多義 FP は全滅。**FSB 制裁記事は apt_leak では不適合 / attribution では適合** = multi-label が意図どおり |
| integrated_cyber_ops | 21 | **14** | 適合 = 宇宙からの GPS 妨害・IAI 電子攻撃プラットフォーム・サイバー司令部 AI・軍事 AI 物流の脆弱性等の作戦レベル。市場予測・学術論文は排除済 (Phase A の NOT research + judge) |
| geopolitical_cyber | 1,154 | **421** | 再定義が機能: 適合 = 米中 AI 技術管理戦略・中国認知戦構想・CISA 組織再編・輸出管理政策等の「国家サイバー戦略分析」。不適合 = NATO 拡大論・イラン政治・EU 中国貿易摩擦・アフリカ紛争等の一般地政学 (旧 keyword 時代は 8ヶ国 victim OR で 90d 2,905 件 → **421 件**) |

**安定性**: 候補 10 件 × 3 回再判定で **10/10 完全一致 (100%、基準 ≥95%)** —
temperature=0.0 で実用上決定論。

**運用移行**: 夜間 pir-entity-rebuild (03:05) の前段で incremental judge (cap 80/夜、
定常流入 ~25-30 件/日)。週次 fill-rate 監査に backlog 行 (>50 で warn)。

---

## 11. recall (削りすぎ) 監査と修正 (2026-07-23、利用者指摘による)

precision 検証 (§10) の後、**却下側・ゲート外側の実データ監査**を実施。LLM judge の
却下 1,921 件は健全 (targeted 検査で FN なし) だった一方、**決定論側に実在の FN を
4 箇所確認し修正した**。共通原因 = 疎な意味フィールドへの AND とゲート語彙不足。

| 箇所 | 実 FN (実例) | 原因 | 修正 |
|---|---|---|---|
| state_ransomware | 米郡政府ランサム支払い / 米政府機関 Kairos / 英水道会社×3 | ransomware 835 件中 **62% が victim_sector 欠損** | **judge PIR 化** (gate=`is_ransomware AND NOT geo` 520 候補/90d)。sector 欠損も is_ransomware flag の地政学 FP も LLM が吸収 |
| attribution ゲート | EU 制裁記事 2 件 (帰属/起訴語なし・**category=geopolitical のため NOT geo では救えない**) | ゲート語彙不足 | **title スコープ** `{制裁, sanction}` 追加 (candidates 516→682、judge が非サイバー制裁を落とす) |
| jp_targeted | 新日本検定協会×4 / PoisonX 日本標的キャンペーン / 日本テレネット等 ~20件 | breach/incident の **victim_country 欠損 50%** | **title スコープ** `{日本, 日系}` × category∈{breach,incident,malware} 枝 (557→579、+22 ほぼ全件正当) |
| agency_alert | JVN/ICS 系機関 advisory 68 件 (Rockwell/Siemens/三菱電機) | category=advisory 限定 | category∈{advisory, **vulnerability**} (75→143、追加分は全件正当) |

**新 primitive: title スコープ keyword_any** (`{property: "title", op: "keyword_any"}`)。
タイトルに載る = 主題の決定論近似。body スコープでは「国内」⊂「米国内」の substring
誤爆や 制裁 の地政学 body 言及 (+1,384) が再発するため、title 限定が要
(`ArticleFacts.title_text`、subject-actor ゲートのタイトル fallback と同じ思想)。

**削りすぎでないことを確認した箇所**: emergency 225→1 (90d に実 Emergency Directive
0 件 = 見逃す対象が無い) / apt_leak・integrated・geopolitical の judge 却下 (borderline
2 件のみ、許容内)。

**上流の構造的所見 (未対処、別スレッド)**: victim_country 充足 = breach/incident の
50% (982 vs 欠損 1,013)。jp/ally 系 PIR の recall 上限を規定しており、恒久解は
summarizer の victim 抽出改善。ally_cyber は keyword 救済が困難 (米/英 等は語として
曖昧) なため、この上流改善までは既知の限界として受容。また geopolitical_cyber の
再定義文言「特に中朝露・米・日」により **EU のサイバー戦略は不適合判定**される —
含めたい場合は description に EU/NATO を明記する (利用者判断)。

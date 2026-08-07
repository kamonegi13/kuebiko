# Prepositioning Posture 常設情報要求 設計 — 「指標の陳列」から「持続する推論」へ

- 起点: 2026-07-12 セッション (重要インフラ脅威の「推論」化、進め方 3 択の 3)
- 前提修復: intent 抽出暗黒化の根治 + backfill (commit 0663350、同日)
- 上流: [synthesis_situation_ledger_design.md](synthesis_situation_ledger_design.md) /
  [jp_critical_infra_board_design.md](jp_critical_infra_board_design.md) /
  [synthesis_reliability_redesign.md](synthesis_reliability_redesign.md)
- 状態: **設計 (実装未着手)**。段A〜C は各段単独出荷可・flag 裏

---

## 0. 要約 (BLUF)

prepositioning (事前配置) は観測できない — 実測でコーパス 90 日 16,515 件中 prepositioning
intent 70 件、うち JP victim **0 件**。したがって「日本の重要インフラへの事前配置は進んで
いるか」への答えは、大小の事象 (世界の国家アクター行動・doctrine・JP 周辺の小信号) の
**集積からの推論**としてしか存在しない。

現 board (v3) はこの推論の**材料の陳列**までを実装した (行動中心レーン・doctrine×コーパス
集約)。だが board はステートレスで窓ごとに数え直すため、**小事象が時間をかけて 1 つの
推論に積み上がることが構造的に不可能**。これは synthesis が 2026-06 に治した病巣
(報告書ジェネレータ → 持続台帳) と同型であり、治療も同型にする:

> **「〈国家 N〉は日本の重要インフラに対する事前配置を進めているか」を situations 台帳の
> 常設 (standing) situation として一級市民化する。板 (board) はその射影。
> 変遷 = revision 履歴に刻まれる確度・リード仮説の軌跡。**

grounded 台帳が既に持つ ACH・対称 adversarial・確度較正・revision 遷移をそのまま使う。
新設するのは (1) `kind='standing'` という situation の種別、(2) code 所有の seed 4 件、
(3) 決定論の証拠収穫規則、(4) posture 固有の固定仮説フレーム、(5) board への第 3 レンズ射影。

---

## 1. この機能の本質

### 1.1 問題の構造

| | 観測 | 判断 |
|---|---|---|
| 通常のインシデント | 事象が報道される | 事象単位の situation が追跡 (既存) |
| prepositioning | **原理的に観測疎** (発見=失敗した prepositioning のみ) | **器が存在しない** ← 本設計 |

「静か≠安全」は board の凡例で明示済みだが、現状は静けさの**解釈** (=いまの posture の
最良推定) を誰も保持していない。窓を変えると数字が変わるだけで、「先月の推定に対して
今月の証拠は何を変えたか」という intelligence の中核操作が不可能。

### 1.2 なぜ台帳 (situations) に乗せるか

- 症状が同型: 「窓ごとの数え直し」= synthesis 旧設計の「run ごとの一発生成」。
  台帳化 (canonical estimate + 増分 ACH + delta 一級) がそのまま処方箋になる。
- 機構が既製: ACH 仮説行列 / 対称 adversarial (週次 sweep) / 確度 cap (方向中立) /
  delta_type 決定論分類 / missing (不足証拠) / indicators (I&W) — すべて
  `situation_revisions` の既存列に乗る。**新しい評価機構は作らない。**
- 過剰帰属リスク最大の領域 (何でも Volt Typhoon に見える病) に対し、grounded の
  「対称な客観性 (脅威過大も穏当過小も禁忌)」が既製の防具になる。

### 1.3 PIR との役割分離 (重要)

「常設の情報要求」は概念上 PIR と重なるが、器は分ける:

| | PIR | standing situation |
|---|---|---|
| 役割 | **関心の定義と評価** (何を集め・何を重要とするか) | **較正された判断の状態** (いまの最良推定 + 軌跡) |
| 状態 | 持たない (criteria の集合) | 持つ (claim / confidence / hypotheses / revisions) |
| 更新 | 人が編集 → compile | 証拠駆動の ACH 再評価 |

これは確立済みの「PIR=関心の定義と評価 / routing=配信」の役割分離と同じ形。
接続は既存列 `situations.pir_ids` で行う (standing-cn → pir_china_apt, pir_jp_targeted 等)。
**PIR に判断状態を持たせる案は採用しない** (§9)。

---

## 2. 中心オブジェクト: Standing Situation

### 2.1 定義と seed (code 所有)

```
kind='standing' の situation。1 件 = 1 つの常設 intelligence question。
初期セット (敵性 4 カ国 × 1 question、code 所有定数):

  s-standing-prepos-cn  「中国の国家アクターは日本の重要インフラへの事前配置を進めているか」
  s-standing-prepos-kp  「北朝鮮の …(同型)」
  s-standing-prepos-ru  「ロシアの …(同型)」
  s-standing-prepos-ir  「イランの …(同型)」
```

- **id は明示の安定 id** (title ハッシュではない)。title は claim 追従 (P2) の対象になり
  うるため、id を title から導出すると identity が壊れる。
- **seed loader** が synthesis run 冒頭で冪等開設 (`INSERT OR IGNORE` 相当)。
  ユーザ定義不可 — jobs メタ (コード所有) と同じ思想。増減はコード変更 = レビューを通る。
- detect-new の `active_titles` / dup ガード (`sit_keys`) に常載し、LLM が同じ問いの
  event situation を重複開設しない。
- **敵性 4 カ国限定** (確定原則の継承)。証拠が薄い国 (IR 等) は低確度が正直に居座る —
  それ自体が「暗域の定量化」の延長で情報価値を持つ。

### 2.2 kind 列とライフサイクル除外

- `situations` に `kind TEXT NOT NULL DEFAULT 'event'` を追加 (§6)。
- `_sweep_lifecycle` (ledger.py) は **kind='standing' を skip**。常設は dormant/close
  しない — 静穏期間こそ問いが生きる時間 ("静か≠安全")。
- 静穏の正直さは status でなく**描画**で表現: 最終評価の鮮度 + 窓内 direct evidence 数を
  board カード / situations UI に常掲 (「直近 30 日 直接証拠 0 件・最終評価 7/10」)。
- `claim_type='structural'` を既定にする (既存 Literal に存在)。鮮度減衰の表示規則は
  structural の既存扱いに従う。

### 2.3 event situation との関係

- Volt Typhoon 型の名指しキャンペーンは従来どおり **event situation** として開き、
  `situation_relations` (`shared_nation` / `same_actor`) で standing と結線される
  (rebuild_relations は既存・毎 run 冪等)。standing はキャンペーン situation を
  置き換えない — キャンペーンは「何が起きているか」、standing は「それらが日本への
  posture について何を意味するか」。

---

## 3. 証拠収穫 (決定論・LLM 不使用)

### 3.1 汎用 matcher に相乗りしない

既存の割当 (`assignment.py`: anchor / nation / token) をそのまま使うと、standing は
国名 anchor を持つため**戦線バケットと同じ吸着病**を再発する (キーウ 110 証拠の教訓。
「国は必要条件であって同一性ではない」は 7/04 に確立済み)。standing 専用の狭い harvest
規則を毎時 refresh / 定時 run の割当段に追加し、`situation_evidence.assigned_by='standing'`
で監査可能にする。

### 3.2 収穫規則 (国家 N の standing に対して)

| 規則 | 条件 | 根拠 |
|---|---|---|
| R1 直接 | `socio_political_intent='prepositioning'` (**全確度**、low 含む) かつ 国 N (辞書ゲート済みアクターの nation、または involved_country) | 問いの直接証拠。low は「仮説級の弱シグナル」として ACH が polarity/重みを判断する — 収穫段で捨てない (抽出層の修復と同じ原則) |
| R2 隣接動機 | 辞書ゲート済み国家アクター (nation=N) × CI 分野該当 (doctrine 標的 or 観測 victim_sector が NISC 分野) × intent ∈ {espionage, disruption} | 侵入の目的は後から判明する。CI への espionage 足場は prepositioning 仮説の関連証拠 |
| R3 JP 観測 | victim_country='JP' × NISC 分野該当 × **辞書ゲート済み帰属で nation=N** | 帰属なし JP 事象は standing に入れない (過剰帰属の再発防止)。帰属なしは event situation / board 観測レーンに残る |

- 上限: 1 run × 1 standing あたり 30 件 (定数)。超過は新しい順 + detection_log に切り詰めを記録
  (no silent caps)。
- polarity は収穫段では 'neutral'。支持/反証の判定は ACH 評価の仕事 (既存分担どおり)。
- **geopolitical intent (coercion/territorial 等) は収穫しない** (§9)。地政学的文脈は
  synthesis の nation correlation レーンが既に並置しており、緊張の高まりを posture 確度に
  直接注入するのは因果推論の混入になる。

### 3.3 収集タスキングへの接続 (人手ループ)

`revisions.missing` (不足証拠) は現状表示のみで下流接続なし (調査で確認済み)。standing の
missing は**そのまま収集タスキングの文面**になる (例: 「JP の OT 境界機器での N 系 TTP の
検知報告」)。v1 は接続を自動化せず、**人が読んで PIR criteria / feeds を更新**する
(機械=検出 / 人=発見 / 機械=解決 の確立 3 段)。triage 閾値の一律緩和はしない。

---

## 4. 評価 (既存 grounded ACH の適用)

### 4.1 posture 固有の固定仮説フレーム

`hypotheses.py` に POSTURE フレームを追加 (CYBER_CORE / GEO_CORE と同じ code 所有 taxonomy。
LLM に仮説を自作させない):

```
H-P1 active_prepositioning_jp   日本 CI への足場確保が進行中 (直接・間接証拠あり)
H-P2 global_activity_no_jp_yet  世界的に CI prepositioning を進めるが日本標的の直接証拠はない
H-P3 other_motive               観測活動は諜報/金銭等が目的で、事前配置と評価する根拠がない
H-P4 reporting_artifact         観測の偏り・報道増幅 (SHARED から再利用)
```

### 4.2 確度の較正 (方向中立の cap を posture 用に 1 つ追加)

既存機構 (evidence_ceiling / attribution cap / adversarial 降格) はそのまま。追加は 1 点:

- **H-P1 を leading にする場合、日本を明示する直接証拠 (JP victim の帰属済み観測、
  または公的勧告の日本名指し) が無ければ confidence は moderate を上限とする。**
- 対称性: 逆方向にも cap — **観測ゼロは H-P3「配置なし」の支持証拠にならない**
  (不在の証明にしない。「静か≠安全」の評価層への実装)。JP 観測ゼロで H-P2 が leading の
  とき、その確度は「世界行動 + doctrine の実在」で接地する (これは観測可能なので
  moderate〜high があり得る)。

過確信だけを防ぎ、仮説は禁止しない — 抽出層の修復 (0663350) と同じ原則を評価層でも守る。

### 4.3 再評価予算 (飢餓させない)

調査で確認した 2 つの構造問題に対処する:

1. **証拠ゼロの日はキューに乗らない** (`unassessed_evidence` は新着のみ返す) →
   staleness 規則を追加: standing は最終 revision から **7 日超で無条件に候補投入**
   (週 1 回は必ず問い直す)。weekly の対称 sweep (全 active 反証試行) には kind 問わず
   含まれるため、反証方向は既存機構が担保する。
2. **優先度式で戦線バケットに常敗する** (新着証拠数順) → `select_reassessments` を
   2 段化: **standing 候補から先に最大 2 件** (daily cap 6 の内数 = 予約枠)、残枠を
   従来式で event に配分。総 cap 不変 = **追加 LLM コストなし** (event 側が 6→4 に
   なる日は P1 の繰越排水 (bare 証拠先行永続化 + weekly 12) が吸収する)。

---

## 5. 射影

### 5.1 board への第 3 レンズ (国粒度カード)

board 頭部 (横断キャンペーンの隣) に **posture カード × 4 カ国**:

```
[中国]  推定: 世界的に CI 事前配置を推進、日本標的の直接証拠なし (H-P2)
        確度: moderate   直近: 7/10 escalated (npm 供給網の JP 到達)
        直近30日 直接証拠: 2 件 / 関連: 14 件    軌跡: ▁▂▂▃▃ (確度×リード仮説)
```

- **分野行 (ラダー / 世界行動) には一切折り込まない** — 確定原則「世界行動は独立レンズ」の
  延長で、**仮説確度も独立レンズ**。分野粒度の posture 表示は作らない (国粒度で判断した
  ものを分野へ按分するのは精度の捏造。分野との対応は doctrine 標的分野の列挙で示す)。
- カードのデータ源は `situations` + `situation_revisions` のみ (board 集計と独立)。
  クリックで situations UI の該当 situation (revision 軌跡) へ遷移。

### 5.2 synthesis render での扱い

- standing の **delta revision (escalated / hypothesis_flip / strengthened / weakened) は
  moved pool に自然参加** — 「接地された変化が headline」の pool 2 段化 (092e8309) と
  整合。posture の変化は日本×最上位 PIR なので salience は自然に高い (新規重みは足さない)。
- no_change の standing は従来どおり standing pool (cog_section 候補)。**常設だからと
  いって毎日 headline を占有しない** — 変化が無い日は変化が無いと言う (静穏正直報告)。

### 5.3 ステップ 2 (as_of 変遷) との関係

posture の軌跡の正 (canonical) は **revision 履歴**であり、board の as_of 再集計は
観測レーンの履歴射影 (較正・検証用の別ツール)。両者は補完であり代替ではない。
as_of 化の seam (jp_ci_board の now / threat_operations snapshot の基準時刻 / cache key)
は調査済みだが、**本設計のスコープ外** (別作業として実施可)。

---

## 6. スキーマ変更

```sql
-- situations に 1 列のみ追加
ALTER TABLE situations ADD COLUMN kind TEXT NOT NULL DEFAULT 'event';
-- 'event' (既定・従来) / 'standing' (常設)
```

- SQLite `_SCHEMA` (schema_sql.py) と `pg_schema.py` の**両方**に追加 (確立済み教訓)。
  PG は既存 DB への `ALTER TABLE ... ADD COLUMN` を**末尾**に置く (新列 index を本体に
  置くと既存 DB で UndefinedColumn → crash loop の既知 gotcha)。index は不要
  (standing は 4 行、走査は status index で足りる)。
- `situation_evidence.assigned_by` に文字列値 'standing' を追加 (スキーマ変更なし)。
- 新テーブルなし。parity test (SQLite/PG) は既存の枠に kind を追加。

---

## 7. コスト

| 段 | 追加コスト |
|---|---|
| 収穫 (毎時/定時) | LLM ゼロ (決定論 SQL + 定数照合)。行数 +≤120/run (30×4) |
| 再評価 | **ゼロ** (daily cap 6 の内数予約。総呼出数不変) |
| 週次 sweep | 既存対象に +4 件 (adversarial 1 呼出/件、weekly のみ) |
| board カード | 読み取りクエリ 1 本 (situations×revisions 直近、TTL cache 同居) |

daily run の 30 分予算に対する増分は実質ゼロ〜数十秒。

---

## 8. 確定原則との整合

| 原則 (出所) | 本設計での扱い |
|---|---|
| 外挿/予測しない (board I&W) | claim は「現在の posture の推定」= 状態評価。攻撃予測はしない。indicators 列 = 何が見えたら評価が動くか (I&W) で将来は語る |
| 因果非主張 (相関のみ) | 地政学 intent を証拠に入れない (§3.2)。relations は共有 anchor/国のみ |
| 世界行動をラダーに折り込まない | 確度も折り込まない (第 3 の独立レンズ、§5.1) |
| 敵性国家限定 | seed 4 カ国固定・code 所有 |
| 確定帰属は辞書ゲート・人承認 | R1-R3 すべて辞書ゲート済み帰属のみ収穫。帰属なし JP 事象は不参加 |
| 仮説を禁止せず過確信のみ防ぐ | low 確度 intent も収穫 (§3.2 R1)。cap は方向中立 (§4.2) |
| 静か≠安全 | 観測ゼロ≠H-P3 支持 (§4.2)。lifecycle 除外 + 鮮度常掲 (§2.2) |
| 一発勝負の廃止・落選に理由 | 収穫の切り詰め/除外は detection_log に記録 (§3.2) |

---

## 9. 採用しない代替案 (検討済み・再提案不可)

1. **分野×国 = 64 常設仮説**: 再評価予算の崩壊 (P1 飢餓の再発) + 分野単位の証拠は疎で
   確度が捏造になる。国粒度 4 件 + doctrine 分野列挙が正。
2. **geopolitical intent (coercion/territorial 等) の posture 証拠化**: 緊張≠配置。
   確度への地政学サーチャージは因果推論の混入 + 過剰帰属の再発。文脈は nation correlation
   レーンの並置で足りる。**→ 90 日 geopolitical 追加 backfill も v1 不要**。
3. **汎用 assignment matcher への相乗り**: 国 anchor による戦線バケット吸着の再発 (§3.1)。
4. **LLM による常設仮説の開設/廃止**: 常設問いは mission に紐づく (code 所有 seed)。
   detect-new が開けるのは event のみ。
5. **PIR への判断状態の統合**: PIR は関心の器 (§1.3)。二重定義は双方を壊す。
6. **posture の合成スコア (指標の重み付き合算)**: 説明可能性を失い、較正もできない。
   ACH のリード仮説 + 較正確度 + 根拠列挙が正 (grounded の中核決定)。
7. **JP CI 事業者の資産インベントリ由来の露出証拠**: 禁止済み (board 確定原則) — 本設計
   でも収穫源にしない。

---

## 10. 段階移行 (各段 単独出荷可・flag `STANDING_SITUATIONS`)

| 段 | 内容 | 出荷判定 |
|---|---|---|
| **A 器と収穫** | kind 列 + seed loader + lifecycle 除外 + R1-R3 収穫 (deploy-dark: 評価には回さず証拠が貯まるだけ) | detection_log / situation_evidence で収穫の実データ検分 (backfill 済みなので初日から流入が見える) |
| **B 評価接続** | POSTURE 仮説フレーム + staleness 規則 + 予約枠 2 段選定 + 初回評価 (90 日 seed 証拠 = 歴史シードで cold start 回避) | 初回 revision 4 件の目視 + 数日の delta 観察 (confidence が静穏で動かないこと) |
| **C 射影** | board posture カード + situations UI の kind 表示 + moved pool 参加 | 実ブラウザ検分 (モバイル 390px 含む) |

rollback = flag off (収穫停止・評価対象から除外。データは残る)。

## 11. 検証 (受入基準)

- seed 開設が冪等 (再デプロイ/再起動で重複 0)
- 実データ: Volt Typhoon 系記事が s-standing-prepos-cn に assigned_by='standing' で収穫される
- 歴史シード初回評価で H-P2 相当が moderate 接地 (CN)、証拠疎の国は low が正直に付く
- 静穏 14 日で dormant にならない / confidence が証拠なしに変動しない
- 戦線バケット多発日 (キーウ型) にも standing 再評価が予約枠で確保される
- board カードの確度・軌跡 = situation_revisions と一致 (別集計を作らない)
- pytest (SQLite/PG parity 含む) / mypy --strict / 390px 横スクロールなし

## 12. 較正 (backfill 完了後に数値を確定)

- R1-R3 の週次収穫流量 → 上限 30/run の妥当性、予約枠 2 の妥当性
- intent low 混入率 → R1 を全確度で維持するか low を関連証拠扱いに落とすか
- ingest vs backfill の intent 一致率検算 (1 回)
- 1 週間の再ベースライン監査 (カテゴリ内・新レジーム内で被覆を測る)

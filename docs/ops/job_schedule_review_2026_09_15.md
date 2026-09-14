# ジョブ実行要領の全面見直し (2026-09-15、全ローカル LLM が Gemma 4 26B MoE 系になったことを受けて)

## 1. 前提 (何が変わったか)

- 2026-09-14 深夜に narrative ティアを 30 層混合 SFT (kuebiko-sft:n17m30) へ統合し、常駐は **s17 (fast 8 処理 + ACH) と n17m30 (narrative 4 処理)** の 2 本。外部は dialog ティア (claudecode:haiku) と reasoning ティア既定 (claudecode:sonnet、ただし ACH は s17 上書き) のみ。
- スケジュールと `max_runtime_minutes` の多くは **Dense 31B が narrative を担っていた時期** (〜09-08) の実測で決めた値。31B は 1 呼出 p50 101 秒だった。

## 2. 実測 (run_logs / docker logs / llm_usage、2026-08-15〜09-15)

### 2.1 モデル別の 1 呼出所要 (subprocess pipeline 分、30 日)

| モデル | 役割 | 呼出/日 | p50 | p90 | 出力 tok/s |
|---|---|---|---|---|---|
| gemma4:31b (Dense) | 旧 narrative | 3 | **101 s** | 179 s | — |
| kuebiko-sft:26b (N1) | 旧 narrative (09-08〜14) | 3 | 32 s | 54 s | 46 |
| kuebiko-sft:n17s2d / n17m30 (30 層) | 現 narrative | — | 52 s (出力 1.3k tok) | 157 s (出力 3.5k tok) | 20〜54 |
| gemma4:26b (base) | detect-new 等 | 774 | 5.2 s | 17.5 s | 24 |
| kuebiko-sft:s17 | fast 全般 | 73 (+ in-process 分) | 5.4 s (出力 94 tok) | 15 s | 21 |

- **fast 系は速くなっていない** (base も s17 も同じ 26B MoE、5 秒台)。速くなったのは **narrative だけで 31B 比 3 倍**。ただし 30 層モデルは出力が 1.7 倍長いので 1 呼出の所要は N1 より長い (52 s)。
- **出力上限 4,096 tok に到達した呼出あり** (22:29 の event news、4,096 tok ちょうど)。30 層モデルは 3,000 字超を書くため `DEFAULT_MAX_TOKENS` が切り詰めている可能性 (凍結 39 件で JSON 不正 1 件が出た原因候補)。

### 2.2 pipeline の所要 (秒)

| pipeline | 14 日 p50 / p90 / max | 直近 3 日 p50 / max | max_runtime (分) | 判定 |
|---|---|---|---|---|
| pir-spotlight | 857 / 1,188 / 1,626 | 606 / 1,165 | **10** | 上限が実測 p90 の半分 |
| morning-brief | 376 / 774 / 1,081 | 174 / 246 | 8 | 3 日は収まるが p90 は超過 |
| evening-brief | 353 / 1,432 / 1,800 | 275 / 400 | 8 | 同上 (1 件 timeout 1,800) |
| weekly-status-synthesis | 967 / — / 990 | 990 | 25 | detect-new 2 呼出 × 319 s (23k tok prompt) が 10 分 |
| direct-rss-fetch | 239 / 565 / 1,800 | 497 / 658 | 12 | s17 84 呼出/run ≈ 590 s、上限に近い |
| weekly-recap | 405 / 545 | 230 | 25 | 余裕 |
| web-scraper-watchers | 25 / 122 / 363 | 121 / 303 | 5 | 余裕 |

in-process (job_run_log に所要なし、docker logs の summary から):

| ジョブ | 実測 | max_runtime | 判定 |
|---|---|---|---|
| eventnews-hourly | 228 s (1 件生成) / **1,116 s (4 件生成、30 層)** | **5** | 上限を 4 倍超過。次の時刻 (:20) に食い込む |
| body-translate-backlog | :10 台に s17 46 呼出 / 16h ≈ 0.7 分/時 | 15 | 余裕 |
| pir-judge-hourly | :40 台に s17 24 呼出 / 16h ≈ 0.3 分/時 | 5 | 余裕 |

### 2.3 時間内の LLM 使用率と競合

- 毎時の LLM 実行時間: rss 約 10 分 + in-process 約 3 分 = **約 13 分/時 (22%)**。GPU に余裕はある。
- **Ollama のモデル load が 1 日 34 回**。s17 / n17m30 / (n17s2d) / embed をジョブごとに切り替えるため。1 回 10〜20 秒 + 初回呼出の warm-up。
- 毎時 11 本の interval ジョブがオフセット (:00 :10 :12 :15 :15 :20 :20 :25 :40 :45 :50) で並び、eventnews-hourly が :20 → :39 まで伸びると :40 の refetch・:45 の pir-judge と重なる。

## 3. 発見

1. **上限値の陳腐化**: `max_runtime_minutes` は watchdog の再実行判断と heavy 帯の衝突判定に使われる。spotlight 10 分・eventnews-hourly 5 分・ブリーフ 8 分は実測と乖離しており、**正常な長い run を「取りこぼし」と誤判定しうる**。
2. **narrative の 3 倍速は活きている**: ブリーフは 3〜5 分、spotlight は 10 分に短縮。夜間の重いブロックを圧縮する余地がある。
3. **出力上限**: 30 層モデルの長文に対し event news の `max_tokens` が既定 (4,096) のまま。spotlight は 6,144。
4. **細分化の副作用は「時間」でなく「切替」**: 合計 LLM 時間は小さいが、モデル切替 34 回/日と、伸びたジョブの重なりが実効速度を落とす。
5. **detect-new の prompt 23k tok × 319 s** は base 26B の問題ではなく入力量の問題 (候補 150 件)。cap を下げるか、週次専用の候補上限を設ける。

## 4. 提案

### A. 即時・低リスク (設定値のみ、rollback = 値を戻す)

| 対象 | 現在 | 提案 | 根拠 |
|---|---|---|---|
| pir-spotlight max_runtime | 10 | **25** | p90 20 分 |
| eventnews-hourly max_runtime | 5 | **25** | 実測 18.6 分 (4 件生成) |
| morning-brief / evening-brief max_runtime | 8 | **15** | p90 13 分 (31B 期の 24 分は不要) |
| event news の max_tokens | 4,096 (既定) | **6,144** | 4,096 到達を実測、spotlight と揃える |
| weekly-status-synthesis の detect 候補 cap | 150 | 100 | 319 s × 2 → 200 s × 2 |

### B. 毎時ジョブの 2 チェーン化 (コード変更あり、段階導入)

11 本の interval ジョブを **モデル順**に並べた 2 本の直列チェーンにまとめる。各段は失敗を隔離 (1 段が落ちても次段へ)、段ごとの所要を job_run_log に記録する。

- **hourly-collect (:00 起点、s17 → n17m30 の順)**: direct-rss-fetch → web-scraper-watchers → grok-briefing → embedding-backfill → pir-judge-hourly → **eventnews-hourly** (最後に narrative モデルへ切替、切替は 1 回/時)
- **hourly-upkeep (:30 起点、s17 のみ)**: body-translate-backlog → body-refetch-backlog → nvd-cvss-refresh → public-reachability
- 独立のまま: job-recovery-watchdog (30 分)、ransomware-live-ingest (3 時間)、daily-heartbeat

効果: モデル切替を 1 日 34 回 → 約 48 回上限だが実質 2 回/時 (s17 ↔ n17m30) に整理、重なりゼロ、デプロイ安全帯が「チェーン終了後〜次の :00」に固定される。学習前の quiesce も 2 ジョブの終了を待てばよい。
リスク: チェーン前段の遅延が後段を押す (rss が 10 分伸びると eventnews が :30 開始)。max_runtime はチェーン全体で 45 分。

### C. 夜間ブロックの再配置 (設定のみ)

| 現在 | 提案 | 狙い |
|---|---|---|
| 00:10 maintenance / 00:30 pir-entity-rebuild / 00:55 recap (月) / 01:30 synthesis (月)・taxonomy (日)・mitre (火) / 02:15 monthly / 02:40 ua (月) / 04:20 distill (月) / 04:40 drift (日) / 04:45 goldset (土) / 04:50 spotlight / 06:30 brief | **00:10〜01:40 に夜間バッチを寄せる** (maintenance → rebuild → 週次系を曜日別に 00:55/01:10/01:25) / **04:30 spotlight** / 04:45 goldset (土) / 05:00 drift (日) / 06:30 brief | 01:40〜04:30 の **約 3 時間を GPU 静穏帯**として学習・評価・取込に使う (現状は 02:15〜04:20 の 2 時間) |

学習 (12 時間級) は依然ブリーフを跨ぐので、静穏帯は評価・取込・短い実験用。

### D. 触らないもの

- 週次ジョブの曜日分散、reactive の auto-trigger-synthesis (debounce 6h)、ransomware 3 時間間隔。

## 5. 実施記録 (2026-09-14 23:17 JST、利用者承認で A/B/C を即時実装)

- A: max_runtime (spotlight 25 / 朝夕ブリーフ 15 / eventnews-hourly 25)、event news max_tokens 6,144、
  nominate pool weekly 150 / monthly 200 — commit 1298cd57。
- B: `kind="chain"` + hourly-collect (:00) / hourly-upkeep (:30) を追加、段の単独ジョブ 10 本を DB で OFF
  (rollback = 段を ON、チェーンを OFF)。段の所要は job_run_log に `chain=… elapsed=…` で記録。
- C: pir-entity-rebuild 00:25 / 週次系 01:10 (曜日別) / monthly 01:40 / ua 01:35 / distill 01:45 /
  **spotlight 04:30** / triage-drift 日 05:00。GPU 静穏帯 = 01:50〜04:30。
- 副産物: `apply_job_registry` が offset の変更を無視していた欠陥を修正。

## 6. 実装順序 (計画時)

1. A を先に (UI「実行管理」または job_registry の既定値 + event news max_tokens のコード 1 行)。1 日観察。
2. B は `kind="chain"` を job_registry に足す実装 (段の失敗隔離・所要記録・UI 表示)。テスト付きで 1 日、shadow なしで切替 (rollback = 旧 11 ジョブの再有効化)。
3. C は B の後、時刻表を UI から変更。

## 7. 監視

- job_run_log に段ごとの所要を持たせ、`weekly-fill-rate-audit` 同様に週次で p90 を見る。
- Ollama の load 回数 (server.log の runner started) を日次 heartbeat に載せる。

---

## 5. 追補 (2026-09-15 朝): 台帳の更新上限も 31B 起点だった

§1 で「スケジュールと `max_runtime_minutes` の多くは Dense 31B 期の実測で決めた値」と書いたが、
**同じことが状況総括の分析幅にも当てはまっていた**。`src/assessment/stateful.py` の
`_MAX_UPDATES_BY_PERIOD` (1 run で増分 ACH にかける Situation 数) のコメントは
「Dense 31B は 1 呼出が数十〜百秒級のため予算制」と明記していた。

### 実測

| 項目 | 実測値 |
|---|---|
| 増分 ACH 1 呼出 (s17 / 26B) | **中央 32 秒** (26-57 s、入力 2.4-7.0k tok、出力 0.5-1.6k tok) |
| detect-new 1 呼出 (base 26B) | 92.7 s (入力 17.0k tok) |
| 上限の拘束 | 25 run で `updated` がほぼ常に cap 貼り付き、**繰越 14-107 件** |
| 証拠の滞留 | 中央 0.8 日 / **p90 3.6 日** (未読 59 件の最古 7.6 日) |
| active situation 数 | 216 (10 日で 198 → 216) |

31B 期は narrative 1 呼出 p50 101 秒だったので、ACH の実コストは約 1/3 になっている。
破綻はしていないが、**上限は「証拠反映の遅れ」を買っている**状態だった。

### 変更 (daily のみ)

| 対象 | 旧 | 新 | 根拠 |
|---|---|---|---|
| `_MAX_UPDATES_BY_PERIOD["daily"]` | 6 | **12** | +6 × 32 s ≒ +3.2 分/run。繰越とp90 滞留が半減する見込み |
| `render._MOVED_SECTION_MAX["daily"]` (新設) | — | **12 + 件数明示** | 台帳の鮮度と報告の幅を分離するためのガード |
| morning/evening の `max_runtime_minutes` | 15 | **20** | 上記 +3.2 分 |

**なぜ幅のガードを別に持つか**: 変更前は台帳の更新上限がそのまま「報告に載る変化の件数」に
なっていた (standing だけは salience 上位 3 件に絞られていたのに moved は無制限)。台帳を広げると
報告まで一緒に広がり、プロンプトが **判定 1 件 ≒ 600 tok** で伸びる。両者は別の要求なので分ける。
落とした件数はプロンプトに明記する (no-silent-caps)。落ちた判定も **PIR ロールアップには残る**
ので関心領域そのものは消えない。

### 測ったうえで**入れなかった**もの

- **PIR 別の最低 1 件保証 (daily)**: 過去 73 窓で moved は中央 5 件、上位 12 の cap が効く窓は
  1 件のみ、PIR を落とす窓も 1 件のみだった。実需が無いので入れない (YAGNI)。
- **weekly/monthly の幅の cap**: ここで一律に切ると報告が壊れる。別途設計する (下記)。

## 6. 次の課題: weekly / monthly の幅 (未着手)

| 期間 | 判定数 (中央) | render プロンプト (中央 / 最大) | 出力上限 |
|---|---|---|---|
| daily | 10 | 8.7k tok / 15.8k | 6,000 tok |
| **weekly** | **92** | **40.2k tok / 55.9k** | 6,000 tok |
| **monthly** | **137** | **60.7k tok / 73.4k** | 6,000 tok |

weekly は 92 件の判定を約 4,000 字に押し込んでいる (1 件あたり 40 字強)。
[SYNTHESIS.md §41](../research/llm_training/SYNTHESIS.md) が観察した「chain の因果/相関の峻別が
同型列挙に退化」は、モデルの能力ではなく**この幅**が原因である可能性が高い。

ただし weekly を salience 上位 8-12 に単純に切ると、測定した 11 窓**すべて**で PIR を落とす
(`pir_general_agency_alert` ×9、`pir_apt_attribution` ×6、`pir_sw_supply_chain` ×4 ほか)。
daily で不要だった PIR 別の最低保証が、weekly では必要になる。
想定形: **salience 上位 N + PIR 別に最低 1 件 + 残りは件数のみ明示**。

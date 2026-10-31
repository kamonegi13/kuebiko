# O3-ALERT-DEF: O3 の警報の規則 (定義) と O3-ALERT-REPLAY の事前登録

- 下書き: 2026-10-31。**この文書の main へのコミットが、O3-ALERT-REPLAY の数えと O3 の実装より前**。
  作成時点で O3 のコード・警報の判定の結果・再生の出力は 1 件も存在しない
- 依存: O3-ALERT-DEF (本書) → O3-ALERT-REPLAY (§5 の手順で過去 14 日に当てて数える) → O3 (本書の規則を実装する)。
  O3 の実装は本書の条件・閾値・窓・抑止を変えない。変えるなら §7 の変更履歴を書いてから
- 作成時に読んだもの (読み取りのみ): 計画台帳の `O3`・`O3-ALERT-DEF`・`O3-ALERT-REPLAY`・`ENV-DISK`、
  `src/ops/write_failures.py`・`src/tools/tier_load_alarm.py`・`src/tools/emergency_fallback.py`・
  `src/tools/model_end_state.py`・`scripts/check_model_end_state.py`・`src/ui/api/pages.py` の health-status、
  `scripts/deploy_site.sh`・`scripts/backup_loop.sh`、本番の DB (SELECT)・`GET /api/v1/health-status`・ホストのファイルの mtime

## 0. 分類と再生の可否 (先に結論)

| 対象 | 警報 | 再生 (O3-ALERT-REPLAY) |
|---|---|---|
| ホストの成果物の鮮度 | H1 公開面 (標準) の配信の停止 / H2 アドバンスドの書き出しの停止 / H3 バックアップの欠け | **不可**。ホストのファイルの mtime・中身で、DB に履歴が無い。**前向きの観測だけ** |
| モデルの組の整合 | M1 2 枠の終着の外 / M2 ML の学習時のモデルと割当の不一致 / M3 緊急の受け皿の使用 (DB 側) | **可** (DB の版履歴・記録・subprocess のログ `run_logs`) |
| 記録の失敗 | R1 記録の失敗 (subprocess) / R2 記録の失敗 (API プロセス) / R3 記録の沈黙 | R1・R3 は**可**。R2 は**不可** (プロセス内の計数で履歴が無い) |
| 容量 | C1 ホストのディスクの空き / C2 VM ディスク (PG の volume) の空き / C3 保持の purge の停止 | **不可**。容量・最古の行の履歴は DB に無い (消えた行は過去の状態を残さない)。**前向きの観測だけ** |

R2 は台帳の注記 (再生不可は鮮度と容量の 2 つ) に無い 3 つ目の再生不可。§6 に記録した。

## 1. 既存の警報 (重複させない)

| 既存 | 実体 | 抑止 | 本書との関係 |
|---|---|---|---|
| TIER-LOAD-ALARM | `tier_load_alarm.record_tier_load_failure` → ops「model_tiers の DB 読み込みに失敗しました」、health の `model_end_state.tier_load_failures` (API プロセスのみ) | プロセスごとに 1 日 1 回 (プロセス内の `_last_notified`) | **新しい警報を作らない**。M1 の判定に使う割当の読み込みが失敗した回は M1 を「判定不能」にし、警報は TIER-LOAD-ALARM に任せる |
| 緊急の受け皿 (`llm_emergency_fallback`) | `emergency_fallback.record_emergency_fallback` → ログ + 計数 + ops「緊急の受け皿が使われました」 | 1 日 1 回 (プロセス内)。**イベントループの無い同期の文脈では ops に出ない** (ログと計数だけ) | M3 が DB (`llm_usage`) から拾い、既存の通知が同じ 24 時間に出ていれば出さない (§3 M3) |
| health-status `model_end_state.violations` | `check_live_assignments()` を API で返すだけ (ops には出ない)。`scripts/check_model_end_state.py` は手で叩く | なし | M1 は同じ関数を定時に当てて ops へ出す。条件は同じ関数 (二重の定義を作らない) |
| health-status `write_failures` | `src/ops/write_failures.snapshot()` (プロセス内、再起動で 0) | なし (ops には出ない) | R2 が増分を ops へ出す |
| 公開面の到達性 (`public_reachability`) | 公開 URL を HTTP/2 で叩く。2 連続の失敗で ops「🔴 公開面に到達できません」 | job の毎時 | **到達できるが古い**は見ない (10-22 から wrangler の版で配信が止まった件は到達性では検知できない)。H1 が補う |
| runner 検知 / ジョブ自動リカバリ / daily heartbeat | 「run 失敗 (runner 検知)」・「ジョブ自動リカバリ: …」・heartbeat の「製品鮮度」(DB の生成物) | 各実装 | DB の生成物の鮮度は heartbeat が見る。本書はホストのファイルだけを見る |
| OrbStack watchdog (LaunchAgent) | `scripts/orbstack_watchdog.sh` が ops へ直接 | 1 時間の回数上限 | VM の無応答は対象外 (本書はアプリから評価するので、VM が止まれば本書の警報も止まる) |
| Grok の取得の失敗 | runner 検知・P0-25 の描画失敗の要約 | P0-25 | Playwright の `data/playwright/state.json` は**警報にしない**。mtime は手で取り直した時刻で鮮度ではなく、cookie の期限の最小は既に過去 (-3.5 日) でも取得は動いている (どの cookie が効くかを名前で特定しない方針)。失効は既存の取得の失敗で知る |

## 2. 共通の規則

- **評価する場所**: full instance の APScheduler の毎時の job (O3 が足す。readonly は scheduler を持たないので評価しない)。
  時刻は毎時 :20 (安全帯 :16-:29。毎時の収集と重ならない)。日次の窓の警報も毎時に評価し、抑止で 1 日 1 回に絞る
- **評価の時刻 t** は job の開始時刻 (UTC)。窓は `(t − 窓, t]`
- **送り先**: 全警報は health-status の新しい節 `o3_alerts` に「警報 id・鍵・いまの値・発火中か・最後に送った時刻」を常に出す
  (送り先 = health)。発火し、抑止に当たらないときだけ `post_ops_message` で ops へも送る (送り先 = ops。ops_notices に 1 行残る)
- **抑止の実装は DB** (`ops_notices`)。題を `[O3:<警報 id>:<鍵>] <固定文>` にし、同じ接頭辞の行が抑止の間隔の中にあれば送らない。
  既存の TIER-LOAD-ALARM・緊急の受け皿のプロセス内の抑止は、再起動・subprocess で効かない (同じ日に複数回出うる) ので真似ない。
  鍵が変わったとき (例: M1 の違反の集合が変わった) は間隔の中でも送る
- **回復の通知は出さない** (ops を静かに保つ)。回復は health の `o3_alerts` で見る
- **文面**: 固定文 + 数値 + 鍵だけ。例外のメッセージ・パス以外のホスト名・資格情報・本文を入れない (§4)
- **重要度**: H3・C1・C2 は `high` (データ消失に直結)、他は `medium`
- **評価そのものの失敗** は警報にしない。`o3_alert_eval_failed` (例外の型だけ) をログに出し、health の `o3_alerts` に `error` を出す

## 3. 警報の定義

閾値のうち根拠が台帳・コードに無いものは **[決定]** と書き、理由を添えた。

### H. ホストの成果物の鮮度 (再生不可・前向きの観測だけ)

アプリのコンテナは `./data` を `/app/data` で読める (compose の volume) ので、ホストの LaunchAgent ではなくアプリから評価する。

**H1 公開面 (標準) の配信の停止** — 鍵 `export` / `deploy`
- `export`: `data/public_site_dist/data/meta.json` の `generated_at` が t − 3 時間より古い (LaunchAgent は毎時 :40 に組み立て→配信)
- `deploy`: `data/deploy_site_last_ok.json` の `ts_epoch` が t − 3 時間より古い (配信の成功の印。成功と「変更なし」の両方で更新)
- 窓: 3 時間。**[決定]** 毎時の配信を 2 回落としても鳴らさず 3 回目で鳴らす (ロック待ち最大 40 分・wrangler 最大 15 分の遅れを吸う)
- 抑止: 6 時間。送り先: ops + health
- 限界: 成功の印は public と mirror で 1 ファイルを上書きする (`side` が最後の 1 回だけ)。mirror の成功 (3 時間ごと) が public の配信の失敗を最大 3 時間隠しうる。
  wrangler の故障なら両側が落ちるので `deploy` で拾える。**[決定] O3 で `deploy_site.sh` が側ごとの印 (`deploy_site_last_ok.<side>.json`) も書くようにし、`deploy` は public の印で判定する**
- 内容が変わらない配信の検知 (`combined_site_deployed.sha256` の mtime) は作らない: `meta.json` が毎回 `generated_at` を含むので digest は毎回変わり、条件にならない

**H2 アドバンスドの書き出しの停止** — 鍵 `export`
- `data/mirror_dist/data/meta.json` の `generated_at` が t − 7 時間より古い (日本時間 2:50 から 3 時間ごと)
- 窓: 7 時間。**[決定]** 2 回の取りこぼし + 1 時間
- 抑止: 12 時間。送り先: ops + health

**H3 バックアップの欠け** — 鍵 `db_dump` / `db_dump_size` / `eval_assets` / `rotation`
- `db_dump`: `data/backups/kuebiko_*.dump` の最新の mtime が t − 30 時間より古い。sidecar は起動時に 1 回 + 24 時間ごと
  (時刻は再起動でずれる。10-16 は 09:47 UTC、10-31 は 01:23 UTC)。**[決定]** 24 時間 + 6 時間の余裕
- `db_dump_size`: 最新の dump の大きさが 1 つ前の 0.5 倍未満。**[決定]** 直近 3 本は 469→472→477 MB と単調に増えており、半減は壊れた dump の疑い
- `eval_assets`: `data/backups/eval_assets_*.tar.gz` の最新の mtime が t − 30 時間より古い (`daily-maintenance` が日次で書く)
- `rotation`: `kuebiko_*.dump` の数が `BACKUP_RETENTION_DAYS` + 2 (= 16) を超える (`find -mtime +14 -delete` は 15 本を残す。作成時 15 本)
- 窓: 30 時間 (`rotation` は時点の状態)。抑止: 24 時間。送り先: ops + health

### M. モデルの組の整合 (再生可)

**M1 2 枠の終着の外** — 鍵 = 違反の一覧の sha256 の先頭 12 桁
- 条件: `src/tools/model_end_state.check_live_assignments(today=t の日付)` が空でない (health の `model_end_state.violations` と同じ関数。
  `scripts/check_model_end_state.py` も同じ `find_violations` を使う)
- 割当の読み込みが例外なら「判定不能」で鳴らさない (TIER-LOAD-ALARM の領分、§1)
- 窓: 時点の状態。抑止: 24 時間 (違反の集合が変わったら即時)。送り先: ops + health
- 既知の発火: `RESIDENT_STEP_EXCEPTIONS` と s23i の例外の期限は 2026-12-30。O3 のデプロイ (台帳 2027-01-03) の時点で
  `step:*` に s23i / gemma4:26b の上書き (作成時の版 46 で s23i 5 件・gemma4:26b 2 件) が残っていれば、デプロイ直後から鳴る。これは誤警報ではなく期限切れの検知
- 再生: `app_config_versions` (config_key = `model_tiers`) から各 t 以前の最新の版の `value_json` を取り、`step:` / `fallback:` の接頭辞で
  tiers・step_overrides・local_fallbacks に分けて `find_violations(..., today=t の日付)` に通す。registry (`model_registry`) は再生の時点のコードのもの (manifest に HEAD の sha)

**M2 ML の学習時のモデルと割当の不一致** — 鍵 = 組の名前
- 本番の条件 (状態の照合、毎時): 次の組で JSON の宣言と `resolve_step_model(<step>)` が違う

  | 組 | JSON の欄 | 割当 | 作成時 |
  |---|---|---|---|
  | `detect_axes` | `config/models/detect_model.json` の `axes_model` | `Step.SEVERITY_AXES` | 一致 (s24ci65) |
  | `severity_c_probe` | `config/models/severity_s3_model.json` の `probe_model` | `Step.SEVERITY_AXES` | 一致 (s24ci65) |
  | `assign_probe` (`ASSIGN_PROBE_FEATURE=1` のときだけ) | `config/models/assign_model_probe.json` の `probe_model` | `Step.ASSIGN_EVIDENCE_JUDGE` | **宣言なし** (§6) |
  | `probe_thresholds:<step>` | `config/models/probe_thresholds.json` に `[割当のモデル][step]` の項がある | `effective_threshold` の呼び手 3 つ (`thin_body_judge`・`pir_focus_claim_judge`・`org_identity_judge`) | すべて項あり |

  - 欄が無い (宣言なし) 組は ops に出さず、health に `undeclared` として出す。**[決定]** 宣言なしは「照合できない」で、
    不一致の証拠ではない (`assign_model.REQUIRE_PROBE_MODEL_KEY = False` と同じ扱い)。`pair_model.json` の `trained_llm_models` は空 (`[]`) のため組に入れない (同じ理由)
  - 外部モデル (`claudecode:` 等) の割当は対象外
- 窓: 時点の状態。抑止: 24 時間。送り先: ops + health
- 再生 (状態は DB に無いので、本番の経路が残した記録で代える。**下限の数え**: 経路が走らない時間は数えない):
  `run_logs.line` の event が `detect_ml_axes_model_mismatch`・`assign_model_probe_model_mismatch`・`probe_threshold_missing` の行、
  または `severity_c_shadow.model_match = 0` の行が `(t − 24h, t]` に 1 件以上。
  参考: 作成時の DB で `detect_ml_axes_model_mismatch` は 10-09〜10-26 に毎日 1〜4 件 (学習 s21・割当 s23i のまま 18 日間、ログだけで誰も気付かなかった)、
  `severity_c_shadow.model_match = 0` は 10-27 に 80 行 (軸の切替の翌日の学び直しまで)。M2 が拾うべき実例

**M3 緊急の受け皿の使用 (DB 側)** — 鍵 = 受け皿のモデル名
- 条件: `llm_usage` の `provider IN ('ollama','ollama-probe')` かつ `fallback = 1` かつ `model ∈ EMERGENCY_FALLBACK_MODELS` (`gemma4:26b`) の行が `(t − 1h, t]` に 1 件以上
- 既存の通知との関係: 同じ 24 時間に `ops_notices.title = '緊急の受け皿が使われました'` の行があれば送らない (既存が届いている)。
  M3 が拾うのは既存が黙る経路 (同期の文脈・subprocess で通知が仕込めなかった回)
- 窓: 1 時間。抑止: 24 時間。送り先: ops + health
- 再生: 同じ SQL を各 t に当てる。`llm_usage` のローカルの行は 2026-10-29 20:30 UTC から (保持 90 日)

### R. 記録の失敗

**R1 記録の失敗 (subprocess)** — 鍵 = `component`
- 条件: `run_logs.line` の event が `ops_write_failed` の行が `(t − 1h, t]` に 1 件以上 (`record_failure` のログ。subprocess の stdout は `run_logs` に入る)
- 窓: 1 時間。抑止: 24 時間。送り先: ops + health
- 再生: 可 (`run_logs` の保持 180 日)。作成時の DB で `ops_write_failed` は 0 件

**R2 記録の失敗 (API プロセス)** — 鍵 = `component`
- 条件: `write_failures.snapshot()[component]` が前回の評価より増えた (前回より小さければ再起動とみなし基準を取り直す。基準はプロセス内)
- 窓: 前回の評価から。抑止: 24 時間。送り先: ops + health
- 再生: **不可** (プロセス内の計数で、API プロセスのログは DB に入らない)。前向きの観測だけ

**R3 記録の沈黙** — 鍵 `llm_usage`
- 条件: 次を満たす 1 時間の区切り (UTC の正時) が**連続 2 つ**: `job_run_log` に `job_id IN ('hourly-collect','severity-axes-hourly')` の `status = 'succeeded'` が 1 件以上あり、
  `llm_usage` の `provider IN ('ollama','ollama-probe')` の行が 0 件。評価の時点で `LLM_USAGE_LOCAL=0` なら評価しない
- 意図: 書き込みの例外が出ずに記録が止まる型 (旗の誤設定・バッファの捨て) を拾う。ホストの sleep の時間はジョブも動かないので鳴らない
- 窓: 2 時間。**[決定]** 1 時間だと正時をまたぐ run で偽の 0 が出うる。作成時 (10-29 21:00〜10-31 10:00 UTC) に 1 時間で 20 行未満は 1 区切りだけで、それはジョブも 0 の区切り
- 抑止: 24 時間。送り先: ops + health
- 再生: 可 (`job_run_log` 180 日・`llm_usage` 90 日)
- 他の観測用の表 (`geo_rescue_shadow`・`severity_c_shadow` 等) への拡張は作らない (YAGNI。必要になったら本書を改訂)

### C. 容量 (再生不可・前向きの観測だけ)

**C1 ホストのディスクの空き** — 鍵 `host`
- 条件: `shutil.disk_usage('/app/data').free < 50 GiB` (台帳 `ENV-DISK` の 50 GiB)。`/app/data` はホストの Data volume を見る (作成時 289 GiB 空き。Ollama の models 458 GB も同じ volume)
- 窓: 時点の状態。抑止: 24 時間。送り先: ops + health

**C2 VM ディスク (PG の volume) の空き** — 鍵 `vm`
- 条件: `shutil.disk_usage('/')` (kuebiko コンテナの rootfs。PG の named volume と同じ OrbStack の VM ディスク。作成時 329 GB 中 289 GB 空き) の空きが全体の 15% 未満
- **[決定]** 15%: 根拠の実測は無い。VM ディスクの上限 (作成時 329 GB) に達すると PG が書けなくなるので、DB (1.4 GB、日次 dump 約 0.5 GB) の増え方に対して数か月の余裕がある線にした。
  O3 で kuebiko と postgres の rootfs が同じデバイスかを確かめる (違えば postgres の `/var/lib/postgresql/data` を別途見る)
- 窓: 時点の状態。抑止: 24 時間。送り先: ops + health

**C3 保持の purge の停止** — 鍵 = 表の名前
- 条件: O3 の `refs.retention` の表ごとに、最古の行の時刻が t − (保持日数 + 3 日) より古い。対象と保持日数は定数から読む
  (`dedup_seen_urls` 90・`access_audit` 180・`ops_notices` 180・`detect_bundles` 180・`triage_rejections` 180・`event_item_focus_log` 180 (`changed_at`))。backup の dump は H3 の `rotation`
- **[決定]** +3 日: `daily-maintenance` が 2 回落ちても鳴らさない
- 窓: 日次 (毎時に評価し抑止で 1 日 1 回)。抑止: 24 時間。送り先: ops + health
- 再生不可の理由: purge で消えた行は過去の時点の最古の行を残さない (今ある行から過去を数えると過小になる)

## 4. まとめの表

| id | 対象 | 条件 (要約) | 窓 | 抑止 | 送り先 | 再生 |
|---|---|---|---|---|---|---|
| H1 | 鮮度 | 公開面の `meta.json` の `generated_at` または配信の成功の印が 3h 超 | 3h | 6h | ops + health | 不可 |
| H2 | 鮮度 | アドバンスドの `meta.json` の `generated_at` が 7h 超 | 7h | 12h | ops + health | 不可 |
| H3 | 鮮度 | 最新の dump / 評価資産が 30h 超・dump が前回の半分未満・dump が 16 本超 | 30h | 24h | ops + health (high) | 不可 |
| M1 | モデルの組 | `check_live_assignments()` が空でない | 時点 | 24h (集合が変われば即時) | ops + health | 可 (`app_config_versions`) |
| M2 | モデルの組 | ML の JSON の学習時のモデル ≠ 割当、しきい値表に項なし | 時点 | 24h | ops + health | 可 (`run_logs`・`severity_c_shadow`、下限) |
| M3 | モデルの組 | `llm_usage` に緊急の受け皿の fallback 行 (既存の通知が同じ 24h に無いとき) | 1h | 24h | ops + health | 可 (`llm_usage`) |
| R1 | 記録の失敗 | `run_logs` に `ops_write_failed` | 1h | 24h | ops + health | 可 (`run_logs`) |
| R2 | 記録の失敗 | `write_failures` の計数が増えた (API プロセス) | 前回から | 24h | ops + health | 不可 |
| R3 | 記録の失敗 | ジョブが成功したのに `llm_usage` が 0 行の正時が 2 連続 | 2h | 24h | ops + health | 可 (`job_run_log`・`llm_usage`) |
| C1 | 容量 | ホストの空き < 50 GiB | 時点 | 24h | ops + health (high) | 不可 |
| C2 | 容量 | VM ディスクの空き < 15% | 時点 | 24h | ops + health (high) | 不可 |
| C3 | 容量 | 保持の表の最古の行が 保持 + 3 日 超 | 日次 | 24h | ops + health | 不可 |

## 5. O3-ALERT-REPLAY の事前登録 (M1・M2・M3・R1・R3 だけ)

H1〜H3・C1〜C3・R2 は再生しない (§0)。O3 のデプロイの後 14 日の前向きの観測で、同じ §5.4 の線を当てて報告する (判定ではなく報告)。

1. **仮説**: 本書の規則を過去 14 日に当てても、新しい警報の ops の通知は運用者が読める量に収まり、発火はすべて DB の中に原因がある
2. **母集団**: 窓の中の毎時 :20 の評価の時刻 t の全部 (14 × 24 = 336 回)。ホストが sleep していた時刻 (その正時に `job_run_log` が 0 行) は数えて報告し、評価からは外さない (規則がそのまま当たる)
3. **期間**: O3 のデプロイの直前の 14 日 (UTC 00:00 で切る)。台帳の日付 (O3-ALERT-REPLAY 2026-12-30・O3 2027-01-03) なら 2026-12-16 00:00Z 〜 2026-12-30 00:00Z。
   デプロイまで 14 日を超えて空いたら、デプロイの前の 14 日で数え直す (台帳の注記)。時刻はシステムの事象の記録時刻 (`ts`・`created_at`・`ran_at`) そのもの。
   記事の事象時刻 (`EVENT_TS_EXPR`) は使わない (記事を数えないので README の分割は当たらない)
4. **数え**: 警報・鍵ごとに (a) 条件が真の評価の回数、(b) 抑止 (§2、再生の中で `ops_notices` の代わりに再生の送信の記録で判定) の後の通知の数、
   (c) 通知ごとの原因 (版の番号・ログの行・表の行)。調整役が原因を読み、対処の要らない通知を「誤警報」とする (理由を 1 行ずつ残す)。
   M1 の期限切れ (§3 M1) は対処が要るので誤警報に数えない。標本・重み・対の検定は無い (全数の数え)
5. **合否の線** (結果を見る前に固定):
   - 新しい警報の通知の合計 ≤ 14 件 / 14 日 **[決定]** 作成時の ops の通知は 14 日で 58 件。新しい警報で 1 日 1 件 (+24%) を上限にした
   - 各警報の通知 ≤ 7 件 / 14 日
   - 各警報の誤警報 ≤ 2 件
   - 不合格の警報は O3 に入れない (その警報だけ外す) か、本書を改訂して新しい 14 日で数え直す (§7 の規則)
6. **汚染の鍵 (R18)・判定集**: 該当なし (学習・正解集を使わない)
7. **manifest**: README のとおり (`prereg_path`・`prereg_git_sha`・`prereg_blob_sha256`・HEAD の sha)。加えて再生の時点の `model_registry` の `RESIDENT_SLOTS`・例外の期限

## 6. 台帳・文書とコードの食い違い (作成時に見つけたもの)

- **O3 のデプロイの日付が台帳の中で 3 通り**: `O3.date` = 2027-01-03、`O3-ALERT-REPLAY.notes` = 「O3 のデプロイ (2027-01-11)」、
  別の項目の notes (行 5815 付近) = 「O3・O4 と同じ 11-15 の観測のデプロイ」(O4 の `date` は 2026-11-30)。§5.3 は `O3.date` で書いた
- **O3-ALERT-REPLAY の lead_days**: 欄は `lead_days: 55`、notes は「O3-ALERT-DEF の後 57 日 (lead_days) = 12-28 前後」、`date` は 2026-12-30
- **再生不可は 2 つではなく 3 つ**: 台帳は鮮度と容量だけを再生不可とするが、R2 (API プロセスの `write_failures`) も DB に履歴が無い。
  逆に、ホストのファイルである ML の JSON の組 (M2) は `run_logs`・`severity_c_shadow` の記録で (下限として) 再生できる
- **`write_failures` を使う呼び手は 1 つだけ**: `record_failure` を呼ぶのは `local_llm_usage` (`src/tools/local_usage.py`・`src/tools/llm_client.py`) だけ。
  docstring の「detect の確率・生成物の入力 等」の表には呼び手が無い。R1・R2 の被覆は現状 `llm_usage` に限られる
- **緊急の受け皿の ops 通知** (CLAUDE.md §2 の手順 5「ログ + 計数 + ops 通知 (1 日 1 回)」): 同期の文脈 (イベントループ無し) では ops に出ない
  (`_notify_ops_once_a_day` が早く return)。1 日 1 回もプロセスごとで、subprocess ごとに数え直す
- **`assign_model_probe.json` に `probe_model` が無い**: CLAUDE.md §7 は `ASSIGN_PROBE_FEATURE` を on とするが、照合 (`_probe_model_matches`) は宣言が無いので常に通り、
  `assign_model_probe_model_undeclared` を毎回ログに出す (作成時 2 日で 6 run・14 行)。M2 はこの組を照合できない
- **`pair_model.json` の `trained_llm_models` が空**: CLAUDE.md §2 (版 45) は「09 月版の pair_model の入力の LLM が変わった」と書くが、JSON はどの LLM で学習したかを持たないので照合できない

## 7. 変更履歴

- 2026-10-31 下書き (O3 のコード・再生の出力は無い)
- 変更の規則は README の「線を変えてよいとき」に従う。O3 の実装のレビューで条件・閾値を変えるときは、ここに日付・項目・前後・理由を書いてから

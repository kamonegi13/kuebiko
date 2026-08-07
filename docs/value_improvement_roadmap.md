# Value Improvement Roadmap (2026-06-07)

このツールの価値を 5 方向ミッション (状況認識 / 将来予測 / 過去参照 / 学習記憶 / 発見支援)
全方向で向上させるための段階的ロードマップ。4 サブシステムの網羅監査 (intelligence
products / sources+enrichment / UI+workflow / knowledge+reliability) に基づく。

## 監査サマリ (現状評価)

| ミッション | 評価 | 主な所見 |
|---|---|---|
| 状況認識 | A− | synthesis/PMESII/Diamond は成熟。ただし synthesis/spotlight は UI のみで Discord 未配信 |
| 発見支援 | B− | actor→incident pivot は強い。逆引き(IOC→actor)無し、検索が substring のみ |
| 過去参照 | C | 検索が title+summary のみ、窓が週単位、actor 想起が regex 依存 |
| 学習記憶 | C | **Adversary 未永続化** (毎回 regex 再走査)、embedding は dedup 専用で死蔵 |
| 将来予測 | F | 実質ゼロ (outlook は prose のみ、履歴・指標追跡なし) |
| 信頼性(横断) | C− | **DB バックアップ自動化が皆無** (過去に corruption 全損事故) |

### 構造的欠落 + 重大バグ
1. 記憶の欠落: Adversary が DB 非永続。PIR evaluator に actor entity を引く dead query。
2. 予測の欠落: 履歴が reasoning loop に入っていない。
3. PIR が飾り routing: R0 は dead code (`matched_pir_ids` 未 populate、全 PIR `target_channel: auto`)。
4. 存続リスク: pg_dump 自動化ゼロ。

## フェーズ構成 (依存順)

各 Phase は単体で価値が出て、次を解放する。手戻り回避のため順序を守る。

| Phase | 狙い | 含む項目 |
|---|---|---|
| **0. 基盤・存続** | 出血を止め礎石を置く | F1 backup / **F2 actor 永続化** / F3 purge 安全化 / Q5 観測可能化 |
| 1. 正しさ・データ品質 | 下流が依存する事実を正す | Q1 PIR routing 結線 / Q2 MITRE 検証 / Q3 actor matching / Q4 export / K5 KEV/NVD |
| 2. 記憶を使える形に | コーパス→使える記憶 | K1 セマンティック検索 / K2 逆引き / K3 全文 index / K4 記事 deep-view / K6 synthesis 配信 |
| 3. 収集の深掘り | breadth (Phase2 と並行可) | 中国/北朝鮮 一次ソース / thin feed の full-text triage |
| 4. 将来予測 | 唯一欠けたミッション | FC1 履歴注入 / FC2 指標説明責任 / FC3 分散考慮 spike / FC4 actor トレンド / FC5 相関 timeline |

依存連鎖: `F2 → K2/FC4/Q1`、`(F2+K1) → Phase4`。F1 は独立・即時。

---

## Phase 0 詳細設計 (実装中)

### F1: PostgreSQL バックアップ自動化 (存続リスク除去)
- **方式**: `postgres:16-alpine` を使う **decoupled な compose sidecar** `backup`
  (pg_dump 内蔵、app 障害時も動く = backup の本旨に合致、app の image/コード無改修)。
- **動作**: 起動時に即時 1 回 + 日次。`pg_dump -Fc` (custom format) を
  `./data/backups/kuebiko_<ts>.dump` に出力。`BACKUP_RETENTION_DAYS` (既定 14) で
  古い dump をローテーション削除。stdout にログ (docker logs backup)。
- **復元**: `scripts/restore_db.sh <dump>` (確認プロンプト + `pg_restore --clean --if-exists`)。
- **受入基準**: `docker compose up -d backup` 後、`data/backups/` に dump が生成され、
  `pg_restore --list` で中身が読め、retention で古い dump が消える。

### F2: Adversary entity 永続化 (学習記憶の礎石) ★最重要
- **現状**: `_persist_article_entities` は capability/infra のみ永続化。actor は毎回
  `threat_operations` が title/summary を regex 再走査。`pir/evaluator.py` の
  `entity_type='actor'` query は誰も書かないので常に空 fallback (dead path)。
- **変更**: `_persist_article_entities` に、metadata の `detected_actor_ids`
  (= `actor_registry.find_all(body)` の canonical id) を `("actor", id)` として追加。
- **backfill**: `scripts/backfill_article_entities.py` を actor 対応に拡張
  (既存 body から find_all で actor 再検出)。
- **効果**: PIR evaluator の actor query が活性化、actor retrospection が DB 事実化、
  Phase4 (actor トレンド/予測) と Phase1-Q1 (PIR routing) の前提を解放。
- **受入基準**: 新規 post 後 `article_entities` に `entity_type='actor'` 行が入る。
  PIR evaluator が actor entity で match する。回帰なし。

### F3: purge の非破壊化 (過去参照コーパス保護)
- **現状**: `/history/purge` → `delete_runs_older_than` が `runs` を削除し、
  `articles` を `ON DELETE CASCADE` で**連鎖削除** (既定 30 日)。1 クリックで数年の
  記憶が消える。`article_entities` (FK 無し) は orphan 化。
- **変更**: purge endpoint を `purge_old_logs(days)` (run_logs のみ) に切替。
  runs/articles は保持 (両 backend で無 migration)。UI ラベルを「古いライブログを削除」に。
  起動時に orphan `article_entities` を cleanup (既存 `cleanup_article_entities` ロジック)。
- **受入基準**: purge 実行で articles 件数が変わらない。run_logs のみ減る。

### Q5: 観測可能化 (silent failure 低減)
- embedder=None で semantic dedup が黙って skip される件 →
  `_try_build_embedder` の `embedding_disabled` を INFO→WARNING に格上げ
  (2/4 dedup 層が off であることを可視化)。
- `content_dedup.py` の docstring drift 修正 (記述 0.5 / 実値 0.4 → 0.4 に統一)。
- **受入基準**: embedder 未設定時に WARNING ログが出る。docstring と定数が一致。

---

## Phase 1 進捗 (2026-06-07)

- **Q1 (PIR routing 結線)** ✅: `_compute_matched_pir_ids` で matched_pir_ids を populate。
  `is_pir_driven_routing_enabled()` が「explicit channel を持つ PIR の有無」で gate するため、
  全 PIR が auto の現状は評価コストゼロ・behavior-preserving。explicit channel 設定で自動活性化。
- **Q3 (actor matching)** ✅: substring → ASCII 英数境界の lookaround に変更。"APT1"⊂"APT10"
  誤帰属を排除しつつ、日本語隣接 ("Lazarusが") は保持。F2 の actor 永続化精度も向上。
- **Q4 (STIX download)** ✅: `/api/v1/stix/files/{name}` (path traversal 防止) + StixPage に
  download リンク。bundle が disk に stranded していた問題を解消。CSV export は Phase 2 (UI) へ。
- **Q2 (MITRE 検証)** ✅: `is_plausible_technique_id` (format + ATT&CK 現実的レンジ [800,1699]) で
  merge_techniques 時に egregious hallucination (T9999/T0001/桁数違い) を drop。G####/他種別は保持。
  実 ATT&CK 全集合の bundle は鮮度保守の負担が大きいため自己完結レンジ検証を採用 (in-range typo は
  将来 real-set 化の余地)。
- **K5 (CISA KEV authority)** ✅: `kev_client` が KEV catalog を 24h cache (hot-path は cache 読むだけ、
  fetch は startup/pipeline 開始時に TTL gated)。routing_signals の `has_kev_or_active_exploit` を
  prose regex 推定から CISA 公式照合の deterministic 判定に。NVD は別途 (Phase 2 K5b)。

→ **Phase 1 完了** (Q1/Q2/Q3/Q4/K5)。CSV export と NVD authority は Phase 2 に送り。

## Phase 2 進捗 (2026-06-07)

- **K2 (逆引き Pivot)** ✅: `GET /api/v1/pivot?entity_type=&value=` が既存 `list_articles`(entity filter)
  + `count_entities_for_articles`(共起集計) を組合せ、1 entity の参照記事 + 共起 entity (特に actor) を返す。
  UI は新規 `逆引き Pivot` ページ (nav: インテリジェンス)。related チップで連鎖 pivot (IOC→actor 帰属 /
  infra 相関)。F2 の actor 永続化が actor 共起の基盤。
- **K3 (全文検索)** ✅: `list_articles` の search を title+summary から **body 全文** まで拡張。
  過去参照の取りこぼしを解消 (他フィルタで絞った後の LIKE なので個人規模で実用上十分速い)。
- **K1 (セマンティック検索)** ⏳: 死蔵 embedding を read API 化 (top-K cosine + url_hash→article 解決)。次。
- **K4 (記事 deep-view + Discord deep-link)** ⏳: 次。
- **K6 (synthesis/spotlight Discord 配信)** ⏳: 次。

## Phase 1-4 設計 (Phase 到達時に詳細化)

各 Phase の項目は監査レポートに file:line 付きで根拠あり。実装着手時に本ファイルへ
詳細設計 + 受入基準を追記する。主要な設計上の決定:
- **Q1 (PIR routing)**: `routing_signals` の 2 つの `extract_signals_*` で
  `evaluate_pir_for_article` を呼んで `matched_pir_ids` を populate → R0 活性化。
  併せて ≥1 PIR に explicit `target_channel` 設定。
- **K1 (セマンティック検索)**: 既存 embedding pipeline + `find_similar_embedding` を
  再利用し read API 化 (新インフラ不要)。
- **K5 (KEV/NVD)**: KEV JSON catalog + NVD API を構造化取得し `on_kev`/CVSS を
  deterministic signal 化 (現状は prose からの regex 推定)。
- **FC2 (指標説明責任)**: outlook の「来週警戒すべき指標」を構造化保存 → 翌週に的中検証
  (軽量 MVP forecast primitive)。

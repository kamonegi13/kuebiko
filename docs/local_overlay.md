# 運用者ローカルの上書き層 (`config/local/`)

2026-10-30 導入、同日に方針改訂 (§1)。一覧の SSoT は追跡された公開版で、`config/local/` は
gitignore した**任意の追加層** (`CLAUDE.local.md` / `prompts/_persona_local.j2` と同じ型)。
**ファイルがあれば和集合で重ね、無ければ公開版だけで動く**。

## 1. 方針 (2026-10-30 利用者判断で改訂)

- **敵対国の政府・国営媒体・軍の発信元を収集するのは周知の CTI 実務**。その登録ドメインの一覧
  (`config/fetch/hostile_domains.yaml` の `domains`、`config/sources/source_reliability.yaml` の
  `adversary_official`) は**公開してよい。SSoT は追跡された公開版**。
- **公開しない (ローカルのみ)**: 運用者の帰属・識別につながるもの (`CLAUDE.local.md`・運用者ペルソナ
  `prompts/_persona_local.j2`・実運用ドメイン。コード/docs 上は `kuebiko.example` に置換)、資格情報 (`.env`)、
  実インシデントの詳細 (被害組織名等)。
- 本書の `config/local/` は、運用者が**私的な追加**をしたいときの任意の層 (和集合)。無くて通常。
  最初の導入時 (同日) はこの一覧自体をローカルへ移していたが、上の判断で公開版へ戻した。
- 購読ソースそのものは DB (config_store の `feeds` / `watchers` / `scrapers`) が runtime の SSoT で、
  `config/sources/*.yaml` は初回の seed 専用 (`src/sources/source_store.py`)。

## 2. ローカル層のファイル (任意)

置き場所は `<repo>/config/local/` (`.gitignore` の `/config/local/`、`.dockerignore` でも除外)。
コンテナには `./config:/app/config` の bind mount でそのまま渡る (readonly instance は `:ro`)。

| ファイル | 中身 | 重なる公開版 (SSoT) | 読み手 |
|---|---|---|---|
| `hostile_domains.yaml` | 私的に足す敵対的 host (任意で ccTLD) | `config/fetch/hostile_domains.yaml` | `src/tools/hostile_hosts.py` |
| `source_reliability.yaml` | 信頼度ティアの私的な追加分 | `config/sources/source_reliability.yaml` | `src/cti/source_basis.py` |
| `source_reliability_overrides.yaml` | **初回 seed 元のみ** (§6)。per-source 上書きの runtime SSoT は DB | 旧 `config/sources/source_reliability_overrides.yaml` (非推奨・seed 専用) | `src/cti/source_basis.py` |

- 形式は公開版と同じ。**和集合**で重なる。公開版と同じ項目をローカルにも書いても重複は無害 (no-op)。
- 読み込みはプロセス内でキャッシュ。編集したら再起動で反映 (overrides は DB で、UI の保存で即時)。
- 起動後のログ (件数のみ、ドメイン名は出さない): `hostile_domains_loaded` / `source_reliability_loaded`
  (`source=generic` または `generic+local`)。**ローカルが無いのは正常**なので常に info。
  壊れているときは `hostile_domains_local_invalid` / `source_reliability_config_load_failed` (layer=local) を出し、
  公開版だけで動く。
- 置き場所は環境変数 `KUEBIKO_LOCAL_CONFIG_DIR` で差し替えられる (主にテスト用。`tests/conftest.py` が
  存在しない一時ディレクトリへ向け、開発機の実ファイルに左右されないようにしている)。

## 3. 公開版に書いてよいもの / 書かないもの

- 書いてよい: ccTLD の規則、敵対国の政府・国営媒体・軍の登録ドメイン、`state_media` 等の host / title。
- 書かない: 運用者の帰属・識別を示す語、実運用のドメイン、資格情報、実インシデントの詳細。
  テスト・コメント・コミットメッセージ・docs の fixture には `*.kuebiko.example` か example のサブドメインを使う。

関門: `tests/unit/test_local_overlay_guard.py` は公開版の一覧が空でなく host の形であること、
`.gitignore` / `.dockerignore` の行、`config/local/` が git に追跡されていないことを確かめる。
push 前の確認は `scripts/check_unpushed_private_terms.sh` (未 push の追加行とコミットメッセージに、
gitignore 済みの `config/local/push_guard_terms.txt` の語が無いか。出力は件数のみ)。

## 4. バックアップと復元

ローカル層は git にも DB にも入らない (日次の `pg_dump` の対象外)。**運用者が自分で退避する**。

- 退避: `config/local/` をディレクトリごと運用者のバックアップ先 (リポジトリの外) へ写す。
  例: `tar czf <backup先>/kuebiko-config-local-$(date +%Y%m%d).tgz -C <repo>/config local`
- 復元: `mkdir -p <repo>/config/local && tar xzf <backup>.tgz -C <repo>/config` の後、
  app service を再作成して (CLAUDE.md §7 のデプロイ規則どおり)、ログの
  `hostile_domains_loaded source=generic+local` と件数が退避前と同じかを確かめる。
- 新しい機械に置くときも同じ。ファイルが無い間は汎用の規則で動く (止まらない)。
- `git status` に `config/local/` が出ないこと (ignore 済み) を確かめてからコミットする。

## 5. ログに出るもの (ドメイン名の扱い)

「ドメイン名を出さない」は**ローダーのログ** (`hostile_domains_loaded` / `source_reliability_loaded` /
`*_local_invalid`) だけの約束で、中身の一覧は出さず件数だけ。**取得ごとのログ**
(`hostile_host_policy`、`ua_health_hostile_canary`) は判定した host 名を含む。これは運用者ローカルの
運用ログで、`run_logs` (DB) と `docker logs`・日次 `pg_dump` に残るが、公開面には出ない:

- 標準面: `scripts/export_public_site.py` は事象ニュースを書き出すだけで、実行ログを持たない
  (`_FORBIDDEN_KEYS` は body 系の検査)。
- アドバンスド (`/app`): `scripts/export_mirror.py` の `_final_gate` は資格情報・本文・notes の項目名を
  弾き、`/api/v1/subscriptions` 等を書き出さない。`dashboard/summary` の `recent_runs` / `next_run_at` は
  書き出しから外している。実行ログ (`/api/v1/runs/*`) の書き出しは無い。
- 公開 instance: `src/ui/read_only_policy.py` の匿名 GET は `PUBLIC_GET_ALLOWLIST` (default-deny、
  `/api/health`・`runtime-flags`・`vocabularies`・`public/news` のみ)。実行ログは認証済み (Tier1) のみ。
  なお旧名 `READ_ONLY_GET_DENYLIST` は 2026-08-25 に allowlist 方式へ反転済みで、コメント中に残る名前は旧称。

per-source 信頼度上書き (キー = 購読先 URL) は DB (config_store) にあり、公開リポにも出ない。

## 6. 信頼度の上書きは DB (2026-10-30)

購読先 URL が key のため、per-source 上書きの runtime SSoT は **DB** (config_store、
key=`source_reliability_overrides`)。版履歴が残り、設定の履歴画面 / `POST /api/v1/config-history/
source_reliability_overrides/revert` で戻せる (`config_history._KNOWN_KEYS` に登録済み)。

- **seed**: DB に key が無いときの最初の読みで、`config/local/source_reliability_overrides.yaml`
  (無ければ旧追跡の `config/sources/source_reliability_overrides.yaml`) を version 1 として取り込む
  (`source_store.seed_all_if_absent` と同型。読み取り専用 instance で書けなければ保存せず seed の値で動く)。
  以後 DB が SSoT で、ファイルを変えても反映されない。UI (`POST /subscriptions/reliability`) の保存は
  DB にだけ書く。この POST は `REMOTE_WRITE_ALLOWLIST` に無く、遠隔 (Tier1) からは書けない (従来どおり)。
- **rollback**: `SOURCE_RELIABILITY_OVERRIDES_DB=0` でファイル (ローカル層) の直読み/直書きに戻る
  (`SOURCES_CONFIG_DB` の最小版)。
- **旧追跡ファイル**: `config/sources/source_reliability_overrides.yaml` は**非推奨・seed 専用**として
  残す。新コードは DB を読み、旧コードは再ビルドまでこのファイルを読むため、マージで消えると
  デプロイの途中で上書きが空になる。**後続の別コミットで削除する** (DB への seed 済みを本番で確かめた後。
  同時に `file_catalog` の項目と `test_file_catalog` の件数 35 → 34 を更新)。

デプロイ手順 (app service 限定、CLAUDE.md §7 の安全帯で):
1. マージ後 `docker compose up -d --build kuebiko readonly`。
2. 初回の読みで seed される (full instance)。`GET /api/v1/config-history` で
   `source_reliability_overrides` の version 1 を確かめる。
3. ローカル層のファイル `config/local/source_reliability_overrides.yaml` は seed 後は不要
   (退避だけ残して消してよい)。

## 7. 関連

- 取得ポリシー: [fetch_escalation_policy.md](fetch_escalation_policy.md) §6
- 運用者ローカルの他のファイル: `CLAUDE.local.md`、`prompts/_persona_local.j2`、`.env`

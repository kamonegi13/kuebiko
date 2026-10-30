# 運用者ローカルの上書き層 (`config/local/`)

2026-10-30 導入。公開リポ (MIT) には**汎用の規則と形式の例だけ**を置き、運用者が実際に
何を集め・何をどう分類しているかが読める一覧は、gitignore したローカルのファイルに置く。
`CLAUDE.local.md` / `prompts/_persona_local.j2` と同じ考え方で、**ファイルがあれば読み、
無ければ汎用の規則だけで動く**。

## 1. なぜ分けるか

- 収集先 (購読ソース) と「敵対的と扱う取得先」の一覧は、それ自体が運用の関心を示す。
  公開リポに置くと誰でも読める。
- 購読ソースそのものは DB (config_store の `feeds` / `watchers` / `scrapers`) が runtime の
  SSoT で、UI / sources API で足した購読は git に入らない (`src/sources/source_store.py`)。
  `config/sources/*.yaml` は**初回の seed 専用**。新しい購読を seed の yaml に足さない。
- 残っていた「git に入る一覧」が本書の対象。

## 2. ローカル層のファイル

置き場所は `<repo>/config/local/` (`.gitignore` の `/config/local/`、`.dockerignore` でも除外)。
コンテナには `./config:/app/config` の bind mount でそのまま渡る (image には焼かない。
readonly instance は `:ro` で同じものを読む)。

| ファイル | 中身 | 重なる公開版 | 読み手 |
|---|---|---|---|
| `hostile_domains.yaml` | 敵対的と扱う登録ドメイン (任意で ccTLD の追加) | `config/fetch/hostile_domains.yaml` (ccTLD の規則と形式の例) | `src/tools/hostile_hosts.py` |
| `source_reliability.yaml` | 信頼度ティアの追加分 (例: `adversary_official` の host) | `config/sources/source_reliability.yaml` | `src/cti/source_basis.py` |
| `source_reliability_overrides.yaml` | **初回 seed 元のみ** (§6)。per-source の信頼度上書きの runtime SSoT は DB | 旧 `config/sources/source_reliability_overrides.yaml` (非推奨・seed 専用) | `src/cti/source_basis.py` |

- 形式は公開版と同じ。**和集合**で重なる (ローカルで公開版の規則を消すことはしない)。
- 読み込みはプロセス内でキャッシュされる。編集したら再起動で反映 (overrides は DB で、UI の保存で即時)。
- どちらの層で動いているかは起動後のログで分かる。**件数だけで、ドメイン名は出さない**:
  - `hostile_domains_loaded` … `source=generic` / `generic+local`、`cctlds` / `domains` の件数
  - `source_reliability_loaded` … `source=generic` / `generic+local`、`local_tiers` の件数
  - ローカルが壊れているときは `hostile_domains_local_invalid` / `source_reliability_config_load_failed`
    (layer=local) を出し、汎用の規則だけで動く
- **本番 (テスト以外) でローカル層が無いと、上の 2 つのログは warning** (`local_missing=true`)。
  置き忘れ・復元漏れに気付くため。テストでは info。
- ローカル層が無いと、敵対的 host の判定は ccTLD (と、ソースの `fetch_profile: hostile` の印) だけになり、
  `adversary_official` は空になる。動作は止まらないが、**取得ポリシーの対象が狭くなる**ので
  本番ではログの `source=generic+local` を確かめる。
- 置き場所は環境変数 `KUEBIKO_LOCAL_CONFIG_DIR` で差し替えられる (主にテスト用。
  `tests/conftest.py` が存在しない一時ディレクトリへ向け、開発機の実ファイルに左右されないようにしている)。

## 3. 公開版に書いてよいもの

- ccTLD の規則 (国の単位。個別の取得先を示さない)
- RFC 2606 の予約名 (`*.example` / `*.test` / `*.invalid`) の placeholder。例: `state-media.kuebiko.example`
- 既に公開済みの分 (`state_media` の既存の host / title 等) はそのまま残す — 消しても公開の履歴からは
  消えず、公開版の利用者の分類だけが変わるため。**2026-10-30 以降の追加はローカル層へ**。

関門: `tests/unit/test_local_overlay_guard.py` が、公開版の `hostile_domains.yaml` の `domains` と
`source_reliability.yaml` の `adversary_official` に placeholder 以外が入ったら落とす。
`.gitignore` / `.dockerignore` の行と、`config/local/` が git に追跡されていないことも確かめる。
テスト・コメント・コミットメッセージ・docs にも実在の取得先を書かない (fixture は `*.kuebiko.example` か
`news.example.ru` のような example のサブドメイン)。

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

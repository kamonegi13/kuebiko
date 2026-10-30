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
| `source_reliability_overrides.yaml` | per-source の信頼度上書き (UI の購読ソース画面が書く。key は feed URL) | なし (旧 `config/sources/` の追跡ファイルから移した) | `src/cti/source_basis.py` |

- 形式は公開版と同じ。**和集合**で重なる (ローカルで公開版の規則を消すことはしない)。
- 読み込みはプロセス内でキャッシュされる。編集したら再起動で反映 (overrides は UI の保存で即時)。
- どちらの層で動いているかは起動後のログで分かる。**件数だけで、ドメイン名は出さない**:
  - `hostile_domains_loaded` … `source=generic` / `generic+local`、`cctlds` / `domains` の件数
  - `source_reliability_loaded` … `source=generic` / `generic+local`、`local_tiers` の件数
  - ローカルが壊れているときは `hostile_domains_local_invalid` / `source_reliability_config_load_failed`
    (layer=local) を出し、汎用の規則だけで動く
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

## 5. 関連

- 取得ポリシー: [fetch_escalation_policy.md](fetch_escalation_policy.md) §6
- 運用者ローカルの他のファイル: `CLAUDE.local.md`、`prompts/_persona_local.j2`、`.env`

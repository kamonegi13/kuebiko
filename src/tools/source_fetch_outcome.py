"""ソース取得の死活観測モデル (transport 横断の SSoT、2026-08-02)。

**なぜ必要か**: 「記事が出てこない」は 2 つの全く違う事象の重ね合わせである —
①ソースが何も発信していない (正常) と ②取得が壊れて発信を観測できていない (異常)。
``articles`` (成果) を数えるだけでは両者を区別できない。区別できる唯一の方法は
**「取得という行為が成立したか」を成果とは別に記録する**ことで、それがこのモデル。

RSS 経路には 2026-07-05 (監査 P2) で ``source_fetch_health`` が入ったが、
html_scraper / sitemap watcher には無く、沈黙が構造的に不可視だった (実測 2026-08-02:
Claroty Team82 / Team Cymru / NCSC が 6 週間ゼロでも「統計データなし」としか出ず、
手で走らせるまで健全か故障か判別できなかった)。ここを埋めて transport 横断で
「壊れた層を名指しする」健全性表示 (docs/fetch_escalation_policy.md §4) を完成させる。

**「行為の成立」の境界は transport ごとに違う** — これが設計の核心:

- RSS: feedparser が汎用パーサなので、**取得と parse の成功**が行為の成立。
  entry 0 件は「ソースが 0 件提示した」という観測であり、失敗ではない。
- html_scraper / sitemap: **自前の selector / include_pattern の適用**まで含めて
  初めて「ソースが何を提示したか」を観測できる。サイト改修で selector が腐ると
  抽出 0 件になるが、listing ページ自体は 200 を返し続けるため、取得成功だけを
  見ていると**無音で死ぬ**。よって抽出 0 件は ok=False として扱う
  (listing/sitemap が常に 0 件を提示することは実務上ありえない)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceFetchOutcome:
    """1 ソースの 1 回の取得結果 (死活記録の書き込み単位)。

    ``source_key`` は購読一覧の ``url`` と一致させる — 死活と購読の結合キーであり、
    RSS は feed URL、html_scraper は listing URL、sitemap は先頭の sitemap URL。
    """

    source_key: str
    name: str
    ok: bool
    item_count: int
    error: str = ""

    def as_row(self) -> tuple[str, str, bool, str, int]:
        """``upsert_source_fetch_health`` が受け取る tuple 形式。"""
        return (self.source_key, self.name, self.ok, self.error, self.item_count)


@dataclass
class FetchObservation:
    """watcher が 1 回の取得を書き残す可変ホルダ (frozen watcher に持たせる)。

    ``src/watchers`` を DB 非依存に保つため、watcher はここに記録するだけで永続化
    しない。pipeline の seam (``src/pipeline/persistence.py``) が取り出して書く
    — RSS の ``DirectRssSource.last_results`` と同じ責務分離。
    """

    ok: bool = False
    item_count: int = 0
    error: str = ""
    # 一度も取得していない状態と「取得して失敗した」状態を区別する
    attempted: bool = False

    def record_success(self, item_count: int) -> None:
        self.ok = True
        self.item_count = item_count
        self.error = ""
        self.attempted = True

    def record_failure(self, error: str, item_count: int = 0) -> None:
        self.ok = False
        self.item_count = item_count
        self.error = error
        self.attempted = True

    def to_outcome(self, source_key: str, name: str) -> SourceFetchOutcome | None:
        """未取得なら None (この run で走らなかった watcher の記録を汚さない)。"""
        if not self.attempted:
            return None
        return SourceFetchOutcome(
            source_key=source_key,
            name=name,
            ok=self.ok,
            item_count=self.item_count,
            error=self.error,
        )

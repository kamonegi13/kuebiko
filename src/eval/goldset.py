"""gold set — 入力を凍結した評価用の記事集合。

**なぜ凍結するのか**: 本文は再抽出・purge・翻訳で変わりうる。入力が動くと
「出力が変わったのはプロンプトのせいか入力のせいか」が分からなくなる。同一入力
比較の前提は入力が不変であること。

**なぜ層化するのか**: CTI の致命的な失敗は「0day・日本標的の high を 1 件落とす」=
**稀少クラスの再現率**で、無作為抽出だと標本から漏れて平均指標に隠れる。
(importance x category) の層ごとに上限件数を取り、稀少層も必ず 1 件は入れる。

**置き場所**: 記事本文を含むため ``data/eval/`` (gitignore 済) に置く。
リポジトリは MIT 公開のため、収集記事を git に載せない (CLAUDE.md §9)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

# 本文がこれ未満の記事は除外する。薄い本文は「抽出の失敗」を測ることになり、
# 判定 (要約・分類) の評価にならない。
DEFAULT_MIN_BODY_CHARS = 500


@dataclass(frozen=True)
class GoldArticle:
    """凍結した 1 記事。``body`` は捕捉時点のスナップショット。"""

    article_id: str
    title: str
    body: str
    feed_title: str
    published: str | None
    # 層化に使ったラベル (捕捉時点の値)。後で標本の偏りを確認するために残す。
    category: str
    importance: str

    def stratum(self) -> tuple[str, str]:
        return (self.importance, self.category)


def select_goldset(
    candidates: Sequence[GoldArticle],
    *,
    per_stratum: int,
    min_body_chars: int = DEFAULT_MIN_BODY_CHARS,
) -> list[GoldArticle]:
    """(importance, category) の層ごとに最大 ``per_stratum`` 件を選ぶ。

    - **決定的**: 同じ入力からは常に同じ標本 (再現できない gold set は基準線にならない)。
      入力の並び順にも依存しない — article_id でソートしてから取る。
    - 稀少層は件数が少なくてもそのまま入る (上限を課すだけで下限は課さない)。
    - 本文が ``min_body_chars`` 未満の記事は除外する。
    """
    if per_stratum < 1:
        raise ValueError(f"per_stratum は 1 以上である必要がある: {per_stratum}")

    unique: dict[str, GoldArticle] = {}
    for a in candidates:
        if len(a.body) < min_body_chars:
            continue
        unique.setdefault(a.article_id, a)

    by_stratum: dict[tuple[str, str], list[GoldArticle]] = {}
    for a in sorted(unique.values(), key=lambda x: x.article_id):
        by_stratum.setdefault(a.stratum(), []).append(a)

    picked: list[GoldArticle] = []
    for stratum in sorted(by_stratum):
        picked.extend(by_stratum[stratum][:per_stratum])
    return picked


def save_goldset(path: Path, articles: Iterable[GoldArticle]) -> int:
    """JSONL で保存し、件数を返す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
            n += 1
    return n


def load_goldset(path: Path) -> list[GoldArticle]:
    with path.open(encoding="utf-8") as f:
        return [GoldArticle(**json.loads(line)) for line in f if line.strip()]

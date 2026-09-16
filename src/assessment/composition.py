"""安定フィードコホート上の構成比 — 型 E (趨勢) / H (閾値) の測定土台。

設計: docs/pir_brief_design.md §6c「E/H の土台」。

**解く問題**: 「N に対する脅威は悪化しているか」を記事数で測ると、活動が増えたのか
収集網が広がったのかを分離できない (CLAUDE.md §7「収集量を重要性の代理にしない」)。
実測 (2026-09-16) では収集網が 8 週で 114→245 フィードへ倍増する一方、両期間に居る
安定コホート 159 フィードが記事の 95.1% を出していた。

**解法**: 比較を**両窓に居るフィード**に限り、**件数でなく構成比**で測る。
- コホート限定 → 我々の網の成長を除去できる
- 構成比 → 実測では同一コホートでも絶対数が 4,465→2,634 に落ちており、件数では
  「全部減った」になる。比にして初めて軸の移動が見える

⚠ **除去できるのは我々の網の成長だけ**。世界の報道量の変動 (大型事案で全媒体が書く) は
残る。よって「観測バイアス」は消えるのではなく、答えられない仮説から**弱いが生きた仮説**
へ変わる。型の競合仮説から外してはならない。本モジュールが `CohortStats` に窓ごとの
フィード数を載せているのは、その仮説を**判定する材料**を呼出側へ渡すため。

⚠ **これは収集網の観測であって重要性ではない**。importance / routing / 記事選抜から
参照してはならない (burst と同じ構造の事故になる)。関門は
``tests/unit/test_composition_boundary.py`` の import 境界が固定する。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: 軸値の母集団がこれ未満なら構成比を語らせない (実測: prepositioning は N=62→55)。
DEFAULT_THIN_THRESHOLD = 30

P1 = "p1"
P2 = "p2"


@dataclass(frozen=True)
class AxisRow:
    """記事 1 件の (出所, 窓, 軸値)。軸値が空 = 未判定。"""

    feed_title: str
    window: str
    value: str


@dataclass(frozen=True)
class CohortStats:
    """コホートの素性 — 観測バイアス仮説を判定するための材料。"""

    feeds: int  # 両窓に居るフィード数 (= 比較に使った母数)
    p1_feeds_total: int  # 窓 1 の全フィード数
    p2_feeds_total: int  # 窓 2 の全フィード数
    p1_articles: int  # コホート内・窓 1 の記事数 (軸値ありのみ)
    p2_articles: int
    article_share: float  # コホートが全記事に占める割合 [0,1]。低いほど測定は弱い


@dataclass(frozen=True)
class ShareShift:
    """1 つの軸値の構成比の動き。**主役は share**、count は素性の開示。"""

    value: str
    p1_count: int
    p2_count: int
    p1_share: float
    p2_share: float
    shift_points: float  # (p2_share - p1_share) * 100 — 百分率ポイント


@dataclass(frozen=True)
class CompositionMeasurement:
    """コホート上の構成比測定の結果。"""

    cohort: CohortStats
    shifts: tuple[ShareShift, ...]
    thin_threshold: int

    @property
    def is_thin(self) -> bool:
        """母集団が薄く、構成比の変動が誤差に埋もれる状態。

        **型の適用前に母集団を数える** — 薄い軸で「悪化した」と言わせないための関門。
        """
        return min(self.cohort.p1_articles, self.cohort.p2_articles) < self.thin_threshold

    def shift_for(self, value: str) -> ShareShift | None:
        return next((s for s in self.shifts if s.value == value), None)


def _share(count: int, total: int) -> float:
    """ゼロ除算を 0.0 に倒す (NaN を下流へ流さない)。"""
    return count / total if total else 0.0


def measure_composition(
    rows: Iterable[AxisRow],
    *,
    thin_threshold: int = DEFAULT_THIN_THRESHOLD,
) -> CompositionMeasurement:
    """両窓に居るフィードだけで軸値の構成比を測る。

    Args:
        rows: 記事ごとの (出所, 窓, 軸値)。軸値が空の行は未判定として除外する
            (1 つの軸値として数えると構成比が歪む)。
        thin_threshold: これ未満の母集団は ``is_thin`` で薄いと申告する。
    """
    materialized = list(rows)
    feeds_by_window: dict[str, set[str]] = {P1: set(), P2: set()}
    for r in materialized:
        if r.window in feeds_by_window:
            feeds_by_window[r.window].add(r.feed_title)
    cohort_feeds = feeds_by_window[P1] & feeds_by_window[P2]

    scored = [r for r in materialized if r.value and r.window in (P1, P2)]
    in_cohort = [r for r in scored if r.feed_title in cohort_feeds]

    counts: dict[str, dict[str, int]] = {}
    for r in in_cohort:
        bucket = counts.setdefault(r.value, {P1: 0, P2: 0})
        bucket[r.window] += 1
    p1_total = sum(b[P1] for b in counts.values())
    p2_total = sum(b[P2] for b in counts.values())

    shifts = tuple(
        sorted(
            (
                ShareShift(
                    value=value,
                    p1_count=bucket[P1],
                    p2_count=bucket[P2],
                    p1_share=_share(bucket[P1], p1_total),
                    p2_share=_share(bucket[P2], p2_total),
                    shift_points=round(
                        (_share(bucket[P2], p2_total) - _share(bucket[P1], p1_total)) * 100, 4
                    ),
                )
                for value, bucket in counts.items()
            ),
            # 読み手が最初に見るべき動きを先頭へ (同点は値名で決定論に)
            key=lambda s: (-abs(s.shift_points), s.value),
        )
    )

    return CompositionMeasurement(
        cohort=CohortStats(
            feeds=len(cohort_feeds),
            p1_feeds_total=len(feeds_by_window[P1]),
            p2_feeds_total=len(feeds_by_window[P2]),
            p1_articles=p1_total,
            p2_articles=p2_total,
            article_share=_share(len(in_cohort), len(scored)),
        ),
        shifts=shifts,
        thin_threshold=thin_threshold,
    )

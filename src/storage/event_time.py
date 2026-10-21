"""事象時刻の錨 — 共有 SSoT (2026-08-23 に repo_synthesis から昇格)。

2026-08-22 の根本修正: **DB へ書いた時刻を事象時刻として使わない。**
article_entities.created_at は「entity 行を書いた時刻」であり、バックフィル
(再抽出 / 別名昇格 / intent・axes backfill) は過去記事へ当日の日付で書くため
事象時刻とは無関係になる (実測: 言及の 44.3% が 1 日超・34.4% が 7 日超ずれ、
週次 FC3 spike の 44% が偽陽性、日次バーストは単日最大 42 件の幻)。

昇格の理由 (2026-08-23、事象単位ニュース設計レビュー C3): 錨の規律を「踏襲」と
書くと複製が生まれ、複製は移植漏れを生む (ACH 2 経路の前科)。consumer は
repo_synthesis と src/eventnews の両方で、**錨式のリテラルはこのモジュールにのみ
存在する** (tests/unit/test_event_time_anchor.py が固定)。
"""

from __future__ import annotations

# 錨は「公開時刻。ただし取込より後にはならない (実測 0.5% が不正)。欠損は取込時刻」。
# 利用側は EVENT_TS_EXPR.format(a="<articles の別名>")。
EVENT_TS_EXPR = (
    "CASE WHEN {a}.published_at IS NOT NULL AND {a}.published_at <= {a}.created_at"
    " THEN {a}.published_at ELSE {a}.created_at END"
)

# articles は同一 article_id が複数行ありうる (実測 3,593 行 / 最大 7 行)。
# join 前に 1 行へ畳んで言及・媒体数の水増し (fan-out) を防ぐ。
DEDUP_ARTICLES = (
    "(SELECT article_id, MIN(created_at) AS created_at,"
    " MIN(published_at) AS published_at FROM articles GROUP BY article_id)"
)

# ---------------------------------------------------------------------------
# 時刻の語彙 (2026-10-18、SSoT)。画面ごとに「報道時刻」「発生時刻」の中身が揃っていなかった
# (アクター画面の報道時刻=取込時刻、発生時刻に basis=reported の報道日の写しが約 71% 混入)。
#   取込時刻   = articles.created_at。新着の窓にだけ使う
#   報道時刻   = 上の錨 (公開時刻、取込で上限)。「いつ報じられたか」
#   発生日     = event_date のうち basis が reported 以外。「いつ起きたか」
#   報道日のみ = basis=reported (event_date は報道日の写しで、発生日ではない)
# UI の文言は frontend/src/utils/timeVocab.ts に同じ語を置く
# (一致は tests/unit/test_time_vocab.py が固定する)。
# ---------------------------------------------------------------------------
TIME_VOCAB: dict[str, str] = {
    "ingested": "取込時刻",
    "reported": "報道時刻",
    "occurred": "発生日",
    "reported_only": "報道日のみ",
    "month_only": "月のみ",
    "unknown": "不明",
}

REPORTED_BASIS = "reported"
# 発生日の精度の列 (T0 が追加予定)。列が無い間は Python 側の判定で None 扱い。
MONTH_ONLY_PRECISIONS = frozenset({"month", "year"})

# SQL: 発生日として使える行 (basis=reported を外す)。利用側は .format(a="<別名>.")。
OCCURRED_DATE_CLAUSE = (
    "({a}event_date IS NOT NULL AND {a}event_date <> ''"
    " AND COALESCE({a}event_date_basis, '') <> 'reported')"
)


def occurred_date_clause(alias: str = "") -> str:
    """OCCURRED_DATE_CLAUSE を別名つきで返す (alias は 'a.' のように末尾ドット込み)。"""
    return OCCURRED_DATE_CLAUSE.format(a=alias)


def window_clause(time_basis: str, alias: str = "") -> str:
    """窓フィルタ SQL 片 (先頭 ' AND ' 込み、param は since.isoformat() を 1 つ追加)。

    report = 報道時刻 (COALESCE published/created)。
    event  = 「その期間に発生した」= 発生日 (basis=reported 除く) で窓を切る。
    地図・アクター・国家の情勢はすべてこの関数を使う。
    """
    if time_basis == "event":
        # event_date は TEXT 'YYYY-MM-DD' のため日付部の辞書順比較 (ISO は辞書順=時系列順)。
        return f" AND {alias}event_date >= substr(?, 1, 10) AND {occurred_date_clause(alias)}"
    pub, cre = f"{alias}published_at", f"{alias}created_at"
    return f" AND COALESCE(datetime({pub}), datetime({cre})) >= datetime(?)"


def classify_event_date(event_date: object, basis: object = None, precision: object = None) -> str:
    """行の発生日の状態: occurred / month_only / reported_only / unknown。

    unknown = 発生日なし。reported_only = basis=reported (報道日の写し)。
    precision (列が無い間は None) が month/year なら month_only。
    """
    if not event_date:
        return "unknown"
    if basis == REPORTED_BASIS:
        return "reported_only"
    if precision in MONTH_ONLY_PRECISIONS:
        return "month_only"
    return "occurred"


def event_date_label(
    event_date: object, basis: object = None, precision: object = None
) -> str | None:
    """発生日の表示文字列 (粒度を反映)。発生日として出せないものは None。

    day/unknown → 'YYYY-MM-DD'、month → 'YYYY-MM'、year → 'YYYY'、relative → 'YYYY-MM-DD頃'。
    basis=reported (報道日の写し) と日付なしは None (発生日として出さない)。
    """
    if not event_date or basis == REPORTED_BASIS:
        return None
    d = str(event_date)[:10]
    if precision == "month":
        return d[:7]
    if precision == "year":
        return d[:4]
    if precision == "relative":
        return f"{d}頃"
    return d


# 発生日で並べるときの日付キー (SQL)。発生日の無い記事 (報道日のみ・不明) は報道時刻の日付で
# 代わりに置き、並べ替えで黙って落とさない。利用側は .format(a="<別名>.", anchor=<錨式>)。
OCCURRED_SORT_KEY = (
    "COALESCE(CASE WHEN {occurred} THEN substr({a}event_date, 1, 10) END,"
    " substr(CAST({anchor} AS TEXT), 1, 10))"
)


def occurred_sort_key_sql(table: str) -> str:
    """OCCURRED_SORT_KEY を table 別名 (末尾ドットなし) で組み立てる。"""
    return OCCURRED_SORT_KEY.format(
        occurred=occurred_date_clause(f"{table}."),
        a=f"{table}.",
        anchor=f"({EVENT_TS_EXPR.format(a=table)})",
    )


def row_value(row: object, key: str) -> object:
    """Row から列を取る。列が無い (まだ追加されていない) なら None。"""
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError):
        return None


def anchor_ts(published_at: object, created_at: object) -> object:
    """EVENT_TS_EXPR の Python 版: 公開時刻 (取込を超えない)、欠損は取込時刻。"""
    if published_at is not None and created_at is not None and str(published_at) <= str(created_at):
        return published_at
    return created_at if created_at is not None else published_at


def coverage_counts(states: list[str], in_window: int = 0) -> dict[str, int]:
    """classify_event_date の結果列から被覆を作る。

    occurred=発生日 N / total=全 M / month_only=月のみ K / unknown=不明 U
    (unknown は発生日なし + 報道日のみ。うち reported_only を内訳として別に出す)。
    dated は後方互換 (= occurred)。
    """
    occurred = states.count("occurred")
    month_only = states.count("month_only")
    reported_only = states.count("reported_only")
    return {
        "dated": occurred,
        "occurred": occurred,
        "month_only": month_only,
        "reported_only": reported_only,
        "unknown": states.count("unknown") + reported_only,
        "total": len(states),
        "event_in_window": in_window,
    }


__all__ = [
    "DEDUP_ARTICLES",
    "EVENT_TS_EXPR",
    "OCCURRED_DATE_CLAUSE",
    "TIME_VOCAB",
    "anchor_ts",
    "event_date_label",
    "occurred_sort_key_sql",
    "classify_event_date",
    "coverage_counts",
    "occurred_date_clause",
    "row_value",
    "window_clause",
]

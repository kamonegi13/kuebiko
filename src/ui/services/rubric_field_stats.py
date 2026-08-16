"""summarizer 判定基準カードの「この基準が実際にどう効いているか」の集計 (WP-B3)。

判定基準エディタは長らく「プロンプト本文」しか見せておらず、基準を書き換える人は
その基準が実データにどう出ているか (どの値がどれだけ出ているか / そもそも埋まって
いるか) を別画面で探すしかなかった。本 module は各フィールドの**直近 N 日の実出力**を
集計し、カード内に添える 1 枚の JSON を組み立てる。

不変量 (docs 設計 §4/§9):

- **分母は「配信済み記事」を明示する**。`status='posted'` を ``COUNT(DISTINCT article_id)``
  で数える (同一 article_id の複数 run 行があるため)。取込のみ / 重複スキップ / 抽出失敗は
  含まない — この定義は fill_rate_audit と揃える (分岐を作らない)。
- **0% と「統計なし」を混同させない**。DB に列が無いフィールドは ``availability="none"`` と
  理由だけを返し、数値 (coverage / distribution) を一切出さない。
- **カテゴリ分母と充足条件は複製しない**。scope は ``src/cti/category_scopes.py``、充足条件は
  ``fill_rate_audit.METRICS``、日本標的は ``japan_relevance`` から import する。
- **SQL は SQLite/PG 両対応**。集約列には必ず別名を付け (PG の dict-row で列名衝突すると
  潰れる)、値は**位置**で読む。SELECT の非集約列はすべて GROUP BY に入れる (bare column
  GROUP BY は SQLite で通り PG で全滅する)。``translate_sql`` が触る構文
  (``substr(x,1,10)`` / ``GROUP_CONCAT`` / ``datetime()``) は使わない。
- **ログに記事本文・タイトル・プロンプトを出さない** (days / 件数 / 所要 ms のみ)。
- **窓は [until - days, until) の半開区間**。``until`` 既定は now で、その場合は上限条件を
  SQL に**足さない** (従来と 1 文字も変わらない SQL のまま)。過去の窓を切り出したい呼び手
  (プロンプト切替の前後比較など) だけが ``until`` を明示する。

公開 API は ``build_field_stats`` (毎回集計) と ``field_stats_cached`` (TTL 5 分) の 2 本。
同期関数として書く — 30〜90 日走査を ``async def`` に載せると event loop を占有して
UI 全体が固まる (PIR 一覧 18 秒→2.5 秒 の教訓)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.cti.category_scopes import (
    COVERAGE_CYBER,
    COVERAGE_CYBER_EVENT,
    COVERAGE_GEOPOLITICAL,
    COVERAGE_VULN,
)
from src.cti.japan_relevance import JAPAN_TARGETED_SQL_FRAGMENT
from src.logging_config import get_logger
from src.pipeline.summary import SummaryOutput, _has_japanese_kana
from src.storage.db_backend import connect
from src.ui.services.fill_rate_audit import METRICS, FillMetric

_log = get_logger(__name__)

# ---------- 定数 ----------

MIN_DAYS = 1
MAX_DAYS = 90
_STATS_TTL_SEC = 300.0
# タイトルのかな判定は Python 側で行うため、走査行数に上限を置く (90 日でも応答を保つ)
_TITLE_SAMPLE_LIMIT = 5000
# 上位値リストの表示件数と、ノイズ (1 回きりの固有名詞) を落とす下限出現数
_TOP_VALUES = 8
_MIN_TOP_COUNT = 2
# summary 文字数の帯 (SQL 別名, 下限, 上限 or None, 帯 ID, ラベル)。
# 別名は SQL 識別子になるため帯 ID (ハイフン込み) と分ける。
_SUMMARY_BANDS: tuple[tuple[str, int, int | None, str, str], ...] = (
    ("n_sum_b0", 0, 100, "0-99", "100 字未満"),
    ("n_sum_b1", 100, 250, "100-249", "100〜249 字"),
    ("n_sum_b2", 250, 500, "250-499", "250〜499 字"),
    ("n_sum_b3", 500, 800, "500-799", "500〜799 字"),
    ("n_sum_b4", 800, None, "800+", "800 字以上"),
)
# PMESII 列 → 軸 ID (SSoT = src/cti/taxonomy_normalizer.PMESII_AXES)。
# T 軸は 2026-07-16 に廃止済みのため集計しない。
_PMESII_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pmesii_p", "P"),
    ("pmesii_m", "M"),
    ("pmesii_e", "E"),
    ("pmesii_s", "S"),
    ("pmesii_i_infra", "I-infra"),
    ("pmesii_i_cyber", "I-cyber"),
    ("pmesii_p_env", "P-env"),
)
# IoC として数える entity_type (cve を含む — カードの「技術指標」は CVE も IoC 扱い)
_IOC_TYPES: tuple[str, ...] = (
    "ioc_ip",
    "ioc_domain",
    "ioc_url",
    "ioc_sha256",
    "ioc_sha1",
    "ioc_md5",
    "cve",
)
# 上位値を出す entity_type (ioc_* は値の種類が爆発するため件数のみ)
_TOP_ENTITY_TYPES_CYBER: tuple[str, ...] = (
    "ttp",
    "malware_family",
    "tool",
    "victim_org",
    "victim_city",
)
_TOP_ENTITY_TYPES_GEO: tuple[str, ...] = ("involved_country",)

_DENOMINATOR_LABEL = "配信済み記事"
_DENOMINATOR_NOTE = (
    "articles.status='posted' の記事を article_id で重複排除した件数。"
    "取込のみ・重複スキップ・抽出失敗の記事は含みません。"
)
_TOP_VALUES_NOTE = "上位値は期間内に 2 件以上の記事で出現した値のみを並べています。"

_SCOPE_LABEL_ALL = _DENOMINATOR_LABEL
_SCOPE_LABEL_CYBER = "サイバー系カテゴリの記事"
_SCOPE_LABEL_VULN = "脆弱性・アドバイザリの記事"
_SCOPE_LABEL_CYBER_EVENT = "侵入事案系カテゴリの記事"
_SCOPE_LABEL_GEO = "地政学・政策カテゴリの記事"

_STATS_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


# ---------- SQL 断片の組み立て ----------


@dataclass(frozen=True)
class _Agg:
    """Q1 (単一パスのスカラ集計) の 1 列。cond は articles alias ``a`` に対する boolean 式。"""

    alias: str
    cond: str
    params: tuple[str, ...] = ()


def _until_clause(until_iso: str | None, alias: str = "a") -> str:
    """窓の上限 (半開区間の右端) を表す SQL 断片。

    ``until_iso`` が None のときは**空文字**を返す。既存の呼び出し (endpoint / cache) は
    上限を渡さないため、生成される SQL は窓上限の導入前と完全に一致する。
    """
    return f" AND {alias}.created_at < ?" if until_iso else ""


def _window_params(since_iso: str, until_iso: str | None) -> list[str]:
    """``created_at >= ?`` (+ 任意で ``< ?``) に対応する bind 値。順序が SQL と対応する。"""
    return [since_iso] if until_iso is None else [since_iso, until_iso]


def _metric(key: str) -> FillMetric:
    """fill_rate_audit の同名 METRIC を引く (充足条件の定義を複製しないため)。"""
    for m in METRICS:
        if m.key == key:
            return m
    raise KeyError(f"unknown fill metric: {key}")


def _cat_clause(categories: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    placeholders = ", ".join("?" for _ in categories)
    return (f"a.category IN ({placeholders})", tuple(categories))


def _scoped(cond: str, categories: tuple[str, ...] | None) -> tuple[str, tuple[str, ...]]:
    if not categories:
        return (cond, ())
    clause, params = _cat_clause(categories)
    return (f"({cond}) AND {clause}", params)


def _metric_agg(alias: str, key: str) -> _Agg:
    """METRIC の condition + categories をそのまま 1 列の集計にする。"""
    m = _metric(key)
    cond, params = _scoped(m.condition, m.categories)
    return _Agg(alias, cond, params)


def _scope_agg(alias: str, categories: tuple[str, ...]) -> _Agg:
    clause, params = _cat_clause(categories)
    return _Agg(alias, clause, params)


def _summary_band_cond(low: int, high: int | None) -> str:
    upper = "" if high is None else f" AND LENGTH(a.summary) < {high}"
    return f"a.summary IS NOT NULL AND a.summary <> '' AND LENGTH(a.summary) >= {low}{upper}"


def _ioc_exists_cond() -> tuple[str, tuple[str, ...]]:
    placeholders = ", ".join("?" for _ in _IOC_TYPES)
    cond = (
        "EXISTS (SELECT 1 FROM article_entities e"
        f" WHERE e.article_id = a.article_id AND e.entity_type IN ({placeholders}))"
    )
    # placeholder の並び = EXISTS 内の entity_type → カテゴリ scope の順
    scoped_cond, cat_params = _scoped(cond, COVERAGE_CYBER)
    return (scoped_cond, tuple(_IOC_TYPES) + cat_params)


def _scalar_aggs() -> tuple[_Agg, ...]:
    """Q1 の集計列 (順序が結果の位置と対応するため固定)。"""
    ioc_cond, ioc_params = _ioc_exists_cond()
    pmesii_any = " OR ".join(f"a.{col} = 1" for col, _ in _PMESII_COLUMNS)
    aggs: list[_Agg] = [
        _scope_agg("n_cyber", COVERAGE_CYBER),
        _scope_agg("n_vuln", COVERAGE_VULN),
        _scope_agg("n_cyber_event", COVERAGE_CYBER_EVENT),
        _scope_agg("n_geo", COVERAGE_GEOPOLITICAL),
        _Agg("n_summary", "a.summary IS NOT NULL AND a.summary <> ''"),
        _metric_agg("n_victim_sector", "victim_sector"),
        _metric_agg("n_victim_country", "victim_country"),
        _metric_agg("n_remediation", "remediation"),
        _metric_agg("n_event_date", "event_date_cyber"),
        _metric_agg("n_compromise_date", "compromise_date"),
        _metric_agg("n_primary_actor", "llm_primary_actor"),
        _metric_agg("n_technical", "technical_axis"),
        _Agg("n_event_date_basis", "a.event_date_basis IS NOT NULL AND a.event_date_basis <> ''"),
        _Agg("n_dedup", "a.dedup_key IS NOT NULL AND a.dedup_key <> ''"),
        _Agg("n_japan", JAPAN_TARGETED_SQL_FRAGMENT),
        _Agg("n_ioc_any", ioc_cond, ioc_params),
        _Agg("n_pmesii_any", pmesii_any),
    ]
    aggs.extend(_Agg(f"n_{col}", f"a.{col} = 1") for col, _ in _PMESII_COLUMNS)
    aggs.extend(
        _Agg(alias, _summary_band_cond(low, high)) for alias, low, high, _, _ in _SUMMARY_BANDS
    )
    return tuple(aggs)


def _build_scalar_query(
    since_iso: str, until_iso: str | None = None
) -> tuple[str, list[str], tuple[_Agg, ...]]:
    """Q1: 母数・全 coverage・summary 帯・PMESII 軸を 1 パスで数える。"""
    aggs = _scalar_aggs()
    params: list[str] = []
    columns = ["COUNT(DISTINCT a.article_id) AS n_total"]
    for agg in aggs:
        columns.append(f"COUNT(DISTINCT CASE WHEN {agg.cond} THEN a.article_id END) AS {agg.alias}")
        params.extend(agg.params)
    params.extend(_window_params(since_iso, until_iso))
    sql = (
        "SELECT "
        + ", ".join(columns)
        + " FROM articles a WHERE a.status = 'posted' AND a.created_at >= ?"
        + _until_clause(until_iso)
    )
    return (sql, params, aggs)


# ---------- クエリ実行 ----------


def _fetch_scalars(con: Any, since_iso: str, until_iso: str | None) -> dict[str, int]:
    sql, params, aggs = _build_scalar_query(since_iso, until_iso)
    row = con.execute(sql, params).fetchone()
    if row is None:
        return {"n_total": 0, **{a.alias: 0 for a in aggs}}
    out = {"n_total": int(row[0] or 0)}
    for index, agg in enumerate(aggs, start=1):
        out[agg.alias] = int(row[index] or 0)
    return out


def _fetch_avg_lengths(
    con: Any, since_iso: str, until_iso: str | None
) -> tuple[float | None, float | None]:
    """summary / remediation の平均文字数 (article_id 単位に潰してから平均)。"""
    sql = (
        "SELECT AVG(CASE WHEN s_len > 0 THEN s_len END) AS avg_summary,"
        " AVG(CASE WHEN r_len > 0 THEN r_len END) AS avg_remediation"
        " FROM (SELECT a.article_id AS aid,"
        " MAX(LENGTH(COALESCE(a.summary, ''))) AS s_len,"
        " MAX(LENGTH(COALESCE(a.remediation, ''))) AS r_len"
        " FROM articles a WHERE a.status = 'posted' AND a.created_at >= ?"
        + _until_clause(until_iso)
        + " GROUP BY a.article_id) t"
    )
    row = con.execute(sql, _window_params(since_iso, until_iso)).fetchone()
    if row is None:
        return (None, None)
    avg_summary = None if row[0] is None else float(row[0])
    avg_remediation = None if row[1] is None else float(row[1])
    return (avg_summary, avg_remediation)


def _fetch_distribution(
    con: Any,
    since_iso: str,
    until_iso: str | None,
    column: str,
    categories: tuple[str, ...] | None = None,
) -> list[tuple[str, int]]:
    """列 1 本の値分布 (NULL は空文字バケット)。非集約列は GROUP BY に同じ式を入れる。"""
    bucket = f"COALESCE(a.{column}, '')"
    params: list[str] = _window_params(since_iso, until_iso)
    cat_sql = ""
    if categories:
        clause, cat_params = _cat_clause(categories)
        cat_sql = f" AND {clause}"
        params.extend(cat_params)
    sql = (
        "SELECT " + bucket + " AS bucket, COUNT(DISTINCT a.article_id) AS n"
        " FROM articles a WHERE a.status = 'posted' AND a.created_at >= ?"
        + _until_clause(until_iso)
        + cat_sql
        + " GROUP BY "
        + bucket
    )
    rows = con.execute(sql, params).fetchall()
    return sorted(((str(r[0]), int(r[1])) for r in rows), key=lambda x: (-x[1], x[0]))


def _posted_exists(
    categories: tuple[str, ...], until_iso: str | None
) -> tuple[str, tuple[str, ...]]:
    clause, params = _cat_clause(categories)
    cond = (
        "EXISTS (SELECT 1 FROM articles a WHERE a.article_id = e.article_id"
        f" AND a.status = 'posted' AND a.created_at >= ?{_until_clause(until_iso)} AND {clause})"
    )
    return (cond, params)


def _fetch_entity_counts(
    con: Any, since_iso: str, until_iso: str | None, categories: tuple[str, ...]
) -> dict[str, tuple[int, int]]:
    """entity_type → (記事数, 値の件数)。scope 内の配信済み記事に限る。"""
    exists_cond, cat_params = _posted_exists(categories, until_iso)
    sql = (
        "SELECT e.entity_type AS etype, COUNT(DISTINCT e.article_id) AS n_articles,"
        " COUNT(*) AS n_values"
        " FROM article_entities e"
        " WHERE e.created_at >= ?"
        + _until_clause(until_iso, "e")
        + " AND "
        + exists_cond
        + " GROUP BY e.entity_type"
    )
    params: list[str] = [
        *_window_params(since_iso, until_iso),
        *_window_params(since_iso, until_iso),
        *cat_params,
    ]
    rows = con.execute(sql, params).fetchall()
    return {str(r[0]): (int(r[1]), int(r[2])) for r in rows}


def _fetch_entity_top(
    con: Any,
    since_iso: str,
    until_iso: str | None,
    categories: tuple[str, ...],
    entity_types: tuple[str, ...],
) -> dict[str, list[tuple[str, int]]]:
    """entity_type → 上位値。上位 N の切り出しは Python 側で行う (方言安全)。"""
    if not entity_types:
        return {}
    exists_cond, cat_params = _posted_exists(categories, until_iso)
    type_placeholders = ", ".join("?" for _ in entity_types)
    sql = (
        "SELECT e.entity_type AS etype, e.value AS val, COUNT(DISTINCT e.article_id) AS n"
        " FROM article_entities e"
        " WHERE e.created_at >= ?"
        + _until_clause(until_iso, "e")
        + f" AND e.entity_type IN ({type_placeholders}) AND "
        + exists_cond
        + " GROUP BY e.entity_type, e.value"
        f" HAVING COUNT(*) >= {_MIN_TOP_COUNT}"
    )
    params: list[str] = [
        *_window_params(since_iso, until_iso),
        *entity_types,
        *_window_params(since_iso, until_iso),
        *cat_params,
    ]
    out: dict[str, list[tuple[str, int]]] = {}
    for row in con.execute(sql, params).fetchall():
        out.setdefault(str(row[0]), []).append((str(row[1]), int(row[2])))
    return {k: sorted(v, key=lambda x: (-x[1], x[0]))[:_TOP_VALUES] for k, v in out.items()}


def _fetch_title_kana(con: Any, since_iso: str, until_iso: str | None) -> tuple[int, int, bool]:
    """(かなを含むタイトル数, 標本数, 標本上限に達したか)。判定は summary.py の SSoT を使う。"""
    sql = (
        "SELECT a.article_id AS aid, a.title AS title FROM articles a"
        " WHERE a.status = 'posted' AND a.created_at >= ?"
        + _until_clause(until_iso)
        + f" ORDER BY a.created_at DESC LIMIT {_TITLE_SAMPLE_LIMIT}"
    )
    rows = con.execute(sql, _window_params(since_iso, until_iso)).fetchall()
    seen: dict[str, str] = {}
    for row in rows:
        seen.setdefault(str(row[0]), str(row[1] or ""))
    kana = sum(1 for title in seen.values() if _has_japanese_kana(title))
    return (kana, len(seen), len(rows) >= _TITLE_SAMPLE_LIMIT)


# ---------- 応答の部品 ----------


def _rate(filled: int, total: int) -> float:
    return round(filled / total, 4) if total else 0.0


def _coverage(filled: int, total: int, scope_label: str) -> dict[str, Any]:
    return {
        "filled": filled,
        "total": total,
        "rate": _rate(filled, total),
        "scope_label": scope_label,
    }


def _bucket(
    value: str,
    count: int,
    total: int,
    *,
    label: str | None = None,
    vocab: str | None = None,
    drilldown: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "label": label if label is not None else ("未設定" if value == "" else None),
        "vocab": vocab if value else None,
        "count": count,
        "share": _rate(count, total),
        "drilldown_query": drilldown,
    }


def _sub(
    key: str,
    filled: int,
    total: int,
    *,
    label: str | None = None,
    vocab: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "vocab": vocab,
        "filled": filled,
        "total": total,
        "rate": _rate(filled, total),
        "note": note,
    }


def _average(label: str, value: float | None, unit: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"label": label, "value": round(value, 1), "unit": unit}


def _stat(
    field_id: str,
    *,
    availability: str = "full",
    source_note: str,
    notes: list[str] | None = None,
    coverage: dict[str, Any] | None = None,
    distribution: list[dict[str, Any]] | None = None,
    sub_metrics: list[dict[str, Any]] | None = None,
    average: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "field_id": field_id,
        "availability": availability,
        "source_note": source_note,
        "notes": notes or [],
        "coverage": coverage,
        "distribution": distribution or [],
        "sub_metrics": sub_metrics or [],
        "average": average,
    }


def _none_stat(field_id: str, source_note: str) -> dict[str, Any]:
    """統計を出せないフィールド (DB に列が無い)。0% と混同させないため数値を一切載せない。"""
    return _stat(field_id, availability="none", source_note=source_note)


# ---------- 集計コンテキスト ----------


@dataclass(frozen=True)
class _Ctx:
    """1 回の集計で読み取った素の数値 (フィールド組み立ての入力)。"""

    days: int
    scalars: dict[str, int]
    avg_summary_len: float | None
    avg_remediation_len: float | None
    dist: dict[str, list[tuple[str, int]]]
    ent_cyber: dict[str, tuple[int, int]]
    ent_geo: dict[str, tuple[int, int]]
    top_cyber: dict[str, list[tuple[str, int]]]
    top_geo: dict[str, list[tuple[str, int]]]
    title_kana: int
    title_sample: int
    title_truncated: bool

    @property
    def total(self) -> int:
        return self.scalars.get("n_total", 0)

    def n(self, alias: str) -> int:
        return self.scalars.get(alias, 0)

    @property
    def since_hours(self) -> int:
        return self.days * 24


def _entity_field(
    field_id: str,
    entity_type: str,
    *,
    source_note: str,
    scope_total: int,
    scope_label: str,
    counts: dict[str, tuple[int, int]],
    tops: dict[str, list[tuple[str, int]]],
    vocab: str | None = None,
    notes: list[str] | None = None,
    average_label: str | None = None,
) -> dict[str, Any]:
    """article_entities 由来のフィールド (coverage + 上位値 + 平均) を組み立てる。"""
    n_articles, n_values = counts.get(entity_type, (0, 0))
    top = tops.get(entity_type, [])
    all_notes = list(notes or [])
    all_notes.append(_TOP_VALUES_NOTE)
    average = None
    if average_label and n_articles:
        average = _average(average_label, n_values / n_articles, "件")
    return _stat(
        field_id,
        source_note=source_note,
        notes=all_notes,
        coverage=_coverage(n_articles, scope_total, scope_label),
        distribution=[_bucket(v, c, scope_total, vocab=vocab) for v, c in top],
        average=average,
    )


# ---------- フィールド別の組み立て ----------


def _classification_fields(ctx: _Ctx) -> dict[str, dict[str, Any]]:
    hours = ctx.since_hours
    importance = [
        _bucket(v, c, ctx.total, vocab="importance", drilldown=_news_query("importance", v, hours))
        for v, c in ctx.dist["importance"]
    ]
    category = [
        _bucket(v, c, ctx.total, vocab="category", drilldown=_news_query("category", v, hours))
        for v, c in ctx.dist["category"]
    ]
    return {
        "importance": _stat(
            "importance",
            source_note="articles.importance",
            notes=[
                "vulnerability / advisory の high は、悪用の裏付け (KEV 掲載・実悪用・0day) が"
                "本文に無いと保存前に medium へ自動降格されます。ここで見えるのは降格後の値です。"
            ],
            distribution=importance,
        ),
        "category": _stat("category", source_note="articles.category", distribution=category),
        "article_type": _stat(
            "article_type",
            source_note="articles.article_type",
            notes=["取込時に judgment_classifier が確定します。"],
            distribution=[
                _bucket(v, c, ctx.total, vocab="article_type") for v, c in ctx.dist["article_type"]
            ],
        ),
    }


def _routing_fields(ctx: _Ctx) -> dict[str, dict[str, Any]]:
    axes = [
        _sub(axis_id, ctx.n(f"n_{col}"), ctx.total, vocab="pmesii_axis")
        for col, axis_id in _PMESII_COLUMNS
    ]
    axis_sum = sum(ctx.n(f"n_{col}") for col, _ in _PMESII_COLUMNS)
    return {
        "routing_flags": _stat(
            "routing_flags",
            availability="partial",
            source_note="サブフラグごとに別の列・代理指標から数えています",
            notes=[
                "is_breaking_critical は保存されていないため計上できません。",
                "japan_targeted は LLM フラグそのものではなく、被害国=JP または"
                " japan_watch 配信を代理指標にしています。",
                "primary_actor_id は judgment_classifier の生出力 (llm_primary_actor_raw) を"
                "代理指標にしています。",
                "dedup_key は LLM が空を返した場合にタイトルから自動生成されるため、"
                "充足率は LLM の出力率より高く出ます。",
            ],
            sub_metrics=[
                _sub("japan_targeted", ctx.n("n_japan"), ctx.total, label="日本標的 (代理指標)"),
                _sub(
                    "primary_actor_id",
                    ctx.n("n_primary_actor"),
                    ctx.n("n_cyber"),
                    label="主要アクター名あり",
                    note="サイバー系カテゴリのみを分母にしています",
                ),
                _sub("dedup_key", ctx.n("n_dedup"), ctx.total, label="同事象キーあり"),
            ],
        ),
        "pmesii_axes": _stat(
            "pmesii_axes",
            source_note="articles.pmesii_* (7 軸それぞれ別の列)",
            notes=["T 軸は 2026-07-16 に廃止済みのため集計対象外です。"],
            coverage=_coverage(ctx.n("n_pmesii_any"), ctx.total, "1 軸以上が立っている記事"),
            sub_metrics=axes,
            average=_average(
                "1 記事あたりの軸数", axis_sum / ctx.total if ctx.total else None, "軸"
            ),
        ),
    }


def _victim_fields(ctx: _Ctx) -> dict[str, dict[str, Any]]:
    cyber = ctx.n("n_cyber")
    sector_top = [
        _bucket(v, c, cyber, vocab="sector") for v, c in ctx.dist["victim_sector_canonical"] if v
    ][:_TOP_VALUES]
    country_top = [
        _bucket(v, c, cyber, vocab="country") for v, c in ctx.dist["victim_country_iso"] if v
    ][:_TOP_VALUES]
    fields = {
        "victim_country": _stat(
            "victim_country",
            source_note="articles.victim_country_iso / victim_country_scope",
            notes=[
                "国 ISO に解決できない global / regional / 複数国は充足に数えますが、"
                "上位値には現れません。"
            ],
            coverage=_coverage(ctx.n("n_victim_country"), cyber, _SCOPE_LABEL_CYBER),
            distribution=country_top,
        ),
        "victim_sector": _stat(
            "victim_sector",
            source_note="articles.victim_sector_canonical",
            notes=["正規化辞書に無い分野は uncategorized として未充足に数えます。"],
            coverage=_coverage(ctx.n("n_victim_sector"), cyber, _SCOPE_LABEL_CYBER),
            distribution=sector_top,
        ),
        "victim_city": _entity_field(
            "victim_city",
            "victim_city",
            source_note="article_entities (victim_city)",
            scope_total=cyber,
            scope_label=_SCOPE_LABEL_CYBER,
            counts=ctx.ent_cyber,
            tops=ctx.top_cyber,
        ),
        "victim_orgs": _entity_field(
            "victim_orgs",
            "victim_org",
            source_note="article_entities (victim_org)",
            scope_total=cyber,
            scope_label=_SCOPE_LABEL_CYBER,
            counts=ctx.ent_cyber,
            tops=ctx.top_cyber,
            average_label="抽出のある記事 1 件あたり",
        ),
        "involved_countries": _entity_field(
            "involved_countries",
            "involved_country",
            source_note="article_entities (involved_country)",
            scope_total=ctx.n("n_geo"),
            scope_label=_SCOPE_LABEL_GEO,
            counts=ctx.ent_geo,
            tops=ctx.top_geo,
            vocab="country",
            notes=["サイバー事案では空が正しいため、地政学・政策カテゴリのみを分母にしています。"],
            average_label="抽出のある記事 1 件あたり",
        ),
    }
    return fields


def _technical_fields(ctx: _Ctx) -> dict[str, dict[str, Any]]:
    cyber = ctx.n("n_cyber")
    ioc_values = sum(ctx.ent_cyber.get(t, (0, 0))[1] for t in _IOC_TYPES)
    ioc_articles = ctx.n("n_ioc_any")
    ioc_dist = [
        _bucket(t, ctx.ent_cyber.get(t, (0, 0))[1], ioc_values, vocab="entity_type")
        for t in _IOC_TYPES
        if ctx.ent_cyber.get(t, (0, 0))[1]
    ]
    return {
        "iocs": _stat(
            "iocs",
            source_note="article_entities (" + " / ".join(_IOC_TYPES) + ")",
            notes=[
                "記事自身の URL は保存前に除去されます。",
                "IoC は検証・正規化を通った後の値のため、"
                "LLM が出した件数より少なくなることがあります。",
            ],
            coverage=_coverage(ioc_articles, cyber, _SCOPE_LABEL_CYBER),
            distribution=sorted(ioc_dist, key=lambda b: -int(b["count"])),
            average=_average(
                "抽出のある記事 1 件あたり",
                ioc_values / ioc_articles if ioc_articles else None,
                "件",
            ),
        ),
        "mitre_techniques": _entity_field(
            "mitre_techniques",
            "ttp",
            source_note="article_entities (ttp)",
            scope_total=cyber,
            scope_label=_SCOPE_LABEL_CYBER,
            counts=ctx.ent_cyber,
            tops=ctx.top_cyber,
            average_label="抽出のある記事 1 件あたり",
        ),
        "malware_families": _entity_field(
            "malware_families",
            "malware_family",
            source_note="article_entities (malware_family)",
            scope_total=cyber,
            scope_label=_SCOPE_LABEL_CYBER,
            counts=ctx.ent_cyber,
            tops=ctx.top_cyber,
            notes=[
                "正規化辞書が canonical 化・tool への再分類・未収録の drop を行うため、"
                "LLM が出した名前がそのまま残るとは限りません。"
            ],
        ),
        "tools": _entity_field(
            "tools",
            "tool",
            source_note="article_entities (tool)",
            scope_total=cyber,
            scope_label=_SCOPE_LABEL_CYBER,
            counts=ctx.ent_cyber,
            tops=ctx.top_cyber,
            notes=["malware 本体は malware_families 側に再分類されるため、ここには残りません。"],
        ),
    }


def _narrative_fields(ctx: _Ctx) -> dict[str, dict[str, Any]]:
    bands = [
        _bucket(band_id, ctx.n(alias), ctx.total, label=label)
        for alias, _, _, band_id, label in _SUMMARY_BANDS
    ]
    title_notes = [
        "元から日本語のタイトルも含むため、LLM の翻訳実施率ではありません。",
        "幻覚検出による原題への差し戻しは DB に残らないため計上されません。",
    ]
    if ctx.title_truncated:
        title_notes.append(f"直近 {_TITLE_SAMPLE_LIMIT:,} 件の標本です。")
    return {
        "title_ja": _stat(
            "title_ja",
            availability="partial",
            source_note="articles.title にひらがな・カタカナが含まれる割合",
            notes=title_notes,
            coverage=_coverage(ctx.title_kana, ctx.title_sample, _SCOPE_LABEL_ALL),
        ),
        "summary": _stat(
            "summary",
            source_note="articles.summary の充足率と文字数",
            coverage=_coverage(ctx.n("n_summary"), ctx.total, _SCOPE_LABEL_ALL),
            distribution=bands,
            average=_average("平均文字数", ctx.avg_summary_len, "字"),
        ),
        "analyst_note": _none_stat(
            "analyst_note",
            "この項目は DB に列がありません (Discord 投稿にのみ使われ、永続化されていません)。",
        ),
        "remediation": _stat(
            "remediation",
            source_note="articles.remediation",
            notes=["本文に対処の記載が無ければ null が正しいため 100% にはなりません。"],
            coverage=_coverage(ctx.n("n_remediation"), ctx.n("n_vuln"), _SCOPE_LABEL_VULN),
            average=_average("平均文字数", ctx.avg_remediation_len, "字"),
        ),
    }


def _suppressed_fields(ctx: _Ctx) -> dict[str, dict[str, Any]]:
    cyber_event = ctx.n("n_cyber_event")
    return {
        "editorial_stance": _stat(
            "editorial_stance",
            source_note="articles.editorial_stance",
            notes=["summarizer には出力させていません。値は focused 分類器が確定します。"],
            distribution=[
                _bucket(v, c, ctx.total, vocab="stance") for v, c in ctx.dist["editorial_stance"]
            ],
        ),
        "bluf": _none_stat("bluf", "RSS 経路では生成させていません (DB にも列がありません)。"),
        "diamond": _stat(
            "diamond",
            availability="partial",
            source_note="articles.socio_political_intent / technical_axis_summary",
            notes=["取込時に analysis_axes_classifier が確定します。"],
            distribution=[
                _bucket(v, c, ctx.total, vocab="intent")
                for v, c in ctx.dist["socio_political_intent"]
            ],
            sub_metrics=[
                _sub(
                    "technical_axis_summary",
                    ctx.n("n_technical"),
                    ctx.n("n_cyber"),
                    label="技術結線あり",
                    note="サイバー系カテゴリのみを分母にしています",
                )
            ],
        ),
        "event_date": _stat(
            "event_date",
            source_note="articles.event_date",
            notes=["取込時に analysis_axes_classifier が確定します。"],
            coverage=_coverage(ctx.n("n_event_date"), cyber_event, _SCOPE_LABEL_CYBER_EVENT),
        ),
        "event_date_basis": _stat(
            "event_date_basis",
            source_note="articles.event_date_basis",
            coverage=_coverage(ctx.n("n_event_date_basis"), ctx.total, _SCOPE_LABEL_ALL),
            distribution=[
                _bucket(v, c, ctx.total, vocab="event_date_basis")
                for v, c in ctx.dist["event_date_basis"]
            ],
        ),
        "compromise_date": _stat(
            "compromise_date",
            source_note="articles.compromise_date",
            notes=[
                "初期侵害日が本文に明示される記事は少ないため、低い充足率が正常です。",
            ],
            coverage=_coverage(ctx.n("n_compromise_date"), cyber_event, _SCOPE_LABEL_CYBER_EVENT),
        ),
    }


def _news_query(param: str, value: str, hours: int) -> str | None:
    """記事一覧へのドリルダウン クエリ (空値バケットは絞り込めないので None)。"""
    if not value:
        return None
    return f"{param}={value}&since={hours}"


def _build_all_fields(ctx: _Ctx) -> dict[str, dict[str, Any]]:
    """24 フィールド分を組み立てる。SummaryOutput に未知フィールドが増えても消さない。"""
    fields: dict[str, dict[str, Any]] = {}
    fields.update(_classification_fields(ctx))
    fields.update(_routing_fields(ctx))
    fields.update(_victim_fields(ctx))
    fields.update(_technical_fields(ctx))
    fields.update(_narrative_fields(ctx))
    fields.update(_suppressed_fields(ctx))
    for name in SummaryOutput.model_fields:
        if name not in fields:
            fields[name] = _none_stat(
                name, "この項目の集計はまだ実装されていません (集計元が未定義)。"
            )
    return fields


# ---------- 公開 API ----------


def build_field_stats(
    days: int, *, db_path: Path | None = None, until: datetime | None = None
) -> dict[str, Any]:
    """``[until - days, until)`` の配信済み記事から、判定基準 24 フィールドの実出力統計を作る。

    ``until`` 既定は now で、その場合は窓の上限条件を SQL に足さない (endpoint / cache の
    既存挙動と完全に同一)。過去の窓を切り出す呼び手 (プロンプト切替の前後比較など) だけが
    明示する。naive datetime は UTC とみなす。

    ``db_path`` は SQLite (dev / tests) 用の明示指定。production は DATABASE_URL の PG。
    """
    if days < MIN_DAYS or days > MAX_DAYS:
        raise ValueError(f"days は {MIN_DAYS}〜{MAX_DAYS} の範囲で指定してください (given: {days})")
    started = time.monotonic()
    now = datetime.now(UTC)
    window_end, until_iso = _resolve_until(until, now)
    since = window_end - timedelta(days=days)
    since_iso = since.isoformat()
    con = _connect(db_path)
    try:
        ctx = _collect(con, days=days, since_iso=since_iso, until_iso=until_iso)
    finally:
        con.close()
    payload = {
        "window": {"days": days, "since": _iso_z(since), "until": _iso_z(window_end)},
        "denominator": {
            "label": _DENOMINATOR_LABEL,
            "count": ctx.total,
            "note": _DENOMINATOR_NOTE,
        },
        "generated_at": _iso_z(now),
        "fields": _build_all_fields(ctx),
    }
    _log.info(
        "rubric_field_stats_built",
        days=days,
        articles=ctx.total,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return payload


def field_stats_cached(days: int) -> dict[str, Any]:
    """TTL 5 分のキャッシュ付き。保存 (PUT /rubric) では invalidate しない。

    統計は「その時々の基準で生成された**過去の**出力」であり、基準を保存した直後に
    変わってはいけない (変わると「保存したら数字が動いた」と誤読される)。
    """
    cached = _STATS_CACHE.get(days)
    if cached is not None and (time.monotonic() - cached[0]) < _STATS_TTL_SEC:
        return cached[1]
    payload = build_field_stats(days)
    _STATS_CACHE[days] = (time.monotonic(), payload)
    return payload


def _connect(db_path: Path | None) -> Any:
    from src.storage.run_history import DEFAULT_DB_PATH

    return connect(db_path if db_path is not None else DEFAULT_DB_PATH)


def _iso_z(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_until(until: datetime | None, now: datetime) -> tuple[datetime, str | None]:
    """(窓の右端, SQL に渡す上限 ISO or None) を返す。None のとき SQL に上限条件を足さない。"""
    if until is None:
        return (now, None)
    aware = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
    return (aware, aware.isoformat())


def _collect(con: Any, *, days: int, since_iso: str, until_iso: str | None = None) -> _Ctx:
    """全クエリを実行して素の数値を集める (組み立ては純粋関数側)。"""
    scalars = _fetch_scalars(con, since_iso, until_iso)
    avg_summary, avg_remediation = _fetch_avg_lengths(con, since_iso, until_iso)
    dist = {
        "importance": _fetch_distribution(con, since_iso, until_iso, "importance"),
        "category": _fetch_distribution(con, since_iso, until_iso, "category"),
        "article_type": _fetch_distribution(con, since_iso, until_iso, "article_type"),
        "editorial_stance": _fetch_distribution(con, since_iso, until_iso, "editorial_stance"),
        "event_date_basis": _fetch_distribution(con, since_iso, until_iso, "event_date_basis"),
        "socio_political_intent": _fetch_distribution(
            con, since_iso, until_iso, "socio_political_intent"
        ),
        "victim_sector_canonical": _fetch_distribution(
            con, since_iso, until_iso, "victim_sector_canonical", COVERAGE_CYBER
        ),
        "victim_country_iso": _fetch_distribution(
            con, since_iso, until_iso, "victim_country_iso", COVERAGE_CYBER
        ),
    }
    kana, sample, truncated = _fetch_title_kana(con, since_iso, until_iso)
    return _Ctx(
        days=days,
        scalars=scalars,
        avg_summary_len=avg_summary,
        avg_remediation_len=avg_remediation,
        dist=dist,
        ent_cyber=_fetch_entity_counts(con, since_iso, until_iso, COVERAGE_CYBER),
        ent_geo=_fetch_entity_counts(con, since_iso, until_iso, COVERAGE_GEOPOLITICAL),
        top_cyber=_fetch_entity_top(
            con, since_iso, until_iso, COVERAGE_CYBER, _TOP_ENTITY_TYPES_CYBER
        ),
        top_geo=_fetch_entity_top(
            con, since_iso, until_iso, COVERAGE_GEOPOLITICAL, _TOP_ENTITY_TYPES_GEO
        ),
        title_kana=kana,
        title_sample=sample,
        title_truncated=truncated,
    )


__all__ = ["MAX_DAYS", "MIN_DAYS", "build_field_stats", "field_stats_cached"]

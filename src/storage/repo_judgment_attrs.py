"""状況総括の「パターン」まとめ用に、判定 (= 状況) ごとの属性を 1 回の SQL で引く (2026-11-17)。

設計: docs/synthesis_pattern_redesign.md §3.1。属性は **評価済み (``assessed_at``) で weak でなく、
期間内に足された** 根拠記事から取る。新しく開いた状況には、まとめ記事が seed で一括紐付けされる
(属性が混ざる) ため、種別が roundup の記事は属性にも事象の根にも使わない。

使う列 (SSoT): ``articles.victim_country_iso`` / ``article_kinds.kind`` /
``article_importance_v2.severity`` (+ ``severity_basis``) /
``event_item_members`` → 根 (``COALESCE(event_items.merged_into, item_id)``、live の事象だけ)。
SQLite (dev / tests) と PostgreSQL の両方で動くよう、DISTINCT ON などは使わず Python 側で畳む。

追加 (2026-11-17): 事象ニュースの見出し (``event_item_versions`` の最新版、事案の題)・手口の手がかり
(``article_entities`` の ``ttp`` = 本文の裏付けを通した ATT&CK 手法 / ``affected_product``)・
状況の最後に根拠が足された時刻 (``situations.last_evidence_at``) も引く (追加の問い合わせは 3 本)。
事象の見出しは ``until`` (= 期間の終わり) 以前に生成された最新版だけ
(窓より後の版の事実を入れない)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_CHUNK = 400
#: 属性に使わない種別 (まとめ記事は複数の独立した事案の束で、属性が混ざる)
EXCLUDED_KIND = "roundup"
_LIVE_ORIGIN = "live"
LARGE_MAGNITUDE_BASIS = "large_magnitude"
TECHNIQUE_ENTITY = "ttp"
PRODUCT_ENTITY = "affected_product"
#: 事案どうしの「つながり」(共通の実体) に使う実体の種類。製品名は偶然の一致が多いので使わない
#: (2026-11-17)。手法 (ttp) と製品は共通の手口の印で別に扱う
LINK_ENTITY_TYPES = ("victim_org", "malware_family", "campaign", "actor", "cve")

_SQL = """
SELECT se.situation_id, se.article_id, ar.victim_country_iso, k.kind, v.severity, v.severity_basis,
       COALESCE(i.merged_into, m.item_id) AS root, i.origin
FROM situation_evidence se
LEFT JOIN articles ar ON ar.article_id = se.article_id
LEFT JOIN article_kinds k ON k.article_id = se.article_id
LEFT JOIN article_importance_v2 v ON v.article_id = se.article_id
LEFT JOIN event_item_members m ON m.article_id = se.article_id
LEFT JOIN event_items i ON i.id = m.item_id
WHERE se.situation_id IN ({ph})
  AND se.assessed_at IS NOT NULL AND se.weak_at IS NULL AND se.added_at >= ? AND se.added_at <= ?
"""  # noqa: S608
_ENTITY_SQL = (
    "SELECT article_id, entity_type, value FROM article_entities "  # noqa: S608
    "WHERE entity_type IN ('ttp', 'affected_product', 'victim_org', 'malware_family',"
    " 'campaign', 'actor', 'cve') AND article_id IN ({ph})"
)
_HEADLINE_SQL = """
SELECT v.item_id, v.headline, v.generated_at FROM event_item_versions v
WHERE v.item_id IN ({ph}) AND v.generated_at <= ?
  AND v.version = (
      SELECT MAX(w.version) FROM event_item_versions w
      WHERE w.item_id = v.item_id AND w.generated_at <= ?
  )
"""  # noqa: S608
_LAST_EVIDENCE_SQL = (
    "SELECT situation_id, last_evidence_at FROM situations "  # noqa: S608
    "WHERE situation_id IN ({ph})"
)


@dataclass(frozen=True)
class EventHeadline:
    """判定の根拠記事が属する事象 (根) と、その事象ニュースの最新の見出し。"""

    root: str
    #: この判定の根拠記事のうち、この事象に属する記事の数 (題の事象を選ぶ。重さには使わない)
    articles: int
    generated_at: str
    headline: str


@dataclass(frozen=True)
class JudgmentAttrs:
    """1 判定の属性 (根拠記事の属性の和集合)。空の集合 = 属性なし。"""

    countries: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    severities: frozenset[str] = frozenset()
    event_roots: frozenset[str] = frozenset()
    large_magnitude: bool = False
    techniques: frozenset[str] = frozenset()
    products: frozenset[str] = frozenset()
    #: 共通の実体の鍵 ("種類:値"、種類は ``LINK_ENTITY_TYPES``)
    links: frozenset[str] = frozenset()
    event_headlines: tuple[EventHeadline, ...] = ()
    last_evidence_at: str = ""


def since_iso(ts: datetime) -> str:
    """``situation_evidence.added_at`` (テキストの ISO) と文字列で比べる境界 (UTC・マイクロ秒)。

    新しい状況の根拠は総括の実行時刻 (= ``period_end``) ちょうどに足されるので、上限は秒に
    丸めず、小数部まで含めて比べる (丸めると同じ秒の ``…:02.538+00:00`` が落ちる)。
    """
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass
class _Acc:
    sets: dict[str, set[str]]
    articles: set[str]
    root_articles: dict[str, set[str]]
    large_magnitude: bool = False


def _fold_main(rows: Sequence[Any]) -> dict[str, _Acc]:
    acc: dict[str, _Acc] = {}
    for r in rows:
        sid, article, country, kind, severity, basis, root, origin = tuple(r)
        if kind == EXCLUDED_KIND:
            continue
        a = acc.setdefault(str(sid), _Acc(defaultdict(set), set(), defaultdict(set)))
        a.articles.add(str(article))
        for name, value in (("countries", country), ("kinds", kind), ("severities", severity)):
            if value:
                a.sets[name].add(str(value))
        a.large_magnitude = a.large_magnitude or basis == LARGE_MAGNITUDE_BASIS
        if root and origin == _LIVE_ORIGIN:
            a.sets["event_roots"].add(str(root))
            a.root_articles[str(root)].add(str(article))
    return acc


def _chunked(
    conn: Any, template: str, values: Iterable[str], tail: tuple[Any, ...] = ()
) -> list[Any]:
    ids = list(dict.fromkeys(values))
    rows: list[Any] = []
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        sql = template.format(ph=",".join("?" * len(chunk)))
        rows.extend(conn.execute(sql, (*chunk, *tail)).fetchall())
    return rows


def _entities_by_article(rows: Sequence[Any]) -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for article, etype, value in (tuple(r) for r in rows):
        if value:
            out[str(article)][str(etype)].add(str(value))
    return out


def _headlines(rows: Sequence[Any]) -> dict[str, tuple[str, str]]:
    return {str(i): (str(h), str(g)) for i, h, g in (tuple(r) for r in rows) if h}


def _attrs_of(
    a: _Acc,
    entities: Mapping[str, Mapping[str, set[str]]],
    headlines: Mapping[str, tuple[str, str]],
    last_evidence: str,
) -> JudgmentAttrs:
    techniques = {v for art in a.articles for v in entities.get(art, {}).get(TECHNIQUE_ENTITY, ())}
    products = {v for art in a.articles for v in entities.get(art, {}).get(PRODUCT_ENTITY, ())}
    links = {
        f"{t}:{v}"
        for art in a.articles
        for t in LINK_ENTITY_TYPES
        for v in entities.get(art, {}).get(t, ())
    }
    event_headlines = tuple(
        EventHeadline(root, len(arts), headlines[root][1], headlines[root][0])
        for root, arts in sorted(a.root_articles.items())
        if root in headlines
    )
    return JudgmentAttrs(
        **{k: frozenset(v) for k, v in a.sets.items()},
        large_magnitude=a.large_magnitude,
        techniques=frozenset(techniques),
        products=frozenset(products),
        links=frozenset(links),
        event_headlines=event_headlines,
        last_evidence_at=last_evidence,
    )


class JudgmentAttrsMixin:
    """``RunHistoryRepository`` に混ぜる読み取り。"""

    def get_judgment_attrs(
        self: Any, situation_ids: Sequence[str], *, since: datetime, until: datetime
    ) -> dict[str, JudgmentAttrs]:
        """状況 id → 属性 (``since`` 以上 ``until`` 以下に足された根拠から)。

        根拠が 1 件も無い状況も、最後に根拠が足された時刻があれば属性を返す (継続中の一覧用)。
        """
        ids = list(dict.fromkeys(situation_ids))
        with self._connect() as conn:
            acc = _fold_main(_chunked(conn, _SQL, ids, (since_iso(since), since_iso(until))))
            articles = {x for a in acc.values() for x in a.articles}
            roots = {r for a in acc.values() for r in a.root_articles}
            entities = _entities_by_article(_chunked(conn, _ENTITY_SQL, articles))
            upper = since_iso(until)
            headlines = _headlines(_chunked(conn, _HEADLINE_SQL, roots, (upper, upper)))
            last = {
                str(sid): str(at or "")
                for sid, at in (tuple(r) for r in _chunked(conn, _LAST_EVIDENCE_SQL, ids))
            }
        empty = _Acc(defaultdict(set), set(), defaultdict(set))
        return {
            sid: attrs
            for sid in ids
            if (attrs := _attrs_of(acc.get(sid, empty), entities, headlines, last.get(sid, "")))
            != JudgmentAttrs()
        }

"""ArticleFacts — signal-first PIR 照合が読む「記事の意味的事実」の read-model。

取込 LLM (summarizer) が付与した強い意味メタデータ (category / intent / victim_sector /
victim_country / is_ransomware) を正規化して保持する。PIR の述語 (signal_match) はこの
read-model だけを見る。生キーワード substring 照合 (evaluator._row_match_signals) の
置換先 (docs/pir_signal_first_matching_design.md §3.1)。

PoC (Phase 1) では ``from_db_row`` のみ実装する。routing の RoutingSignals からの
constructor (``from_routing_signals``) と PROPERTY_CATALOG の一元化は Phase 3。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _get(row: Any, key: str) -> Any:  # noqa: ANN401 — row は sqlite3.Row / PG row / dict-like
    """欠損列でも例外を出さず取得 (sqlite3.Row は欠損で IndexError)。"""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _norm(value: Any) -> str:  # noqa: ANN401
    """str 値を「小文字・trim」に正規化 (述語値も小文字で照合するため)。"""
    return str(value).strip().lower() if value is not None else ""


def _as_bool(value: Any) -> bool:  # noqa: ANN401
    """is_ransomware は DB で smallint(0/1)。bool / '0'/'1' / 'true' も許容して bool 化。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes")
    return False


def _build_text(row: Any) -> str:  # noqa: ANN401
    """title + summary + body を結合した小文字の検索テキスト (keyword_any 述語用)。"""
    parts = [str(_get(row, k) or "") for k in ("title", "summary", "body")]
    return " ".join(p for p in parts if p).lower()


@dataclass(frozen=True)
class ArticleFacts:
    """1 記事の意味的事実 (PIR 述語の評価対象)。値はすべて小文字正規化済み。"""

    category: str = ""
    victim_sector: str = ""
    victim_country: str = ""
    intent: str = ""
    is_ransomware: bool = False
    # フィード名 (完全名、小文字)。feed_title は "CISA Advisories" のような完全名なので
    # 述語は exact でなく contains_any (substring) で照合する (signal_match)。
    feed_title: str = ""
    # title+summary+body の検索テキスト (小文字)。keyword_any 述語 (語境界照合) 用。
    # keyword は意味 clause と AND した弱補強に限る (単独 sole-match は設計上避ける)。
    text: str = ""
    # title のみの検索テキスト (小文字)。title スコープの keyword_any 用 —
    # body 言及の氾濫 (「制裁」「日本」が地政学記事の本文に頻出) を避け、
    # 「タイトルに載る = 主題の決定論近似」で照合する (recall 監査 2026-07-23)。
    title_text: str = ""
    # 主題アクター層 (authoring 統一 2026-07-23、actor/actor_nation leaf 用)。
    # subject_source 非空 = 主題評価済み行 (id 照合)、空 = legacy 行 (text/entity 照合)。
    subject_source: str = ""
    subject_ids: frozenset[str] = frozenset()
    # article_entities (entity_type=actor) の値 (小文字)。legacy 照合と nation 解決に使う。
    actor_values: frozenset[str] = frozenset()

    @classmethod
    def from_db_row(
        cls,
        row: Any,  # noqa: ANN401
        text: str | None = None,
        actor_values: set[str] | frozenset[str] | None = None,
    ) -> ArticleFacts:
        """articles 行 (posted rows) から ArticleFacts を構築する。

        必要列: category / socio_political_intent / victim_sector_canonical /
        victim_country_iso / is_ransomware / feed_title / title / summary / body /
        subject_actor_ids / subject_actor_source (evaluator._load_posted_rows の SELECT が供給)。
        ``text`` は batch 評価で使い回すため呼び出し側が渡せる (None なら行から組み立て)。
        ``actor_values`` = article_entities (actor) の値 (evaluator の actor_map 由来、
        actor/actor_nation leaf の legacy 照合に使う。無ければ空 = leaf が不成立に倒れる)。
        """
        raw_subject = str(_get(row, "subject_actor_ids") or "")
        subject_ids = frozenset(s.strip().lower() for s in raw_subject.split(",") if s.strip())
        return cls(
            category=_norm(_get(row, "category")),
            victim_sector=_norm(_get(row, "victim_sector_canonical")),
            victim_country=_norm(_get(row, "victim_country_iso")),
            intent=_norm(_get(row, "socio_political_intent")),
            is_ransomware=_as_bool(_get(row, "is_ransomware")),
            feed_title=_norm(_get(row, "feed_title")),
            text=text if text is not None else _build_text(row),
            title_text=str(_get(row, "title") or "").lower(),
            subject_source=_norm(_get(row, "subject_actor_source")),
            subject_ids=subject_ids,
            actor_values=frozenset(v.lower() for v in (actor_values or frozenset())),
        )

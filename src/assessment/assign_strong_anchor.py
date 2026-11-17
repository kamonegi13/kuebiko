"""サイバー事案の情勢への「国・話題語だけ」の割当を落とす (``LEDGER_ASSIGN_STRONG_ANCHOR``)。

docs/ledger_upstream_fixes_2026_11_17.md §3。``assignment._score`` の ``nation`` / ``token`` 規則は
強い手がかり (cve / 被害組織 / アクター等) の共通なしに記事を情勢へ付ける。サイバー事案の情勢では
標本の大半が無関係だったため、強い手がかりの共通を必須にする。地政学・軍事・政治等の情勢は
強い手がかりが無いので従来どおり (対象外)。

旗: 0 = 何もしない / shadow = 落とすはずの割当を log (``ledger_assign_strong_anchor_shadow``) して
従来どおり割当 / 1 = 落とす。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from src.assessment.assignment import ArticleKeys, SituationKeys
from src.assessment.ledger_flags import assign_strong_anchor_mode
from src.assessment.name_relevance import article_names, is_relevant
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.assessment.situation_store import SituationStore

_log = get_logger(__name__)

#: サイバー事案とみなす situations.domain (これ以外の地政学・軍事・政治・経済等は対象外)
CYBER_DOMAINS: frozenset[str] = frozenset({"cyber_incident", "tech", "infrastructure"})
#: 強い手がかり無しで付きうる規則 (anchor 規則は強い手がかりの共通を要求するので対象外)
_WEAK_RULES: frozenset[str] = frozenset({"nation", "token"})


def should_drop_assignment(
    *,
    rule: str,
    domain: str,
    shared_strong: Collection[str],
    cyber_domains: Collection[str] = CYBER_DOMAINS,
) -> bool:
    """この割当を落とすか (純粋関数)。サイバー事案 ∧ nation/token 規則 ∧ 強い手がかりの共通なし。"""
    return domain in cyber_domains and rule in _WEAK_RULES and not shared_strong


def strong_anchor_blocks(
    matched: tuple[SituationKeys, str] | None,
    art: ArticleKeys,
    *,
    entity_keys: Iterable[str] = (),
    store: SituationStore | None = None,
    ml_prob: float | None = None,
) -> bool:
    """``match_situation`` の結果を旗に従って落とすか。shadow は log だけして False を返す。

    強い手がかりの共有が無くても、記事の固有の名前 (entity + 題の固有語、漢字名を含む) が
    状況の題・最新の判定文に出れば関係ありとして落とさない (§8、2 回目)。
    """
    mode = assign_strong_anchor_mode()
    if mode == "off" or matched is None:
        return False
    sit, rule = matched
    if not should_drop_assignment(
        rule=rule, domain=sit.row.domain, shared_strong=art.strong & sit.strong
    ):
        return False
    if _is_relevant_to_situation(art, sit, entity_keys, store):
        return False
    _log.info(
        "ledger_assign_strong_anchor_shadow",
        mode=mode,
        situation_id=sit.row.situation_id,
        article_id=art.article_id,
        rule=rule,
        ml_prob=None if ml_prob is None else round(ml_prob, 3),
    )
    return mode == "on"


def _is_relevant_to_situation(
    art: ArticleKeys,
    sit: SituationKeys,
    entity_keys: Iterable[str],
    store: SituationStore | None,
) -> bool:
    """記事の固有の名前が状況の題・最新の判定文に出るか (判定文は必要になった時だけ引く)。"""
    names = article_names(art.title, [*entity_keys, *art.strong])
    if is_relevant(names, sit.row.title):
        return True
    if store is None:
        return False
    rev = store.latest_revision(sit.row.situation_id)
    return rev is not None and is_relevant(names, rev.claim)

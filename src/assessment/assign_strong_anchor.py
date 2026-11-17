"""サイバー事案の情勢への「国・話題語だけ」の割当を落とす (``LEDGER_ASSIGN_STRONG_ANCHOR``)。

docs/ledger_upstream_fixes_2026_11_17.md §3。``assignment._score`` の ``nation`` / ``token`` 規則は
強い手がかり (cve / 被害組織 / アクター等) の共通なしに記事を情勢へ付ける。サイバー事案の情勢では
標本の大半が無関係だったため、強い手がかりの共通を必須にする。地政学・軍事・政治等の情勢は
強い手がかりが無いので従来どおり (対象外)。

旗: 0 = 何もしない / shadow = 落とすはずの割当を log (``ledger_assign_strong_anchor_shadow``) して
従来どおり割当 / 1 = 落とす。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import TYPE_CHECKING

from src.assessment.assignment import ArticleKeys, SituationKeys
from src.assessment.ledger_flags import assign_strong_anchor_mode
from src.assessment.name_relevance import article_names, is_relevant
from src.logging_config import get_logger
from src.synthesis.grounded.hypotheses import is_cyber_domain

if TYPE_CHECKING:
    from src.assessment.situation_store import SituationStore

_log = get_logger(__name__)

#: 強い手がかり無しで付きうる規則 (anchor 規則は強い手がかりの共通を要求するので対象外)
_WEAK_RULES: frozenset[str] = frozenset({"nation", "token"})


def should_drop_assignment(
    *,
    rule: str,
    domain: str,
    shared_strong: Collection[str],
) -> bool:
    """この割当を落とすか (純粋関数)。サイバー事案 ∧ nation/token 規則 ∧ 強い手がかりの共通なし。

    サイバー事案の判定は ``hypotheses.is_cyber_domain`` (SSoT)。
    """
    return is_cyber_domain(domain) and rule in _WEAK_RULES and not shared_strong


class StrongAnchorRun:
    """1 run の状態: 判定文の cache (N+1 回避)・関門後に log する保留・log 済みの組。"""

    def __init__(
        self,
        store: SituationStore | None = None,
        claim_by_sid: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._claims: dict[str, str] = dict(claim_by_sid or {})
        self._pending: dict[tuple[str, str], str] = {}
        self._logged: set[tuple[str, str]] = set()

    def claim(self, situation_id: str) -> str:
        if situation_id not in self._claims:
            rev = self._store.latest_revision(situation_id) if self._store is not None else None
            self._claims[situation_id] = rev.claim if rev is not None else ""
        return self._claims[situation_id]

    def record_shadow(self, situation_id: str, article_id: str, rule: str, *, defer: bool) -> None:
        key = (situation_id, article_id)
        if key in self._logged:
            return
        if defer:
            self._pending[key] = rule
            return
        self._emit(key, rule, None)

    def flush_shadow(
        self,
        kept_by_sid: Mapping[str, Collection[str]],
        ml_prob: Mapping[tuple[str, str], float | None] | None = None,
    ) -> None:
        """割当の関門を通って実際に残る割当だけを log する (落ちた割当は数えない)。"""
        for key, rule in list(self._pending.items()):
            sid, aid = key
            if aid in kept_by_sid.get(sid, ()):
                self._emit(key, rule, (ml_prob or {}).get(key))
        self._pending.clear()

    def _emit(self, key: tuple[str, str], rule: str, ml_prob: float | None) -> None:
        self._logged.add(key)
        _log.info(
            "ledger_assign_strong_anchor_shadow",
            mode="shadow",
            situation_id=key[0],
            article_id=key[1],
            rule=rule,
            ml_prob=None if ml_prob is None else round(ml_prob, 3),
        )


def strong_anchor_blocks(
    matched: tuple[SituationKeys, str] | None,
    art: ArticleKeys,
    *,
    entity_keys: Iterable[str] = (),
    store: SituationStore | None = None,
    run: StrongAnchorRun | None = None,
    defer_shadow_log: bool = False,
) -> bool:
    """``match_situation`` の結果を旗に従って落とすか。shadow は記録だけして False を返す。

    強い手がかりの共有が無くても、記事の固有の名前 (entity + 題の固有語、漢字名を含む) が
    状況の題・最新の判定文に出れば関係ありとして落とさない (§8、2 回目)。
    ``defer_shadow_log=True`` なら shadow の log は ``run.flush_shadow`` まで保留する。
    どの旗でも失敗は握り、従来どおり (落とさない) に倒す。
    """
    mode = assign_strong_anchor_mode()
    if mode == "off" or matched is None:
        return False
    run = run if run is not None else StrongAnchorRun(store)
    sit, rule = matched
    try:
        if not should_drop_assignment(
            rule=rule, domain=sit.row.domain, shared_strong=art.strong & sit.strong
        ):
            return False
        if _is_relevant_to_situation(art, sit, entity_keys, run):
            return False
    except Exception as e:  # noqa: BLE001 — 旗の判定の失敗で台帳の run を止めない
        _log.warning("ledger_assign_strong_anchor_shadow_failed", error=type(e).__name__, mode=mode)
        return False
    if mode == "on":
        _log.info(
            "ledger_assign_strong_anchor_dropped",
            situation_id=sit.row.situation_id,
            article_id=art.article_id,
            rule=rule,
        )
        return True
    run.record_shadow(sit.row.situation_id, art.article_id, rule, defer=defer_shadow_log)
    return False


def _is_relevant_to_situation(
    art: ArticleKeys,
    sit: SituationKeys,
    entity_keys: Iterable[str],
    run: StrongAnchorRun,
) -> bool:
    """記事の固有の名前が状況の題・最新の判定文に出るか (判定文は必要になった時だけ引く)。"""
    names = article_names(art.title, [*entity_keys, *art.strong])
    if is_relevant(names, sit.row.title):
        return True
    return is_relevant(names, run.claim(sit.row.situation_id))

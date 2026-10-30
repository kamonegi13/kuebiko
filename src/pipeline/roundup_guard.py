"""まとめ記事は投稿直前の重複判定の対象にしない (2026-10-02)。

規則の重複判定 (CVE / 内容 / 被害組織) は、まとめ記事を中の 1 話題の記事の重複として
落としていた (「ThreatsDay … 13 本」が Zammad の 1 本の重複に)。逆に、まとめ記事を先に
取り込むと、その中の話題を扱う個別記事が重複にされる。どちらの向きも同じ内容ではない。

種別は記事の種別分類 (event_kind、article_kinds にキャッシュ) を使う。分類は重複判定に
当たった記事にだけ行う (1 日 数十件)。分類に失敗したら免除しない (従来どおり重複)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.eventnews.models import ROUNDUP_KIND
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

#: (title, summary) → 種別
Classify = Callable[[str, str], Awaitable[str]]


#: 分類器がモデル名を持たないとき (テストの代役等) の印
UNKNOWN_MODEL_LABEL = "unknown"


def model_label_of(classify: Classify) -> str:
    """分類器が実際に使ったモデル名 (``KindClassifier.model``)。取れなければ ``unknown``。"""
    return str(getattr(classify, "model", "") or UNKNOWN_MODEL_LABEL)


async def _kind_of(
    repo: RunHistoryRepository,
    classify: Classify,
    article_id: str,
    title: str,
    summary: str,
    cached: dict[str, str],
) -> str:
    if article_id in cached:
        return cached[article_id]
    kind = await classify(title, summary)
    # 実際に分類したモデル名を残す (P0-23)。過去の "roundup_guard" はモデル不明の印として残す
    repo.set_article_kind(article_id, kind, model_label_of(classify))
    return kind


async def involves_roundup(
    repo: RunHistoryRepository,
    classify: Classify,
    *,
    article_id: str,
    title: str,
    summary: str,
    prior_article_id: str | None,
) -> bool:
    """当該記事か一致先のどちらかがまとめ記事なら True (= 重複として扱わない)。"""
    ids = [article_id] + ([prior_article_id] if prior_article_id else [])
    try:
        cached = repo.get_article_kinds(ids)
        if await _kind_of(repo, classify, article_id, title, summary, cached) == ROUNDUP_KIND:
            return True
        if not prior_article_id:
            return False
        prior = repo.get_articles_by_ids([prior_article_id]).get(prior_article_id)
        if prior is None:
            return False
        prior_kind = await _kind_of(
            repo, classify, prior_article_id, prior.title, prior.summary or "", cached
        )
        return prior_kind == ROUNDUP_KIND
    except Exception as e:  # noqa: BLE001 — 判定できないものは免除しない (従来どおり重複)
        _log.warning("roundup_guard_failed", article_id=article_id, error=str(e)[:200])
        return False


class KindClassifier:
    """本番の分類器 (Step.EVENT_KIND のモデル)。LLM は最初に使うときに作る。

    作れなければ呼び出しで例外 → ``involves_roundup`` が「免除しない」に倒す。
    ``model`` は分類に使った実モデル名 (まだ作っていなければ空)。
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._llm: Any = None

    @property
    def model(self) -> str:
        return str(getattr(self._llm, "model", "") or "")

    async def __call__(self, title: str, summary: str) -> str:
        from src.eventnews import event_kind
        from src.tools.model_tiers import Step, build_llm_for

        if self._llm is None:
            self._llm = build_llm_for(Step.EVENT_KIND, self._config)
        return await event_kind.classify(self._llm, title, summary)


def kind_classifier(config: Any) -> Classify:
    """本番の分類器を返す (``KindClassifier``)。"""
    return KindClassifier(config)


__all__ = [
    "Classify",
    "KindClassifier",
    "involves_roundup",
    "kind_classifier",
    "model_label_of",
]

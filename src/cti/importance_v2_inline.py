"""M3b 段1: routing 時点 (``_build_briefing``) で ``ImportanceV2`` を即座に導く (2026-10-10)。

本番の投稿先は変えない — ``src/cti/routing_v2.py`` の ``M3B_RULES`` を使った shadow 記録
(``routing_v2_shadow``) 専用の材料を作る。

深刻度の軸 (``src/cti/severity_axes.py``) はこの段で LLM を呼んで先取りする (毎時の段
``src/ui/services/severity_axes_job.py`` の前倒し)。対象は毎時の段と同じ importance 帯
(high/medium、posted 記事のみが対象という条件は routing 時点では未確定だが、
importance 自体は毎時の段の絞り込みと同一の帯を使う) に加え、**深刻さを導出しうる
カテゴリだけに絞る** — ``importance_v2.derive_severity()`` は非サイバー categoryは
常に ``severity=None`` ("non_cyber") を返すため、そのカテゴリで LLM を呼ぶのは無駄になる。
毎時の段 (``severity_axes_job.py``) 自体はカテゴリで絞っていない (posted + importance
high/medium のみ) が、ここでの追加のカテゴリ絞りは inline 版だけが支払う LLM コストを
減らすための最適化であり、対象記事の集合を変えるものではない (非サイバーの記事は
どのみち軸があっても severity=None になるので、shadow の結果は変わらない)。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from src.cti.importance_v2 import ImportanceV2
    from src.tools.discord_publisher import BriefingMessage
    from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_INLINE_AXES_FLAG = "ROUTING_V2_INLINE_AXES"
_ELIGIBLE_IMPORTANCE: frozenset[str] = frozenset({"high", "medium"})


def inline_axes_enabled() -> bool:
    """``ROUTING_V2_INLINE_AXES=0`` で停止 (既定 on)。"""
    return os.environ.get(_INLINE_AXES_FLAG, "1") != "0"


def axes_eligible(importance: str, category: str) -> bool:
    """inline で深刻度の軸 LLM を呼ぶ対象か (importance 帯 + 深刻さを導出しうる category)。"""
    from src.cti.importance_v2 import NON_CYBER_CATEGORIES

    return importance in _ELIGIBLE_IMPORTANCE and category not in NON_CYBER_CATEGORIES


def _axes_llm() -> LLMClient:
    """毎時の段 (severity_axes_job) と同じ Step で client を組む。

    要約用の client を流用しない — 課題の接頭辞 (task_prefix の ``[task: axes]``) と
    step 単位のモデル割当が Step.SEVERITY_AXES に紐づいているため、流用すると学習時と
    違う形のプロンプトが届く。
    """
    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for

    return build_llm_for(Step.SEVERITY_AXES, load_app_config())


async def classify_axes_inline(
    title: str,
    summary: str,
    *,
    importance: str,
    category: str,
    body: str = "",
    published: str | None = None,
) -> tuple[dict[str, str] | None, str]:
    """eligible なら深刻度の軸を 1 回 LLM で取る。不適格・失敗は ``(None, "")``。

    ``classify_axes`` 自体が失敗を飲み込んで ``None`` を返す (fail-safe) ので、本処理は
    本パイプラインを止めない。``model_dump()`` 呼び出しも defensively 包む —
    ``classify_axes`` は型どおり ``SeverityAxes | None`` を返すが、本処理は本番の
    LLM 呼び出し経路の末端 (本パイプラインを絶対に止めない) なので、想定外の戻り値
    (モック等) でも例外を外に出さない。
    """
    if not inline_axes_enabled() or not axes_eligible(importance, category):
        return None, ""
    from src.cti.severity_axes import classify_axes

    try:
        llm = _axes_llm()
    except Exception as e:  # noqa: BLE001 — client を組めなくても本処理を止めない
        _log.warning("routing_v2_inline_axes_client_failed", error=type(e).__name__)
        return None, ""
    axes = await classify_axes(llm, title, summary, body, published)
    if axes is None:
        return None, ""
    try:
        return axes.model_dump(), str(getattr(llm, "model", ""))
    except Exception as e:  # noqa: BLE001 — 想定外の戻り値で本処理を止めない
        _log.warning("routing_v2_inline_axes_dump_failed", error=type(e).__name__)
        return None, ""


def build_importance_v2_inline(
    *,
    msg: BriefingMessage,
    body_text: str,
    feed_title: str,
    severity_axes: Mapping[str, str] | None,
) -> ImportanceV2:
    """route() 直前の ``BriefingMessage`` から ``ImportanceV2`` を導く (shadow 専用)。

    材料はすべて ``_build_briefing`` が既に組み立てた metadata / 本文から取る
    (新規抽出はしない)。SIR (旧 PIR) の該当は規則ベース (``subject_actor_ids=None``、
    ``src/pipeline/persistence.rule_based_sir_ids`` と共有) で評価する — judgment 分類器の
    主題アクター判定はこの時点ではまだ articles 行として確定していない。
    """
    from src.cti.importance_v2 import build_inputs, derive
    from src.cti.mention_tagger import derive_mention_entities
    from src.cti.routing_signals import _article_cves
    from src.pipeline.persistence import rule_based_sir_ids
    from src.tools.kev_client import get_kev_cve_set
    from src.tools.nvd_client import max_cvss as nvd_max_cvss

    title = msg.title or ""
    summary_text = msg.summary or ""
    full_text = f"{title}\n{summary_text}\n{body_text or ''}"
    category = str(msg.category or "")
    article_type = str(msg.metadata.get("article_type") or "")
    victim_country = str(msg.metadata.get("victim_country_iso") or "")

    involved_raw = msg.metadata.get("involved_country_isos")
    involved = [str(c) for c in involved_raw] if isinstance(involved_raw, list) else []

    mention_entities = derive_mention_entities(
        title=title, summary=summary_text, involved_isos={c.lower() for c in involved}
    )
    mentioned = [v for (etype, v) in mention_entities if etype == "mentioned_country"]

    detected_raw = msg.metadata.get("detected_actor_ids")
    actor_values = (
        {str(a).strip().lower() for a in detected_raw if str(a).strip()}
        if isinstance(detected_raw, list)
        else set()
    )

    article_data: dict[str, Any] = {
        "title": title,
        "summary": summary_text,
        "body": body_text or "",
        "feed_title": feed_title,
        "victim_sector_canonical": msg.metadata.get("victim_sector_canonical"),
        "victim_country_iso": msg.metadata.get("victim_country_iso"),
        "article_type": article_type,
        "category": category,
        "socio_political_intent": msg.metadata.get("socio_political_intent"),
        "is_ransomware": bool(msg.metadata.get("is_ransomware")),
        # 主題アクター層は routing 時点では未確定 (articles 永続化後に determine_subject_actors
        # が決める)。None = legacy keyword 照合にフォールバック (evaluator の既定動作)。
        "subject_actor_ids": None,
        "subject_actor_source": None,
    }
    sir_ids: frozenset[str]
    try:
        sir_ids = rule_based_sir_ids(article_data, actor_values)
    except Exception as e:  # noqa: BLE001 — SIR 評価の失敗で shadow 計算を止めない
        _log.debug("importance_v2_inline_sir_failed", error=str(e))
        sir_ids = frozenset()

    cves = _article_cves(msg, full_text)
    kev = get_kev_cve_set()

    inputs = build_inputs(
        category=category,
        article_type=article_type,
        axes=severity_axes,
        cves=cves,
        kev=kev,
        year=datetime.now(UTC).year,
        victim_country=victim_country,
        involved_countries=involved,
        mentioned_countries=mentioned,
        sir_ids=sir_ids,
        text=full_text,
        stored_max_cvss=nvd_max_cvss(list(cves)),
        title=title,
    )
    return derive(inputs)


__all__ = [
    "axes_eligible",
    "build_importance_v2_inline",
    "classify_axes_inline",
    "inline_axes_enabled",
]

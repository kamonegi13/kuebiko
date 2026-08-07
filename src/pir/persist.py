"""PIR 適合の永続化 (PIR-persist, 2026-06-20)。

これまで article×PIR の match は query 時に ``evaluate_pir_matches`` で都度計算していた
(KPI / flow / daily-focus)。PIR-persist はそれを **タグとして永続化** (entity_type='pir',
value=pir_id) し、PIR 中心の retrieval / 地図の PIR 次元 / KPI 即時化を解禁する。

PIR は可変 (describe-then-compile) なので、ingest 時固定でなく **日次 full rebuild**
(pir-daily-focus に相乗り) で eventual consistency を保つ。窓内 posted を全 enabled PIR に
1 パス適用し、entity_type='pir' を atomic に置換する。
"""

from __future__ import annotations

from src.logging_config import get_logger
from src.pir.evaluator import (
    _fetch_llm_confirmed_map,
    _llm_judge_required,
    _load_posted_rows,
    has_match_criteria,
    pir_match_signals,
)
from src.pir.integration import get_pir_config
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

# rebuild が対象にする窓 (地図/KPI は ≤90d なので 90d を rolling rebuild)。
DEFAULT_WINDOW_DAYS = 90
_LIMIT = 20000


def rebuild_pir_entities(
    *,
    repo: RunHistoryRepository | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = _LIMIT,
    dry_run: bool = False,
) -> dict[str, int]:
    """窓内 posted 記事 × 全 enabled PIR を再評価し entity_type='pir' を置換する。

    PIR 編集で消えた match も反映するため **full replace** (既存 pir entity を全削除 → 再投入)。
    ``dry_run=True`` は計算のみで書き込まない。返り値 = {pirs, articles_scanned,
    articles_matched, pir_links}。
    """
    # 運用 SSoT は DB (config_store)。yaml 直読み (load_pir_config) では UI/DB 編集 +
    # signal-first の match ツリーが反映されないため get_pir_config (DB 正、失敗時 yaml
    # fallback) を使う。
    repo_inst = repo or RunHistoryRepository()
    cfg = get_pir_config()
    pirs = [p for p in cfg.priorities if p.enabled and has_match_criteria(p)]
    rows, actor_map = _load_posted_rows(
        repo=repo_inst, lookback_hours=window_days * 24, limit=limit
    )
    confirmed_map = (
        _fetch_llm_confirmed_map(repo_inst, None)
        if any(_llm_judge_required(p) for p in pirs)
        else {}
    )

    by_article: dict[str, set[str]] = {}
    for pir in pirs:
        for r in rows:
            aid = str(r["article_id"])
            actor_values = actor_map.get(r["article_id"], set())
            if pir_match_signals(pir, r, actor_values, llm_confirmed=confirmed_map.get(aid)):
                by_article.setdefault(aid, set()).add(pir.id)

    links = sum(len(v) for v in by_article.values())
    if not dry_run:
        # full replace: 既存 pir entity を全削除してから再投入 (idempotent な日次 rebuild)。
        with repo_inst._connect() as conn:  # noqa: SLF001 (intra-package)
            conn.execute("DELETE FROM article_entities WHERE entity_type='pir'")
        for aid, pir_ids in by_article.items():
            repo_inst.add_article_entities(aid, [("pir", pid) for pid in sorted(pir_ids)])

    result = {
        "pirs": len(pirs),
        "articles_scanned": len(rows),
        "articles_matched": len(by_article),
        "pir_links": links,
    }
    _log.info("pir_entities_rebuilt", dry_run=dry_run, **result)
    return result

"""概念 PIR の LLM 主題判定バッチ (docs/pir_concept_llm_judge_design.md §4)。

決定論 (述語 / keyword) では表現できない「言及≠主題」「概念判定」を、
候補ゲート (match ツリー) 通過分にのみ走る focused judge で確定する。

- verdict は正負とも ``pir_llm_judgments`` に永続化 (再判定防止)。消費側
  (evaluator / rebuild) は保存済み verdict を読むだけ = 決定論のまま。
- pir_rev (title+description+question ハッシュ) が変わった行は stale として
  再判定される — PIR の description 編集が判定基準の編集になる
  (PIR is canonical intent)。
- 実行形態は夜間バッチ (pir-entity-rebuild の前段) + 毎時 incremental
  (pir-judge-hourly、新着候補のみ小 cap + entity タグ即時反映) + 手動 backfill script。
  triage / summarizer には責務を足さない (focused 分類器の教訓、監査 2026-07-13)。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger
from src.pir.article_facts import ArticleFacts
from src.pir.evaluator import _llm_judge_enabled, _load_posted_rows
from src.pir.models import Pir
from src.pir.signal_match import evaluate_match
from src.storage.run_history import RunHistoryRepository

if TYPE_CHECKING:
    from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

DEFAULT_WINDOW_DAYS = 90
_ROW_LIMIT = 20000
# 夜間 job (max_runtime 15 分) に収まる 1 run あたりの判定上限。定常流入は
# 候補ゲート通過 ~10-30 件/日なので余裕がある。backfill は script が引き上げる。
DEFAULT_MAX_JUDGMENTS = 80
# 毎時 incremental (pir-judge-hourly) の cap。平常時の毎時流入は 1-3 件なので
# 十分な余裕。stale 再判定 (PIR 編集で最大 ~1000 件) は日中に流さず夜間に残す。
HOURLY_MAX_JUDGMENTS = 20
DEFAULT_CONCURRENCY = 2
_BODY_EXCERPT_CHARS = 2000
# LLM 全断時に候補全件へ無駄打ちしないための連続エラー打ち切り閾値。
_ABORT_AFTER_CONSECUTIVE_ERRORS = 5


class JudgeVerdict(BaseModel):
    """1 記事 × 1 PIR の主題判定。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    matched: bool = False
    reason: str = Field(default="", max_length=200)


def pir_judge_rev(pir: Pir) -> str:
    """判定基準の版ハッシュ。title / description / question のどれを編集しても変わる。"""
    basis = "\n".join([pir.title or "", pir.description or "", pir.llm_judge.question or ""])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def build_judge_prompt(pir: Pir, row: Any) -> str:  # noqa: ANN401 — row は Row / dict-like
    """主題判定 prompt。「言及≠主題」の区別を明示する (①型の是正が核心)。

    「迷ったら不適合」の指示は置かない — 過保守指示は coverage を崩壊させる
    (intent 修復の教訓 2026-07-12)。主題/言及の定義だけを与える。
    """
    question = (pir.llm_judge.question or "").strip()
    criteria = f"判定基準: {question}\n" if question else ""
    title = str(row["title"] or "").strip()
    summary = str(row["summary"] or "").strip()
    body = str(row["body"] or "").strip()[:_BODY_EXCERPT_CHARS]
    return (
        "あなたは日本の CTI アナリストです。"
        "記事が、次の情報要求 (PIR) の関心事を**主題**として扱っているか判定してください。\n\n"
        "## 情報要求 (PIR)\n"
        f"{pir.title}\n"
        f"{(pir.description or '').strip()}\n"
        f"{criteria}\n"
        "## 記事\n"
        f"タイトル: {title}\n"
        f"概要: {summary}\n"
        f"本文抜粋: {body}\n\n"
        "## 判定ルール\n"
        "- 記事の主題 (中心的に扱う事象) がこの PIR の関心事なら matched=true。\n"
        "- 過去事例としての例示・他事象との比較・背景説明としての言及だけなら matched=false "
        "(語が出てくることと主題であることは別)。\n"
        "- reason: 30 字以内で判定理由。\n"
    )


def _judge_targets_for_pir(
    pir: Pir,
    rows: list[Any],
    state: dict[str, str],
    actor_map: dict[str, set[str]] | None = None,
    *,
    include_stale: bool = True,
) -> tuple[int, list[Any]]:
    """候補ゲート通過 rows と、うち未判定/stale の判定対象を返す。

    返り値 = (candidates 総数, 判定対象 rows)。rows は新しい順 (created_at DESC) の
    まま返す — cap に掛かるときは新しい記事を優先する。``actor_map`` は
    actor/actor_nation leaf を候補ゲートに使う PIR のために渡す (無ければ leaf 不成立)。
    ``include_stale=False`` は未判定 (判定行なし) のみを対象にする — 毎時 incremental が
    stale 再判定 (PIR 編集起因、最大 ~1000 件) を日中の Ollama に流さないための絞り。
    """
    rev = pir_judge_rev(pir)
    amap = actor_map or {}
    candidates = 0
    targets: list[Any] = []
    for r in rows:
        ok, _fired = evaluate_match(
            pir.match,
            ArticleFacts.from_db_row(r, actor_values=amap.get(r["article_id"], set())),
        )
        if not ok:
            continue
        candidates += 1
        existing = state.get(str(r["article_id"]))
        if existing == rev:
            continue
        if existing is not None and not include_stale:
            continue
        targets.append(r)
    return candidates, targets


def judge_backlog_stats(
    *,
    repo: RunHistoryRepository | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, dict[str, int]]:
    """PIR ごとの候補/判定済み/未判定 backlog (LLM を呼ばない観測用、週次監査が使う)。"""
    from src.pir.integration import get_pir_config

    repo_inst = repo or RunHistoryRepository()
    cfg = get_pir_config()
    pirs = [p for p in cfg.enabled_priorities() if p.llm_judge.enabled and p.match]
    if not pirs:
        return {}
    rows, actor_map = _load_posted_rows(
        repo=repo_inst, lookback_hours=window_days * 24, limit=_ROW_LIMIT
    )
    out: dict[str, dict[str, int]] = {}
    for pir in pirs:
        state = repo_inst.fetch_pir_llm_state(pir.id)
        candidates, targets = _judge_targets_for_pir(pir, rows, state, actor_map)
        out[pir.id] = {
            "candidates": candidates,
            "judged": candidates - len(targets),
            "backlog": len(targets),
        }
    return out


async def judge_pending(
    *,
    repo: RunHistoryRepository | None = None,
    llm: LLMClient | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_judgments: int = DEFAULT_MAX_JUDGMENTS,
    concurrency: int = DEFAULT_CONCURRENCY,
    include_stale: bool = True,
    sync_entities: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """llm_judge が有効な全 PIR の未判定/stale 候補を判定して upsert する (incremental)。

    backfill も定常運転も同一経路 — 差は cap (``max_judgments``) だけ。
    LLM 障害は per-item に数え、連続 ``_ABORT_AFTER_CONSECUTIVE_ERRORS`` 回で
    打ち切る (残りは翌日バッチが拾う)。返り値 = 統計 dict。

    ``sync_entities=True`` は matched=true の verdict を entity タグ (pir, pir_id) へ
    即時反映する — 候補ゲート通過 ∧ verdict 適合 = match なので、夜間 full rebuild を
    待たずにタグを立てて良い (削除側の整合は従来どおり夜間 rebuild が担う)。
    """
    from src.pir.integration import get_pir_config

    if not _llm_judge_enabled():
        return {"disabled": 1, "pirs": 0, "candidates": 0, "judged": 0, "errors": 0}

    repo_inst = repo or RunHistoryRepository()
    cfg = get_pir_config()
    pirs = [p for p in cfg.enabled_priorities() if p.llm_judge.enabled and p.match]
    if not pirs:
        return {"disabled": 0, "pirs": 0, "candidates": 0, "judged": 0, "errors": 0}

    rows, actor_map = _load_posted_rows(
        repo=repo_inst, lookback_hours=window_days * 24, limit=_ROW_LIMIT
    )

    # (pir, row) の判定対象を PIR 横断で集める。cap は PIR ごとのラウンドロビンで
    # 公平配分する (監査 2026-08-01 ③: config 並び順の FIFO だと候補の多い先頭 PIR が
    # cap を独占し、後段の PIR が恒常的に押し出される)。PIR 内の順序 (新しい記事優先)
    # は維持される。
    from itertools import zip_longest

    per_pir_targets: list[list[tuple[Pir, Any]]] = []
    total_candidates = 0
    for pir in pirs:
        state = repo_inst.fetch_pir_llm_state(pir.id)
        candidates, targets = _judge_targets_for_pir(
            pir, rows, state, actor_map, include_stale=include_stale
        )
        total_candidates += candidates
        per_pir_targets.append([(pir, r) for r in targets])
    work: list[tuple[Pir, Any]] = [
        item
        for round_items in zip_longest(*per_pir_targets)
        for item in round_items
        if item is not None
    ]

    capped = work[: max(max_judgments, 0)]
    stats = {
        "disabled": 0,
        "pirs": len(pirs),
        "candidates": total_candidates,
        "pending": len(work),
        "judged": 0,
        "errors": 0,
        "capped_out": len(work) - len(capped),
        "entities_added": 0,
    }
    if dry_run or not capped:
        return stats

    if llm is None:
        from src.config_loader import load_app_config
        from src.tools.model_tiers import Step, build_llm_for

        llm = build_llm_for(Step.PIR_LLM_JUDGE, load_app_config())

    sem = asyncio.Semaphore(max(concurrency, 1))
    consecutive_errors = 0
    aborted = False

    async def _one(pir: Pir, row: Any) -> None:
        nonlocal consecutive_errors, aborted
        if aborted:
            return
        async with sem:
            if aborted:
                return
            try:
                verdict = await llm.generate_structured(
                    build_judge_prompt(pir, row),
                    schema=JudgeVerdict,
                    temperature=0.0,
                    max_tokens=300,
                    think=False,
                )
            except Exception as e:  # noqa: BLE001 — 個別失敗は翌日バッチが拾う
                stats["errors"] += 1
                consecutive_errors += 1
                if consecutive_errors >= _ABORT_AFTER_CONSECUTIVE_ERRORS:
                    aborted = True
                    _log.warning("pir_llm_judge_aborted", consecutive_errors=consecutive_errors)
                _log.warning(
                    "pir_llm_judge_item_failed",
                    pir_id=pir.id,
                    article_id=str(row["article_id"]),
                    error=str(e),
                )
                return
            consecutive_errors = 0
            repo_inst.upsert_pir_llm_judgment(
                str(row["article_id"]),
                pir.id,
                matched=bool(verdict.matched),
                reason=verdict.reason,
                pir_rev=pir_judge_rev(pir),
            )
            stats["judged"] += 1
            if sync_entities and verdict.matched:
                added = repo_inst.add_article_entities(str(row["article_id"]), [("pir", pir.id)])
                stats["entities_added"] += added

    await asyncio.gather(*(_one(pir, row) for pir, row in capped))
    if aborted:
        stats["aborted"] = 1
    return stats


async def judge_hourly(
    *,
    repo: RunHistoryRepository | None = None,
    llm: LLMClient | None = None,
) -> dict[str, int]:
    """毎時 incremental judge (pir-judge-hourly job 本体)。

    新着候補のみ (include_stale=False) を小 cap で判定し、適合は entity タグへ
    即時反映する — judge PIR の適合が夜間バッチまで最大 24h 未確定になるラグを
    1h に縮める (PIR 画面ライブ評価 / News facet / 夕方 synthesis が対象)。
    stale 一掃と full reconcile (削除側) は従来どおり夜間 pir-entity-rebuild が担う。
    """
    return await judge_pending(
        repo=repo,
        llm=llm,
        max_judgments=HOURLY_MAX_JUDGMENTS,
        include_stale=False,
        sync_entities=True,
    )

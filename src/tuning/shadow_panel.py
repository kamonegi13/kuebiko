"""シャドーパネル — 較正格子 P3 (docs/self_evolving_tuning_design.md §4 C2/C3、§10.2)。

E1 正解 (feed 突合ラベル) が判明している主体判定の事例を、多様 2 モデルが**盲検**
(本番判定を見せず証拠のみ) で再判定し、panel_verdicts に記録する。目的は計測:

- **分裂率** = パネル内で意見が割れた率 → 外部 LLM にエスカレーションされるはずだった
  件数/週の実数 (§10.2 の予算判断の前提)
- **合意パターン × 実正解率** = 較正曲線 (C3) の原資。E1 正解がある層でのみ測る
- 分裂サンプルの送信本文量 (外部呼び出しのトークン見積の実数)

**シャドー専用** — 本番パイプラインは panel_verdicts を読まない (§10.1)。per-article の
パネル呼び出しはしない (§5-6): 入力は E1 ラベル付き事例のみ (週 ~10-20 件 × 2 モデル)。

パネル構成: 本番 main (安定性の測定) + 異議者 (env ``TUNING_PANEL_SECOND_MODEL``、
既定 gpt-oss:20b)。設計 §4 C2 の当初案は Llama 3.1 8B だったが、ローカル未 pull かつ
**別系統ファミリの方が誤りの脱相関に効く** (壁 1) ため gpt-oss を既定にした。
denylist 検証は build_llm_for_ref が同一に掛ける。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

_FIELD = "subject_actor"
# 週次 + 取りこぼし余裕 (ラベルの arrived_at 基準)
_LOOKBACK_DAYS = 10
# 1 回の実行上限 (係争を優先し、残り枠を一致対照に使う)
_MAX_DISPUTES = 12
_MAX_CONTROLS = 8

ENV_SECOND_MODEL = "TUNING_PANEL_SECOND_MODEL"
_DEFAULT_SECOND_MODEL = "gpt-oss:20b"

# llm_factory(model_ref) -> LLM。None = 本番 main ティア (テストで差し替える seam)
LlmFactory = Callable[[str | None], Any]


@dataclass(frozen=True)
class PanelRunResult:
    """1 回のシャドーパネル実行の集計。"""

    cases: int = 0
    disputes: int = 0
    controls: int = 0
    splits: int = 0
    unreachable: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)


def _second_model_ref() -> str:
    return os.environ.get(ENV_SECOND_MODEL, "").strip() or _DEFAULT_SECOND_MODEL


# 推論系モデル: think=False だと出力が reasoning 側へ流れ structured content が空になる
# (2026-08-22 実測。Gemma4 の「digest 系は think=False 必須」と真逆の癖)
_FORCE_THINK_PREFIXES = ("gpt-oss",)


class _ForceThink:
    """generate_structured の think を強制する薄い adapter (パネル呼出限定)。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["think"] = True
        return await self._inner.generate_structured(*args, **kwargs)


def _default_llm_factory(model_ref: str | None) -> Any:
    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for, build_llm_for_ref

    config = load_app_config()
    if model_ref is None:
        return build_llm_for(Step.ARTICLE_SUMMARY, config)
    llm = build_llm_for_ref(model_ref, Step.ARTICLE_SUMMARY, config)
    if model_ref.startswith(_FORCE_THINK_PREFIXES):
        return _ForceThink(llm)
    return llm


def _classify_agreement(values: list[str], truth: str) -> str:
    """パネル 2 値の合意パターン。エラー混入は 'error' (集計の分母から外す)。"""
    if any(v == "(error)" for v in values):
        return "error"
    if len(set(values)) > 1:
        return "split"
    return "unanimous_correct" if values[0] == truth else "unanimous_wrong"


async def run_weekly_shadow_panel(
    *,
    repo: Any | None = None,
    llm_factory: LlmFactory | None = None,
    now: datetime | None = None,
    lookback_days: int = _LOOKBACK_DAYS,
    max_disputes: int = _MAX_DISPUTES,
    max_controls: int = _MAX_CONTROLS,
) -> PanelRunResult:
    """週次: E1 正解付き事例をパネルに通し、裁定と分裂率の原資を記録する。

    lookback_days / cap は過去データ一括検証 (backfill) 用に広げられる — 冪等
    (case_key) なので何度実行しても既裁定は再判定されない。
    """
    try:
        return await _run_impl(
            repo=repo,
            llm_factory=llm_factory or _default_llm_factory,
            now=now or datetime.now(UTC),
            lookback_days=lookback_days,
            max_disputes=max_disputes,
            max_controls=max_controls,
        )
    except Exception as e:  # noqa: BLE001 — パネルの失敗で scheduler を汚さない
        _log.error("shadow_panel_failed", error=f"{type(e).__name__}: {e}")
        return PanelRunResult(errors=[f"{type(e).__name__}: {e}"])


async def _run_impl(
    *,
    repo: Any | None,
    llm_factory: LlmFactory,
    now: datetime,
    lookback_days: int,
    max_disputes: int,
    max_controls: int,
) -> PanelRunResult:
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.judgment_classifier import _BODY_MAX_CHARS, classify_judgment
    from src.pipeline.persistence import _relevant_actors
    from src.storage.run_history import RunHistoryRepository

    _repo = repo if repo is not None else RunHistoryRepository()
    since_iso = (now - timedelta(days=lookback_days)).isoformat()
    raw_cases = _repo.fetch_subject_panel_cases(since_iso)

    # 係争 (本番≠正解) を優先し、一致対照は較正曲線の完全性のために少数混ぜる。
    # ⚠ cap は**既裁定を除いてから**切る — 先に切ると窓内の既裁定が枠を占有し、
    # 反復実行・翌週実行が新事例に永久に到達しない (08-22 初回運転で発覚)
    disputes = []
    controls = []
    skipped = 0
    for c in raw_cases:
        if _repo.has_panel_verdict(f"subjpanel:{c['article_id']}:{c['truth']}"):
            skipped += 1
            continue
        production_first = c["production"].split(",")[0].strip()
        if production_first != c["truth"]:
            disputes.append(c)
        else:
            controls.append(c)
    picked = disputes[:max_disputes] + controls[:max_controls]

    second_ref = _second_model_ref()
    panelists: list[tuple[str, Any]] = [
        ("main", llm_factory(None)),
        (second_ref, llm_factory(second_ref)),
    ]
    # §13-17: 裁定時の rubric 版を層別次元として記録 (版横断の regime 混合防止)
    from src.tuning.fewshot_pool import current_judgment_rubric_version

    rubric_version = current_judgment_rubric_version()

    aliases = load_actor_aliases()
    result_cases = 0
    result_disputes = 0
    result_controls = 0
    splits = 0
    unreachable = 0
    errors: list[str] = []

    for c in picked:
        is_dispute = c["production"].split(",")[0].strip() != c["truth"]
        case_key = f"subjpanel:{c['article_id']}:{c['truth']}"
        body = c["body"][:_BODY_MAX_CHARS]
        candidates = _relevant_actors(aliases.find_all(f"{c['title']}\n{body}"), c["category"])
        if not any(a.id == c["truth"] for a in candidates):
            # 正解が候補に無い = 構造ゲート上 LLM は選べない (候補未到達)。
            # パネルの誤り率でなく到達率の問題なので裁定せず数だけ残す
            unreachable += 1
            continue

        verdicts: list[dict[str, str]] = []
        for model_label, llm in panelists:
            try:
                out = await classify_judgment(
                    llm,
                    title=c["title"],
                    category=c["category"] or None,
                    body=body,
                    published=c["published"],
                    candidates=candidates,
                    # §13-6: パネルは自分が監査すべき答え (プール) を読んではならない —
                    # flag 状態に依らず構造的に遮断
                    allow_fewshot=False,
                )
                value = str(out.subject_actor_id or "") if out is not None else "(error)"
            except Exception as e:  # noqa: BLE001 — 1 件の失敗でパネル全体を止めない
                _log.warning("shadow_panel_judge_failed", model=model_label, error=str(e))
                value = "(error)"
            verdicts.append({"model": model_label, "value": value})

        agreement = _classify_agreement([v["value"] for v in verdicts], c["truth"])
        if agreement == "error":
            # 情報ゼロの裁定は永続化しない — error 行が case_key を塞ぐと修正後の
            # 再裁定が構造的に不可能になる (来週の sweep で自動再試行される)
            errors.append(f"{c['article_id']}: パネルにエラー混入")
            continue
        _repo.record_panel_verdict(
            case_key=case_key,
            field=_FIELD,
            article_id=c["article_id"],
            truth_value=c["truth"],
            production_value=c["production"],
            verdicts=json.dumps(verdicts, ensure_ascii=False),
            agreement=agreement,
            is_dispute=is_dispute,
            prompt_chars=len(c["title"]) + len(body),
            rubric_version=rubric_version,
        )
        result_cases += 1
        result_disputes += int(is_dispute)
        result_controls += int(not is_dispute)
        splits += int(agreement == "split")

    result = PanelRunResult(
        cases=result_cases,
        disputes=result_disputes,
        controls=result_controls,
        splits=splits,
        unreachable=unreachable,
        skipped_existing=skipped,
        errors=errors,
    )
    # P4: TTL 失効 (§12 の無応答既定) → 裁定待ち件数を通知に載せる (起票通知の原則)
    from src.tuning.adjudication import expire_stale_cases

    expire_stale_cases(_repo, now=now)
    pending = len(_repo.list_pending_adjudications())
    await _notify_summary(_repo, result, second_ref, pending=pending)
    _log.info(
        "shadow_panel_complete",
        cases=result.cases,
        disputes=result.disputes,
        splits=result.splits,
        unreachable=result.unreachable,
        skipped=result.skipped_existing,
        error_count=len(result.errors),
    )
    return result


async def _notify_summary(
    repo: Any, result: PanelRunResult, second_ref: str, *, pending: int = 0
) -> None:
    """週次 ops 通知 (0 件でも 1 通 — 届くこと自体が監査の生存証明)。"""
    try:
        totals = repo.summarize_panel_verdicts()
        split_rate = totals.get("split_rate")
        split_cell = f"{split_rate:.1%}" if split_rate is not None else "-"
        avg_chars = totals.get("by_agreement", {}).get("split", {}).get("avg_prompt_chars", 0)
        from src.tuning.adjudication import ADJUDICATION_TTL_DAYS

        pending_line = (
            f"\n⚖️ 人間の裁定待ち {pending} 件 — 運用タブの裁定カードでレビューしてください"
            f" (無応答は {ADJUDICATION_TTL_DAYS} 日で保守側に自動アーカイブ)"
            if pending
            else ""
        )
        body = (
            f"今回: 裁定 {result.cases} 件 (係争 {result.disputes} / 対照 {result.controls})、"
            f"分裂 {result.splits} 件、候補未到達 {result.unreachable} 件。\n"
            f"累計: 裁定 {totals.get('judged', 0)} 件、分裂率 {split_cell}"
            f" (= 外部 LLM に上がるはずだった率)、分裂例の平均送信 {avg_chars:,} 字。\n"
            f"パネル: main + {second_ref} (シャドー — 本番は読まない)" + pending_line
        )
        from src.ui.services.ops_notify import post_ops_message

        await post_ops_message(
            title=f"シャドーパネル週次: 裁定 {result.cases} 件 / 分裂 {result.splits}",
            body=body,
            importance="medium" if pending else "low",
        )
    except Exception as e:  # noqa: BLE001 — 通知失敗で裁定結果を捨てない
        _log.warning("shadow_panel_notify_failed", error=str(e))

"""緊急の受け皿 (gemma4:26b) の明示の記録 (P0-26、10-29 判断 J4)。

受け皿を 2 枠 (S・N) に揃えられないときに限り ``EMERGENCY_FALLBACK_MODELS`` を使ってよい。
ただし**黙って落ちない**: 発動のたびに構造化ログ ``llm_emergency_fallback`` を出し、
プロセス内で数える。通常の割当 (tiers・``step:*``) には入れない。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date

from src.logging_config import get_logger
from src.tools.model_registry import EMERGENCY_FALLBACK_MODELS, RESIDENT_SLOTS

_log = get_logger(__name__)

_counts: Counter[str] = Counter()
_last_notified: list[date] = []

_LATEST_SUFFIX = ":latest"


def is_emergency_model(model: str) -> bool:
    return model.strip() in EMERGENCY_FALLBACK_MODELS


def record_emergency_fallback(*, step: str, original: str, fallback: str, reason: str) -> None:
    """緊急の受け皿が実際に使われたことを記録する (ログ + 計数)。"""
    _counts[fallback] += 1
    _log.warning(
        "llm_emergency_fallback",
        step=step,
        original_model=original,
        fallback_model=fallback,
        reason=reason[:200],
        count=_counts[fallback],
    )


def emergency_fallback_counts() -> dict[str, int]:
    return dict(_counts)


def should_notify_ops_today(today: date) -> bool:
    """ops 通知を 1 日 1 回に抑える判定 (True を返した日は以後 False)。"""
    if _last_notified and _last_notified[-1] == today:
        return False
    _last_notified.append(today)
    del _last_notified[:-1]
    return True


def reset_for_tests() -> None:
    _counts.clear()
    _last_notified.clear()


def _is_installed(model: str, installed: Iterable[str]) -> bool:
    names = {n.removesuffix(_LATEST_SUFFIX) for n in installed}
    return model.removesuffix(_LATEST_SUFFIX) in names


def missing_resident_models(installed: Iterable[str]) -> list[str]:
    """常駐の枠 (S・N) のうち Ollama に無いモデル。"""
    names = list(installed)
    return [m for m in RESIDENT_SLOTS.values() if not _is_installed(m, names)]


def pick_installed_or_emergency(
    preferred: str, installed: Iterable[str], *, step: str, reason: str
) -> str:
    """``preferred`` が Ollama に無く、緊急の受け皿が在るときだけ受け皿を返す (記録つき)。

    どちらも無ければ ``preferred`` のまま (黙って別のモデルへは落とさない)。
    """
    names = list(installed)
    if _is_installed(preferred, names):
        return preferred
    for emergency in sorted(EMERGENCY_FALLBACK_MODELS):
        if _is_installed(emergency, names):
            record_emergency_fallback(
                step=step, original=preferred, fallback=emergency, reason=reason
            )
            return emergency
    return preferred


def warn_missing_resident_models(installed: Iterable[str]) -> list[str]:
    """起動時の警告。常駐の枠のモデルが無ければ ``resident_model_missing`` を出す。"""
    missing = missing_resident_models(installed)
    for model in missing:
        _log.warning(
            "resident_model_missing", model=model, emergency=sorted(EMERGENCY_FALLBACK_MODELS)
        )
    return missing

"""生成物の入力の記録 (P0-18)。

物語系の生成物 (事象ニュース・Spotlight・状況総括・PIR 日次要点) を作る LLM 呼び出しの前後で
prompt 本文を ``generation_inputs`` へ残す。**生成の出力を変えず、失敗しても生成を止めない**:
書き込みの失敗は ``write_failures`` に数えて (ログ ``ops_write_failed``) 握る。
旗 ``GENERATION_INPUT_RECORD`` (既定 1、0 で記録を止める。表は残る)。
本文を含むため公開面へは出さない (CLAUDE.md §9)。年齢で消す処理は置かない。
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

from src.ops.write_failures import record_failure

GENERATION_INPUT_RECORD_ENV = "GENERATION_INPUT_RECORD"
COMPONENT = "generation_inputs"
_OFF_VALUES = frozenset({"0", "false", "off", "no", ""})


_repo_lock = threading.Lock()
_cached_repo: Any | None = None


def _default_repo() -> Any:
    """呼ぶたびに作らず、遅延生成した 1 つを使い回す。"""
    global _cached_repo  # noqa: PLW0603
    with _repo_lock:
        if _cached_repo is None:
            from src.storage.run_history import RunHistoryRepository

            _cached_repo = RunHistoryRepository()
        return _cached_repo


def reset_for_tests() -> None:
    """テスト専用: 使い回しの repo を捨てる。"""
    global _cached_repo  # noqa: PLW0603
    with _repo_lock:
        _cached_repo = None


def is_enabled() -> bool:
    raw = os.environ.get(GENERATION_INPUT_RECORD_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _OFF_VALUES


def prompt_sha256(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def record_generation_input(
    *,
    step: str,
    model: str,
    product_kind: str,
    product_id: str,
    prompt_text: str,
    repo: Any | None = None,
) -> None:
    """prompt を 1 行残す。例外は出さない (失敗は計数してログ)。"""
    if not is_enabled() or not prompt_text:
        return
    try:
        target = repo if repo is not None else _default_repo()
        target.record_generation_input(
            step=step,
            model=model,
            product_kind=product_kind,
            product_id=product_id,
            prompt_sha256=prompt_sha256(prompt_text),
            prompt_text=prompt_text,
        )
    except Exception as exc:  # noqa: BLE001 - 記録は本流を止めない。計数して握る
        record_failure(COMPONENT, exc)

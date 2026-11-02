"""生成物の入力の記録 (P0-18)。

物語系の生成物 (事象ニュース・Spotlight・状況総括・PIR 日次要点) を作る LLM 呼び出しの前後で
prompt 本文と構造化入力 (``input_json``、prompt の元の id・欄) を
``generation_inputs`` へ残す。**生成の出力を変えず、失敗しても生成を止めない**:
書き込みの失敗は ``write_failures`` に数えて (ログ ``ops_write_failed``) 握る。
旗 ``GENERATION_INPUT_RECORD`` (既定 1、0 で記録を止める。表は残る)。
本文を含むため公開面へは出さない (CLAUDE.md §9)。年齢で消す処理は置かない。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from src.ops.write_failures import record_failure

GENERATION_INPUT_RECORD_ENV = "GENERATION_INPUT_RECORD"
COMPONENT = "generation_inputs"
# 構造化入力 (input_json) の上限。表と同じく年齢で消さないため、1 行の大きさで増え方を抑える
MAX_INPUT_JSON_CHARS = 200_000
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


def _json_default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    return str(obj)


def _dumps(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def serialize_input(data: Mapping[str, Any], *, limit: int = MAX_INPUT_JSON_CHARS) -> str:
    """構造化入力を JSON 文字列にする。``limit`` 字を超えるときは最も大きい欄から
    ``{"_dropped_chars": n}`` に置き換えて収め、``_truncated`` を立てる (元の欄名は残る)。"""
    text = _dumps(data)
    if len(text) <= limit:
        return text
    current: dict[str, Any] = {**data, "_truncated": True}
    sizes = {k: len(_dumps({k: v})) for k, v in data.items()}
    for key in sorted(sizes, key=lambda k: sizes[k], reverse=True):
        current = {**current, key: {"_dropped_chars": sizes[key]}}
        text = _dumps(current)
        if len(text) <= limit:
            return text
    return text


def record_generation_input(
    *,
    step: str,
    model: str,
    product_kind: str,
    product_id: str,
    prompt_text: str,
    repo: Any | None = None,
    input_data: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
) -> None:
    """prompt を 1 行残す。例外は出さない (失敗は計数してログ)。

    ``input_data`` は prompt の元になった構造化入力 (id・欄)。callable なら旗が on のときだけ
    評価する。作れない・直列化できないときは ``input_json`` だけ NULL にして prompt は残す。
    """
    if not is_enabled() or not prompt_text:
        return
    input_json: str | None = None
    if input_data is not None:
        try:
            built = input_data() if callable(input_data) else input_data
            input_json = serialize_input(built)
        except Exception as exc:  # noqa: BLE001 - 構造化入力の失敗は prompt の記録を妨げない
            record_failure(COMPONENT, exc)
    try:
        target = repo if repo is not None else _default_repo()
        target.record_generation_input(
            step=step,
            model=model,
            product_kind=product_kind,
            product_id=product_id,
            prompt_sha256=prompt_sha256(prompt_text),
            prompt_text=prompt_text,
            input_json=input_json,
        )
    except Exception as exc:  # noqa: BLE001 - 記録は本流を止めない。計数して握る
        record_failure(COMPONENT, exc)

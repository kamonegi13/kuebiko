"""記録の失敗の計数器 (P0-10)。

観測用の表 (llm_usage・detect の確率・生成物の入力 等) への書き込みは本流を止めてはいけないが、
黙って飲むと「記録が空なのか、書けていないのか」が区別できない。呼び手は例外を握りつぶす代わりに
``record_failure`` を呼ぶ。計数はプロセス内 (再起動で 0 に戻る)、``snapshot`` は
``/api/v1/health-status`` の ``write_failures`` に出る。ログには例外の型だけを出し、
メッセージ (本文・URL・資格情報を含みうる) は出さない。
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType

from src.logging_config import get_logger

_log = get_logger(__name__)

_lock = threading.Lock()
_counts: Counter[str] = Counter()


def record_failure(component: str, exc: BaseException) -> None:
    """``component`` の記録の失敗を 1 件数える。例外の型だけをログに出す。"""
    with _lock:
        _counts[component] += 1
        total = _counts[component]
    _log.warning(
        "ops_write_failed",
        component=component,
        error_type=type(exc).__name__,
        count=total,
    )


def snapshot() -> Mapping[str, int]:
    """現在の計数の読み取り専用の写し (取得後の加算は反映されない)。"""
    with _lock:
        return MappingProxyType(dict(_counts))


def reset_for_tests() -> None:
    """テスト専用: 計数を 0 に戻す。"""
    with _lock:
        _counts.clear()

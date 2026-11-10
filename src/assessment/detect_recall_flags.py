"""detect の分けた形の回収率を戻す 4 つの旗 (DETECT-RECALL、2026-11-10、すべて既定 ``0``)。

11-10 の再判定 (data/eval/detect_rejudge/) で分けた形の開設が現行の 0.70 倍で不合格だった原因の
分解に対する直し。旗は呼び出しのたびに環境変数から読む (本番の shadow の記録にも同じ旗が効く)。
``1`` 以外 (未設定・``0``・不明な値) はすべて off = 従来と同一。

- ``DETECT_CLAIM_SALVAGE``: 忠実さの関門で止まった claim の付け足しの節を削って照合し直す
  (:mod:`src.assessment.claim_salvage`)
- ``DETECT_WRITER_INPUT_V2``: 書き手の入力から媒体名と「再浮上」の印を外し、記事の要約を足す
- ``DETECT_WRITER_CAP_REFILL``: 書き手の上限を、止まった・見送った回の分だけ +3 回まで広げる
  (開設の上限は変えない)
- ``DETECT_MERGE_CONFLICT_GATE``: CVE が交わらない・被害組織が食い違う合流をさせない
"""

from __future__ import annotations

import os

CLAIM_SALVAGE_ENV = "DETECT_CLAIM_SALVAGE"
WRITER_INPUT_V2_ENV = "DETECT_WRITER_INPUT_V2"
WRITER_CAP_REFILL_ENV = "DETECT_WRITER_CAP_REFILL"
MERGE_CONFLICT_GATE_ENV = "DETECT_MERGE_CONFLICT_GATE"

#: 本ファイルが管理する旗の名簿 (再判定の道具が腕ごとに固定する対象)
RECALL_FLAG_KEYS: tuple[str, ...] = (
    CLAIM_SALVAGE_ENV,
    WRITER_INPUT_V2_ENV,
    WRITER_CAP_REFILL_ENV,
    MERGE_CONFLICT_GATE_ENV,
)

#: 書き手の上限の追加分 (止まった・見送った回の数がこれを超えても足さない)
WRITER_CAP_REFILL_MAX = 3


def _is_on(name: str) -> bool:
    return os.environ.get(name, "0").strip() == "1"


def claim_salvage_enabled() -> bool:
    return _is_on(CLAIM_SALVAGE_ENV)


def writer_input_v2_enabled() -> bool:
    return _is_on(WRITER_INPUT_V2_ENV)


def writer_cap_refill_enabled() -> bool:
    return _is_on(WRITER_CAP_REFILL_ENV)


def merge_conflict_gate_enabled() -> bool:
    return _is_on(MERGE_CONFLICT_GATE_ENV)

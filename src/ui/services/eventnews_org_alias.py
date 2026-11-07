"""被害組織名の別名の写像 (照合キー → 代表キー) の唯一の読み口 (ORG-D-IMPL、2026-10-31)。

``EVENTNEWS_ORG_ALIAS_SOURCE``:
  * ``pairs`` (既定) — ``org_identity_pairs`` の「同一」判定を連結して毎回作る (従来どおり)。
  * ``dict`` — ``org_alias_dict`` (``scripts/build_org_alias_dict.py`` が実体化した表) を読む。

使い手は 2 つ: 統合の焦点の関門 (``match_focus`` の ``org_alias``。conflict を消す側にだけ効く)
と毎時の群化 (``eventnews_org_identity``)。読めなくても動く (別名なし = conflict が多めの側)。
"""

from __future__ import annotations

import os

from src.eventnews.org_identity import build_alias_map
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

ALIAS_SOURCE_ENV = "EVENTNEWS_ORG_ALIAS_SOURCE"
SOURCE_PAIRS = "pairs"
SOURCE_DICT = "dict"


def org_alias_source() -> str:
    """``pairs`` / ``dict``。未設定・読めない値は pairs (従来どおり)。"""
    raw = os.environ.get(ALIAS_SOURCE_ENV, SOURCE_PAIRS).strip().lower()
    return SOURCE_DICT if raw == SOURCE_DICT else SOURCE_PAIRS


def load_org_alias(repo: RunHistoryRepository) -> dict[str, str]:
    """被害組織の別名判定 (「同一」) の写像。失敗は警告して空 (照合は別名なしでも成り立つ)。"""
    try:
        if org_alias_source() == SOURCE_DICT:
            return repo.load_org_alias_dict()
        return dict(build_alias_map(repo.list_org_identity_same_pairs()).mapping)
    except Exception as e:  # noqa: BLE001 — 補助。別名が無くても照合は成り立つ
        _log.warning("eventnews_focus_org_alias_failed", error=str(e)[:160])
        return {}

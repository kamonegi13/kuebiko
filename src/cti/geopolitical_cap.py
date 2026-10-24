"""サイバーでない地政学・政策記事の重要度上限 (2026-10-24、利用者承認)。

背景: 保存される importance は要約 LLM の出力そのもので、要約の基準が geopolitical に
「日本・同盟国への地政学的インパクト」で high を許している。triage の平たい基準
(軍事・外交は high にしない) と設計書 §4.1 (地政学は深刻さの対象外、重さは
``strategic_weight`` で扱う) に反する。直近 14 日の posted high のうち geopolitical 55 件は
全件が非サイバー (深刻さ未導出)、正解集では 34/34 が正解でも high でない。

ここでは **category が geopolitical / policy** の high を medium に下げる。サイバーの地政学
(国家系のサイバー作戦・重要インフラへのサイバー攻撃) は要約が apt / incident 等の
サイバー系カテゴリに入れるので対象外。IOC・ATT&CK 手法を伴う記事も下げない。
重さは ``article_importance_v2.strategic_weight`` に残る (保存値は変えない)。
``other`` はまとめ記事等の雑多な受け皿で、正解集に取りこぼしが出たため対象にしない。

非サイバーの category の SSoT は ``src/cti/importance_v2.NON_CYBER_CATEGORIES``
(geopolitical / policy / other)。ここはその部分集合 (``other`` を除く)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from src.cti.importance_v2 import NON_CYBER_CATEGORIES
from src.tools.discord_publisher import Importance

CAP_ENV: Final = "GEOPOLITICAL_HIGH_CAP"
CAP_MODE_OFF: Final = "off"
CAP_MODE_SHADOW: Final = "shadow"
CAP_MODE_ON: Final = "on"

#: 上限をかける category。``other`` は取りこぼし (まとめ記事 S3) が出たため除く。
CAPPED_CATEGORIES: Final[frozenset[str]] = NON_CYBER_CATEGORIES - {"other"}


def cap_mode() -> str:
    """``GEOPOLITICAL_HIGH_CAP`` を解釈する (未設定・不明値は shadow = ログのみ)。"""
    raw = os.environ.get(CAP_ENV, CAP_MODE_SHADOW).strip().lower()
    if raw in ("0", CAP_MODE_OFF):
        return CAP_MODE_OFF
    if raw in ("1", CAP_MODE_ON):
        return CAP_MODE_ON
    return CAP_MODE_SHADOW


@dataclass(frozen=True)
class GeopoliticalCapResult:
    importance: Importance
    would_cap: bool  # high → medium に該当したか (shadow でも True)
    applied: bool  # 実際に importance を変えたか


def cap_geopolitical_importance(
    importance: Importance,
    category: str | None,
    *,
    has_cyber_evidence: bool = False,
    mode: str | None = None,
) -> GeopoliticalCapResult:
    """非サイバーの地政学・政策記事の high を medium に下げる (要約の後・保存の前)。"""
    effective = cap_mode() if mode is None else mode
    if effective == CAP_MODE_OFF or importance != "high" or has_cyber_evidence:
        return GeopoliticalCapResult(importance, False, False)
    if (category or "").strip().lower() not in CAPPED_CATEGORIES:
        return GeopoliticalCapResult(importance, False, False)
    applied = effective == CAP_MODE_ON
    return GeopoliticalCapResult("medium" if applied else importance, True, applied)

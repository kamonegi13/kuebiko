"""被害国の過剰付与を抑える関門 (2026-10-10)。

本番 140 件の監査 (data/eval/victim_country_audit_v1.jsonl) で、被害組織の無い
非インシデント記事 (advisory/press/research/opinion) に victim_country が誤って
付く割合が高いことが判明した: advisory 13 誤/3 正、press 12/1、research+opinion
8/5 (breaking は 74 正/11 誤で問題なし)。典型例は CISA KEV advisory が US、
ドイツ BSI の警告が DE になる — 発行元の国を被害国と混同している。

このルールをシミュレーションすると誤り 27 件を修正し正しい値 3 件を NULL 化する
(誤った国名を出すより欠落させる方が許容できるという判断)。

rollback: 環境変数 ``VICTIM_COUNTRY_GATE`` を ``"0"`` にすると常に無効化 (False)。
"""

from __future__ import annotations

import os
from collections.abc import Sequence

#: 被害組織が実在するインシデントではない記事種別 (src.pipeline.summary.ArticleType より)。
#: tutorial は監査対象には無かったが、定義上非インシデントのため含める。
NON_INCIDENT_ARTICLE_TYPES: frozenset[str] = frozenset(
    {"advisory", "press", "research", "opinion", "tutorial"}
)

#: 関門の有効/無効を切り替える環境変数名 (既定 on、"0" で無効化)。
GATE_ENV = "VICTIM_COUNTRY_GATE"


def _gate_enabled() -> bool:
    """``VICTIM_COUNTRY_GATE`` (既定 on)。"0" のときだけ無効化する。"""
    return os.environ.get(GATE_ENV, "1").strip() != "0"


def should_drop_victim_country(article_type: str, victim_orgs: Sequence[str]) -> bool:
    """被害国を落とすべきか。

    非インシデント種別 (``NON_INCIDENT_ARTICLE_TYPES``) で、かつ被害組織名が
    1 件も (空白除去後に) 無いときに True を返す。
    """
    if not _gate_enabled():
        return False
    if article_type not in NON_INCIDENT_ARTICLE_TYPES:
        return False
    has_org = any(str(org).strip() for org in victim_orgs)
    return not has_org

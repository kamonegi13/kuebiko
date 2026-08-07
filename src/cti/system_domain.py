"""脅威が影響する「システム軸」の導出 (OT / 境界 / IT / 不明)。

国家 CI 評価の最重要切り口。OT/IT の二値でなく**影響順 4 段**:
- ``ot``: 制御系 (物理・供給に直結。最重要)
- ``boundary``: OT/IT 境界・監視系 (HMI/historian/リモート保守 = Purdue L3/3.5。潜伏の標的)
- ``it``: 情報系 (業務・顧客データ・基幹系)
- ``unknown``: 不明 (保守的既定)

**derive-first**: 既存信号 (ICS TTP / ICS 製品 / OT-ICS キーワード) から決定論導出。分離できない時は
``unknown`` (憶測で OT にしない)。本質は**過大評価の減衰機構** — CI 事業者のただの IT 被害
(電力会社の人事系ランサム等) を国家 CI の OT 脅威に水増ししない (詳細は board 設計 doc §6)。
"""

from __future__ import annotations

from collections.abc import Iterable

from src.cti.attack_catalog import is_ics_technique
from src.cti.nisc_sectors import kw_hit

SystemDomain = str  # "ot" | "boundary" | "it" | "unknown"

# OT (制御系) を示す語。ICS 製品名・プロトコル・制御用語。
# 短英語 (ics/plc 等) は kw_hit の境界一致で照合する ("forensics"⊂ics 等の誤爆防止)。
_OT_KEYWORDS: tuple[str, ...] = (
    "ics",
    "scada",
    "plc",
    "rtu",
    "dcs",
    "modbus",
    "dnp3",
    "iec 61850",
    "profinet",
    "制御系",
    "制御システム",
    "産業制御",
    "安全計装",
    "safety instrumented",
    "配電制御",
    "電力系統",
    "系統制御",
    "運転制御",
)
# OT 製品を示す語 (affected_product 側で見る)。
_OT_PRODUCT_KEYWORDS: tuple[str, ...] = (
    "scada",
    "plc",
    "dcs",
    "rtu",
    "triconex",
    "modbus",
    "clearscada",
    "wonderware",
    "ignition",
)
# OT/IT 境界・監視系 — **強**: それ自体が OT 文脈の語 (単独で境界と判定してよい)。
_BOUNDARY_STRONG_KEYWORDS: tuple[str, ...] = (
    "hmi",
    "historian",
    "engineering workstation",
    "エンジニアリングワークステーション",
    "ot/it",
    "it/ot",
)
# 境界 — **弱**: IT 侵害でも頻出する一般語 (リモート保守/踏み台等)。OT 文脈の共起が
# あるときのみ境界と判定する (bare「境界」「remote access」は境界型防御等に誤爆するため不使用)。
_BOUNDARY_WEAK_KEYWORDS: tuple[str, ...] = (
    "リモート保守",
    "remote maintenance",
    "ジャンプサーバ",
    "jump server",
    "踏み台",
)
# 弱キーワードを境界に昇格させる OT 文脈語 (bare「制御」「産業」はアクセス制御/産業界に誤爆
# するため複合語のみ)。
_OT_CONTEXT_KEYWORDS: tuple[str, ...] = (
    "scada",
    "ics",
    "plc",
    "制御システム",
    "制御系",
    "産業制御",
    "産業用",
    "工場",
    "プラント",
    "変電",
    "浄水",
    "パイプライン",
    "ot",
)
# 情報系 (IT) を示す語。
_IT_KEYWORDS: tuple[str, ...] = (
    "情報漏洩",
    "個人情報",
    "顧客情報",
    "会員情報",
    "業務システム",
    "基幹系",
    "情報系",
    "メールサーバ",
    "email server",
    "erp",
    "ファイルサーバ",
    "データ漏洩",
    "顧客データ",
)


def derive_system_domain(
    *,
    text: str,
    ttps: Iterable[str] | None = None,
    affected_products: Iterable[str] | None = None,
) -> SystemDomain:
    """記事の信号からシステム軸を導出する。憶測で OT にせず、迷えば ``unknown``。"""
    lowered = (text or "").lower()

    # 1. ICS ATT&CK テクニック → OT (最強信号)
    if any(is_ics_technique(t) for t in (ttps or ())):
        return "ot"
    # 2. ICS/OT 製品 → OT
    prod_text = " ".join(p.lower() for p in (affected_products or ()))
    if any(kw_hit(kw, prod_text) for kw in _OT_PRODUCT_KEYWORDS):
        return "ot"
    # 3. OT/制御系 キーワード → OT
    if any(kw_hit(kw, lowered) for kw in _OT_KEYWORDS):
        return "ot"
    # 4. 境界・監視系: 強は単独可、弱は OT 文脈の共起が必須 (IT 侵害の水増し防止)
    if any(kw_hit(kw, lowered) for kw in _BOUNDARY_STRONG_KEYWORDS):
        return "boundary"
    if any(kw_hit(kw, lowered) for kw in _BOUNDARY_WEAK_KEYWORDS) and any(
        kw_hit(kw, lowered) for kw in _OT_CONTEXT_KEYWORDS
    ):
        return "boundary"
    # 5. 情報系 (IT)
    if any(kw_hit(kw, lowered) for kw in _IT_KEYWORDS):
        return "it"
    return "unknown"


SYSTEM_DOMAIN_LABELS: dict[str, str] = {
    "ot": "制御系(OT)",
    "boundary": "境界・監視系",
    "it": "情報系(IT)",
    "unknown": "不明",
}

"""JP 重要インフラ 事業者の 3 層判定 (指定事業者名簿 / 事業者型 / 非該当)。

board の観測レーンの精度の土台。victim_org (抽出済みの被害主体文字列) に対して:

- **層1 指定事業者 (designated)**: curated 名簿 (DB SSoT + BUILTIN fail-safe)。
  別名 (和名/英名/略称/主要子会社) を同一事業者 id に解決 → 報道クラスタ merge の鍵にもなる。
- **層2 事業者型 (type)**: 種別サフィックス/固有語 (〜病院/〜市/〜銀行/自衛隊 等) の決定論
  パターン。全列挙不能な中小 CI 事業者 (半田病院型) を回収する。
- **非該当 (None)**: どちらにも合致しない (fintech SaaS/商社/ポイント等) → board では
  周辺観測に降格 (段階を駆動しない)。

名簿は**事業者名→分野のみ** (公知情報)。技術・製品・露出の列は将来も足さない
(資産インベントリ構築禁止の確定原則、設計 doc §8)。

prose (title/summary) には照合しない — 言及 (「三菱重工を装う」等) と被害主体を混同しない。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from src.cti.ci_operators_builtin import BUILTIN_OPERATORS_DATA
from src.cti.nisc_sectors import NISC_SECTORS, kw_hit
from src.logging_config import get_logger

_log = get_logger(__name__)

OPERATORS_CONFIG_KEY = "jp_ci_operators"


@dataclass(frozen=True)
class OperatorDef:
    """指定事業者 1 件 (id / 表示名 / 別名 / NISC 分野)。"""

    id: str
    canonical: str
    aliases: tuple[str, ...]
    sector: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "canonical": self.canonical,
            "aliases": list(self.aliases),
            "sector": self.sector,
        }


@dataclass(frozen=True)
class OrgMatch:
    """victim_org の判定結果。tier: designated (名簿) | type (事業者型)。"""

    tier: str
    sector: str
    operator_id: str | None = None
    canonical: str | None = None
    systemic: bool = False


BUILTIN_OPERATORS: tuple[OperatorDef, ...] = tuple(
    OperatorDef(id=row[0], canonical=row[1], aliases=row[2], sector=row[3])
    for row in BUILTIN_OPERATORS_DATA
)

# 集中ノード (単一障害点): 侵害の影響が分野横断にカスケードする清算・決済・取引の集中点。
# 「代替可能な大手」とはカテゴリカルに異なる (I&W 上、最大級の信号)。**コード所有**の
# 構造ドクトリン (公知の構造知識であり資産インベントリではない。deployment 設定に晒さない)。
SYSTEMIC_NODE_IDS: frozenset[str] = frozenset(
    {
        "zengin_net",  # 全銀ネット (資金清算 = 銀行間決済の集中点)
        "jscc",  # 日本証券クリアリング機構 (清算)
        "hofuri_clearing",  # ほふりクリアリング (清算)
        "jasdec",  # 証券保管振替機構 (振替 = 株式権利の集中管理)
        "jpx",  # 日本取引所グループ / 東証・大阪取引所
        "tfx",  # 東京金融取引所
        "zengin_densai",  # 全銀電子債権ネットワーク
        "cardnet",  # 日本カードネットワーク (クレカ決済スイッチ)
        "custody_bank",  # 日本カストディ銀行 (資産管理の集中点)
        "jprs",  # 日本レジストリサービス (.jp DNS)
    }
)

# ---------- 層2: 事業者型パターン (victim_org 専用、上から順に最初の一致が勝つ) ----------
# 具体 (防衛/水道/医療) を汎用 (政府サフィックス) より上に。org は正規化済 (会社形態語を除去)。
_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("defense", re.compile("自衛隊|防衛省|防衛装備庁|方面総監部|幕僚監部|駐屯地")),
    ("water", re.compile("水道局|水道企業団|下水道")),
    (
        "medical",
        re.compile("病院|医療センター|クリニック|診療所|がんセンター|国立病院機構|医療法人"),
    ),
    ("airport", re.compile("空港")),
    ("aviation", re.compile("航空|空輸")),
    ("railway", re.compile("旅客鉄道|鉄道|電鉄|地下鉄|メトロ|交通局")),
    ("port", re.compile("港運|港湾|埠頭|汽船|郵船|海運")),
    ("electricity", re.compile("電力|送配電|発電|送電")),
    ("gas", re.compile("ガス$|瓦斯")),
    ("oil", re.compile("石油|製油所")),
    ("chemical", re.compile("化学工業|化学$")),
    ("logistics", re.compile("運輸$|運送|通運|急便|物流")),
    (
        "finance",
        re.compile("銀行|信用金庫|信用組合|信金|労働金庫|証券|生命保険|損害保険|農業協同組合|共済"),
    ),
    ("credit", re.compile("カード$|信販")),
    # 放送: 地方局を回収 (キー局・NHK は名簿)。「放送大学」誤爆回避に末尾一致を使う。
    (
        "it_telecom",
        re.compile("電信電話|テレコミュニケーション|ケーブルテレビ|放送$|テレビ$|テレビジョン"),
    ),
    (
        "government",
        re.compile(
            "(?:省|庁|市|町|村|区|都|道|府|県)$"
            "|市役所|町役場|村役場|県警|府警|道警|警察本部|検察庁|裁判所|教育委員会|消防本部"
        ),
    ),
)

_CORP_NOISE = re.compile(r"株式会社|\(株\)|（株）|合同会社|有限会社|\s+")


def _normalize_org(org: str) -> str:
    return _CORP_NOISE.sub("", org)


# ---------- 名簿ロード (DB SSoT → BUILTIN fail-safe、channel_registry と同パターン) ----------

# key = "default" (db_path 省略時)。UI 保存/revert 時は invalidate_operators_cache()。
_OPERATORS_CACHE: dict[str, tuple[OperatorDef, ...]] = {}
# 照合用の (NFKC 正規化済み小文字名, OperatorDef) index。名簿と同時に無効化する。
_NAME_INDEX_CACHE: dict[str, tuple[tuple[str, OperatorDef], ...]] = {}


def _name_index() -> tuple[tuple[str, OperatorDef], ...]:
    """照合用 name index (両側 NFKC: 名簿名も全角記号を含みうる)。"""
    cached = _NAME_INDEX_CACHE.get("default")
    if cached is not None:
        return cached
    index = tuple(
        (unicodedata.normalize("NFKC", name).lower(), op)
        for op in load_operators()
        for name in (op.canonical, *op.aliases)
    )
    _NAME_INDEX_CACHE["default"] = index
    return index


def _parse_entry(raw: Any) -> OperatorDef | None:
    if not isinstance(raw, dict):
        return None
    op_id = str(raw.get("id") or "").strip()
    canonical = str(raw.get("canonical") or "").strip()
    sector = str(raw.get("sector") or "").strip()
    if not op_id or not canonical or sector not in NISC_SECTORS:
        return None
    aliases = tuple(str(a).strip() for a in raw.get("aliases") or [] if str(a).strip())
    return OperatorDef(id=op_id, canonical=canonical, aliases=aliases, sector=sector)


def load_operators() -> tuple[OperatorDef, ...]:
    """名簿を返す。**DB (config_store) を正**、未投入/読み失敗は BUILTIN に fallback。"""
    cached = _OPERATORS_CACHE.get("default")
    if cached is not None:
        return cached
    ops = _load_operators_uncached()
    _OPERATORS_CACHE["default"] = ops
    return ops


def _load_operators_uncached() -> tuple[OperatorDef, ...]:
    try:
        from src.storage import config_store

        raw = config_store.get_config(OPERATORS_CONFIG_KEY)
        if isinstance(raw, list):
            parsed = [op for r in raw if (op := _parse_entry(r)) is not None]
            if parsed:
                return tuple(parsed)
    except Exception as e:  # noqa: BLE001 — DB 不達でも BUILTIN で動作継続
        _log.warning("jp_ci_operators_db_load_failed", error=str(e))
    return BUILTIN_OPERATORS


def invalidate_operators_cache() -> None:
    """UI 保存 / config-history revert 後に呼ぶ。"""
    _OPERATORS_CACHE.clear()
    _NAME_INDEX_CACHE.clear()


def seed_operators_if_absent() -> bool:
    """初回起動時に BUILTIN を config_store へ seed する (既存 key があれば何もしない)。"""
    from src.storage.config_store import seed_config_if_absent

    return seed_config_if_absent(
        OPERATORS_CONFIG_KEY,
        [op.as_dict() for op in BUILTIN_OPERATORS],
        note="初期 seed (コード BUILTIN 名簿)",
    )


def validate_operators(rows: list[dict[str, Any]]) -> list[str]:
    """UI 保存前の検証。エラーメッセージ list (空 = OK)。"""
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}
    for i, raw in enumerate(rows):
        label = f"{i + 1} 行目"
        op_id = str(raw.get("id") or "").strip()
        canonical = str(raw.get("canonical") or "").strip()
        sector = str(raw.get("sector") or "").strip()
        if not op_id:
            errors.append(f"{label}: id が空")
        elif op_id in seen_ids:
            errors.append(f"{label}: id 重複 ({op_id})")
        seen_ids.add(op_id)
        if not canonical:
            errors.append(f"{label}: 事業者名 (canonical) が空")
        if sector not in NISC_SECTORS:
            errors.append(f"{label}: 未知の sector ({sector or '空'})")
        for name in (canonical, *(str(a).strip() for a in raw.get("aliases") or [])):
            if not name:
                continue
            low = name.lower()
            owner = seen_names.get(low)
            if owner is not None and owner != op_id:
                errors.append(f"名称 '{name}' が {owner} と {op_id} で衝突 (誤 merge の原因)")
            seen_names.setdefault(low, op_id)
    return errors


# ---------- 判定 ----------


def classify_org(org: str | None) -> OrgMatch | None:
    """victim_org を 3 層判定する。層1 名簿 (最長一致) → 層2 型 → None。

    NFKC 正規化で全角英数 (官報・公的文書の「ＮＴＴ東日本」等) を半角に揃えて照合する。
    """
    if not org or not org.strip():
        return None
    org = unicodedata.normalize("NFKC", org)
    lowered = org.lower()
    # 層1: 名簿。複数事業者に一致した場合は最長の名前が勝つ
    # (「近鉄エクスプレス」が仮に「近鉄」系の別名と両方に当たっても具体側に解決)。
    best: tuple[int, OperatorDef] | None = None
    for name, op in _name_index():
        if kw_hit(name, lowered) and (best is None or len(name) > best[0]):
            best = (len(name), op)
    if best is not None:
        op = best[1]
        return OrgMatch(
            tier="designated",
            sector=op.sector,
            operator_id=op.id,
            canonical=op.canonical,
            systemic=op.id in SYSTEMIC_NODE_IDS,
        )
    # 層2: 事業者型 (正規化した org 文字列への決定論パターン)
    normalized = _normalize_org(org)
    for sector, pattern in _TYPE_PATTERNS:
        if pattern.search(normalized):
            return OrgMatch(tier="type", sector=sector)
    return None


def designated_counts_by_sector() -> dict[str, int]:
    """分野ごとの指定事業者 (名簿) 数。カバレッジ分母 (暗域の定量化) に使う。"""
    counts: dict[str, int] = {}
    for op in load_operators():
        counts[op.sector] = counts.get(op.sector, 0) + 1
    return counts

"""config 駆動の routing rule engine (案 B / Stage 1)。

``config/delivery/routing_rules.yaml`` の順序付きルールを上から評価し、最初に ``when`` が
一致した rule の channel を採用する (first-match)。現行ハードコード ladder (R2-R7) の
「順次 return」と等価で、**順序が tier を、AND がゲートを表現**する。

設計方針:
    - 判定 (どの条件 → どの channel) は **すべて config**。検出 (KEV カタログ照合・
      regex 等で signal を算出) は routing_signals.py 側 (コード) に残す。
    - 本 engine は ``ROUTING_RULES_ENGINE=1`` のときだけ使用 (既定は旧 route())。
    - 不明な述語・壊れた config は **fail-safe** (no-match / None) に倒し routing を壊さない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.config_loader import SourceQualityConfig
from src.cti.routing_decision import RoutingDecision
from src.cti.routing_signals import RoutingSignals
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_RULES_PATH = Path("config/delivery/routing_rules.yaml")

# 運用 config DB store の key (案 B: yaml/git を runtime の正にしない)。
CONFIG_KEY = "routing_rules"

# 述語 flag 名 → RoutingSignals の boolean 属性名。
# B2 の DerivedSignal と整合 (kev/zero_day/japan_critical/...) させつつ routing 専用
# flag (confidence_all_low) も含む。"brief_cap_reached" は sq との計算 (下記特別扱い)。
_FLAG_FIELDS: dict[str, str] = {
    "kev": "has_kev_or_active_exploit",
    "zero_day": "is_zero_day",
    "japan_critical": "mentions_japan_critical",
    "japan_relevant": "is_japan_security_relevant",
    "japan_mentioned": "mentions_japan",
    "known_apt": "has_known_apt",
    "apt_leak": "is_apt_leak",
    "security_relevant": "is_security_relevant",
    "confidence_all_low": "confidence_all_low",
    "llm_breaking_critical": "llm_is_breaking_critical",
    "llm_japan_targeted": "llm_japan_targeted",
}

# config 駆動ルールのキャッシュ (process-life、UI 編集後は invalidate)。
_RULES_CACHE: list[dict[str, Any]] | None = None


def load_seed_from_yaml(path: Path = DEFAULT_RULES_PATH) -> list[dict[str, Any]]:
    """config/delivery/routing_rules.yaml (初期 seed) を読む。壊れていれば空 list (fail-safe)。

    DB が正 (config_store) になった後は seed/フォールバック用途のみ。
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules = data.get("rules") if isinstance(data, dict) else None
        return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
    except Exception as e:  # noqa: BLE001 — seed 不在/破損で routing を壊さない
        _log.warning("routing_rules_seed_load_failed", error=str(e), path=str(path))
        return []


def load_routing_rules(
    path: Path = DEFAULT_RULES_PATH,
    *,
    force_reload: bool = False,
) -> list[dict[str, Any]]:
    """routing ルールをロード (cache 付き)。

    **DB (config_store) を正とする**。未保存 (None) のときだけ yaml seed を読む
    (read-only fallback。DB 障害時も seed に degrade)。
    """
    global _RULES_CACHE
    if _RULES_CACHE is not None and not force_reload:
        return _RULES_CACHE
    db_rules: Any = None
    try:
        from src.storage.config_store import get_config

        db_rules = get_config(CONFIG_KEY)
    except Exception as e:  # noqa: BLE001 — DB 障害で routing を壊さない
        _log.warning("routing_rules_db_read_failed", error=str(e))
        db_rules = None
    if isinstance(db_rules, list):
        _RULES_CACHE = [r for r in db_rules if isinstance(r, dict)]
    else:
        _RULES_CACHE = load_seed_from_yaml(path)
    return _RULES_CACHE


def seed_routing_rules_if_absent() -> bool:
    """DB に未投入なら yaml seed を version 1 として取り込む (起動時、full instance)。"""
    try:
        from src.storage.config_store import seed_config_if_absent

        return seed_config_if_absent(
            CONFIG_KEY,
            load_seed_from_yaml(),
            note="初期 seed (config/delivery/routing_rules.yaml)",
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("routing_rules_seed_failed", error=str(e))
        return False


def invalidate_rules_cache() -> None:
    """routing ルール編集後に呼ぶ (UI 保存時)。"""
    global _RULES_CACHE
    _RULES_CACHE = None


def _brief_cap_reached(signals: RoutingSignals, sq: SourceQualityConfig) -> bool:
    """brief 24h cap 到達 (R6.cap_demote 用、現行ロジックと同一)。"""
    return sq.brief_cap_24h > 0 and signals.brief_count_24h_snapshot >= sq.brief_cap_24h


# ===== 語彙統一 (2026-06-14): プロパティ・カタログ (SSoT) =====
# flag/記事属性 の人工的分離を撤廃し、全条件を「型付きプロパティ」に一元化する。
# eval / 検証 / vocab はすべてこのカタログから導出する。flag は boolean プロパティに
# 吸収。詳細: docs/routing_rule_authoring_design.md


@dataclass(frozen=True)
class PropertySpec:
    """条件に使える 1 プロパティ (型 + signal 読取 + 表示メタ)。"""

    id: str
    kind: str  # "boolean" | "str" | "set" | "number"
    group: str
    label: str
    accessor: Callable[[RoutingSignals, SourceQualityConfig], object]
    domain: str = ""  # vocab の値域ソース名 (例: "categories")


# boolean プロパティ (旧 flag) の topic/label。
_BOOLEAN_META: dict[str, tuple[str, str]] = {
    "kev": ("脅威性", "KEV・実環境で悪用中"),
    "zero_day": ("脅威性", "ゼロデイ"),
    "japan_critical": ("日本関連", "日本の重要インフラ標的"),
    "japan_relevant": ("日本関連", "日本関連の脅威"),
    "japan_mentioned": ("日本関連", "日本への言及 (一般)"),
    "known_apt": ("脅威性", "既知 APT への言及"),
    "apt_leak": ("脅威性", "APT 内部情報のリーク"),
    "security_relevant": ("分類", "セキュリティ関連 (広義)"),
    "confidence_all_low": ("品質", "信頼度がすべて低い"),
    "llm_breaking_critical": ("脅威性", "LLM が速報級と判定"),
    "llm_japan_targeted": ("日本関連", "LLM が日本標的と判定"),
}


def _bool_accessor(attr: str) -> Callable[[RoutingSignals, SourceQualityConfig], object]:
    return lambda s, _sq: bool(getattr(s, attr, False))


def _build_property_catalog() -> list[PropertySpec]:
    specs: list[PropertySpec] = []
    # boolean (旧 flag): _FLAG_FIELDS の id→attr から。
    for fid, attr in _FLAG_FIELDS.items():
        group, label = _BOOLEAN_META.get(fid, ("脅威性", fid))
        specs.append(PropertySpec(fid, "boolean", group, label, _bool_accessor(attr)))
    specs.append(
        PropertySpec(
            "brief_cap_reached",
            "boolean",
            "品質",
            "朝刊(brief)が1日の上限に到達",
            lambda s, sq: _brief_cap_reached(s, sq),
        ),
    )
    # str
    specs += [
        PropertySpec(
            "article_type",
            "str",
            "分類",
            "記事の形式",
            lambda s, _sq: s.article_type,
            domain="article_types",
        ),
        PropertySpec(
            "importance",
            "str",
            "深刻度",
            "重要度",
            lambda s, _sq: s.importance,
            domain="importances",
        ),
        PropertySpec(
            "stance", "str", "品質", "論調", lambda s, _sq: s.editorial_stance, domain="stances"
        ),
        PropertySpec(
            "category", "str", "分類", "カテゴリ", lambda s, _sq: s.category, domain="categories"
        ),
        PropertySpec("source", "str", "ソース", "ソース種別", lambda s, _sq: s.source),
        PropertySpec(
            "victim_sector",
            "str",
            "被害",
            "被害セクター",
            lambda s, _sq: s.victim_sector,
            domain="victim_sectors",
        ),
    ]
    # set
    specs += [
        PropertySpec(
            "actor_nation",
            "set",
            "アクター",
            "脅威アクター国家",
            lambda s, _sq: s.threat_actor_nations,
            domain="actor_nations",
        ),
        PropertySpec(
            "keyword_list",
            "set",
            "カスタム",
            "キーワードリスト",
            lambda s, _sq: s.matched_keyword_lists,
            domain="keyword_lists",
        ),
    ]
    # number
    specs += [
        PropertySpec("max_cvss", "number", "深刻度", "CVSS 最大値", lambda s, _sq: s.max_cvss),
    ]
    return specs


PROPERTY_CATALOG: list[PropertySpec] = _build_property_catalog()
PROPERTY_BY_ID: dict[str, PropertySpec] = {p.id: p for p in PROPERTY_CATALOG}

# 型ごとの許容演算子 (順序つき = UI の表示順 / 既定 op)。否定は str/set は ne/not_in、
# boolean は is_false で表す。category のみ in_config (source_quality の list 参照) を追加。
_OPS_BY_KIND: dict[str, list[str]] = {
    "boolean": ["is_true", "is_false"],
    "str": ["eq", "ne", "in", "not_in"],
    "set": ["in", "not_in"],
    "number": ["gte", "gt", "lte", "lt", "eq"],
}


def _ordered_ops(prop: PropertySpec) -> list[str]:
    """プロパティの許容演算子 (順序つき)。category は in_config を末尾に追加。"""
    ops = list(_OPS_BY_KIND.get(prop.kind, []))
    if prop.id == "category":
        ops.append("in_config")
    return ops


def _allowed_ops(prop: PropertySpec) -> frozenset[str]:
    return frozenset(_ordered_ops(prop))


def describe_properties() -> list[dict[str, Any]]:
    """プロパティ・カタログを UI 向けに serialize する (値域 domain 名のみ、値解決は API 層)。

    全条件編集はこのカタログから駆動される (flag/記事属性 の分離撤廃の SSoT)。
    """
    return [
        {
            "id": p.id,
            "kind": p.kind,
            "group": p.group,
            "label": p.label,
            "ops": _ordered_ops(p),
            "domain": p.domain,
        }
        for p in PROPERTY_CATALOG
    ]


def _apply_op(kind: str, actual: object, op: str, value: object, sq: SourceQualityConfig) -> bool:
    """型 × 演算子 × 値 を実際の signal 値に適用 (旧 _match_* を統合)。"""
    if kind == "boolean":
        b = bool(actual)
        if op == "is_true":
            return b
        if op == "is_false":
            return not b
        return False
    if kind == "str":
        v = str(actual or "")
        if op == "eq":
            return v == value
        if op == "ne":
            return v != value
        if op == "in":
            return isinstance(value, list) and v in value
        if op == "not_in":
            return not (isinstance(value, list) and v in value)
        if op == "in_config":
            allowed = getattr(sq, str(value), None)
            if allowed is None:
                _log.warning("routing_rule_unknown_config_list", ref=value)
                return False
            return v in set(allowed)
        return False
    if kind == "set":
        s = actual if isinstance(actual, (set, frozenset)) else frozenset()
        vals = set(value) if isinstance(value, list) else set()
        if op == "in":
            return bool(s & vals)
        if op == "not_in":
            return not (s & vals)
        return False
    if kind == "number":
        try:
            x = float(actual)  # type: ignore[arg-type]
            y = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if op == "gte":
            return x >= y
        if op == "gt":
            return x > y
        if op == "lte":
            return x <= y
        if op == "lt":
            return x < y
        if op == "eq":
            return x == y
        return False
    return False


def _old_leaf_to_new(key: str, spec: Any) -> dict[str, Any]:
    """旧形の葉 (1 述語) を新形 {property,op,value} に変換 (後方互換)。"""
    if key == "flag":
        return {"property": str(spec), "op": "is_true"}
    if isinstance(spec, dict) and spec:
        op = next(iter(spec))
        return {"property": key, "op": op, "value": spec[op]}
    return {"property": key, "op": "eq", "value": spec}


def _normalize_condition(node: Any) -> Any:
    """旧形 (flag/field) を新形 (property/op/value) に正規化 (load/eval 後方互換)。"""
    if not isinstance(node, dict):
        return node
    if "all" in node:
        return {"all": [_normalize_condition(c) for c in node.get("all", [])]}
    if "any" in node:
        return {"any": [_normalize_condition(c) for c in node.get("any", [])]}
    if "not" in node:
        return {"not": _normalize_condition(node["not"])}
    if "always" in node:
        return {"always": bool(node["always"])}
    if "property" in node:
        return node  # 既に新形
    # 旧形の葉 (複数キーは AND)
    leaves = [_old_leaf_to_new(k, v) for k, v in node.items()]
    return leaves[0] if len(leaves) == 1 else {"all": leaves}


def _eval_leaf(node: dict[str, Any], signals: RoutingSignals, sq: SourceQualityConfig) -> bool:
    """新形の葉 {property,op,value} を catalog 駆動で評価。"""
    prop_id = str(node.get("property", ""))
    spec = PROPERTY_BY_ID.get(prop_id)
    if spec is None:
        _log.warning("routing_rule_unknown_property", property=prop_id)
        return False
    return _apply_op(
        spec.kind,
        spec.accessor(signals, sq),
        str(node.get("op", "")),
        node.get("value"),
        sq,
    )


def _eval_condition(
    node: Any,
    signals: RoutingSignals,
    sq: SourceQualityConfig,
) -> bool:
    """when 条件ノードを再帰評価。不明な構造は False (fail-safe)。

    combinator (all/any/not/always) は均一・再帰。葉は新形 {property,op,value}。
    旧形の葉は _normalize_condition で正規化してから評価 (後方互換)。
    """
    if not isinstance(node, dict):
        return False
    if "all" in node:
        return all(_eval_condition(c, signals, sq) for c in node["all"])
    if "any" in node:
        return any(_eval_condition(c, signals, sq) for c in node["any"])
    if "not" in node:
        return not _eval_condition(node["not"], signals, sq)
    if "always" in node:
        return bool(node["always"])
    if "property" in node:
        return _eval_leaf(node, signals, sq)
    # 後方互換: 旧形の葉 → 正規化して再評価
    return _eval_condition(_normalize_condition(node), signals, sq)


# ----- 検証 / 描画 / プレビュー (Stage 2: UI から編集するための支援) -----


# rule が出力できる Discord channel (auto は rule では使わない)。
# C1: 固定 frozenset → channel registry (DB 正) の routable な ch に動的化。
# built-in seed では旧 VALID_CHANNELS と完全同一 (ops は routable=False で除外)。
def valid_channels() -> frozenset[str]:
    from src.tools.channel_registry import routable_channel_ids

    return routable_channel_ids()


# boolean プロパティ id 群 (= 旧 VALID_FLAGS)。vocab API が「flags」として出力する後方互換。
VALID_FLAGS: frozenset[str] = frozenset(p.id for p in PROPERTY_CATALOG if p.kind == "boolean")


def _validate_condition(node: Any, known_categories: set[str], path: str) -> list[str]:
    """when 条件を検証。旧形 (flag/field) は正規化してから catalog 駆動で検証する。"""
    if not isinstance(node, dict):
        return [f"{path}: when 条件は dict である必要があります"]
    if not node:
        return []  # 空 = 全件該当 (catch-all、許容)
    # combinator は再帰、それ以外は新形に正規化して葉検証。
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        spec = node[key]
        if not isinstance(spec, list):
            return [f"{path}.{key}: list が必要"]
        errs: list[str] = []
        for j, child in enumerate(spec):
            errs.extend(_validate_condition(child, known_categories, f"{path}.{key}[{j}]"))
        return errs
    if "not" in node:
        return _validate_condition(node["not"], known_categories, f"{path}.not")
    if "always" in node:
        return [] if isinstance(node["always"], bool) else [f"{path}.always: bool が必要"]
    # 葉: 旧形なら正規化 (複数キー AND は all に展開され再帰)
    leaf = node if "property" in node else _normalize_condition(node)
    if "property" not in leaf:
        return _validate_condition(leaf, known_categories, path)
    return _validate_leaf(leaf, path)


def _validate_leaf(leaf: dict[str, Any], path: str) -> list[str]:
    prop_id = str(leaf.get("property", ""))
    spec = PROPERTY_BY_ID.get(prop_id)
    if spec is None:
        valid = sorted(PROPERTY_BY_ID)
        return [f"{path}: 未知のプロパティ '{prop_id}' (可: {valid})"]
    op = str(leaf.get("op", ""))
    allowed = _allowed_ops(spec)
    if op not in allowed:
        return [f"{path}.{prop_id}: 未知の演算子 '{op}' (可: {sorted(allowed)})"]
    return []


def validate_routing_rules(
    rules: list[dict[str, Any]],
    known_categories: set[str] | None = None,
) -> list[str]:
    """ルールセットを検証しエラー文字列の list を返す (空 = OK)。silent typo を防ぐ。"""
    errs: list[str] = []
    cats = known_categories or set()
    if not isinstance(rules, list) or not rules:
        return ["rules は空でない list である必要があります"]
    routable = valid_channels()
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            errs.append(f"rule[{i}]: dict が必要")
            continue
        rid = r.get("id") or f"#{i}"
        ch = r.get("channel")
        if not ch:
            errs.append(f"rule[{rid}]: channel 必須")
        elif ch not in routable:
            errs.append(f"rule[{rid}]: 未知 channel '{ch}' (可: {sorted(routable)})")
        errs.extend(_validate_condition(r.get("when", {}), cats, f"rule[{rid}]"))
    return errs


# Stage 2 プレビュー用の代表シナリオ (name, RoutingSignals kwargs)。
# 旧 vs 新ルールの挙動差を即座に・決定的に見せる (DB 不要)。
PREVIEW_SCENARIOS: list[tuple[str, dict[str, Any]]] = [
    (
        "日本重要インフラ標的＋既知APT",
        {
            "importance": "high",
            "category": "apt",
            "article_type": "breaking",
            "mentions_japan_critical": True,
            "has_known_apt": True,
            "is_security_relevant": True,
        },
    ),
    (
        "KEV 速報 (実環境悪用)",
        {
            "importance": "high",
            "category": "vulnerability",
            "article_type": "breaking",
            "has_kev_or_active_exploit": True,
            "is_security_relevant": True,
        },
    ),
    (
        "0day 速報",
        {
            "importance": "high",
            "category": "vulnerability",
            "article_type": "breaking",
            "is_zero_day": True,
            "is_security_relevant": True,
        },
    ),
    (
        "APT 内部リーク",
        {
            "importance": "high",
            "category": "apt_leak",
            "article_type": "breaking",
            "is_apt_leak": True,
            "is_security_relevant": True,
        },
    ),
    (
        "高脅威の技術解析 (research形式)",
        {
            "importance": "high",
            "category": "malware",
            "article_type": "research",
            "is_security_relevant": True,
        },
    ),
    (
        "日本関連の弱信号",
        {
            "importance": "medium",
            "category": "incident",
            "article_type": "breaking",
            "is_japan_security_relevant": True,
            "is_security_relevant": True,
        },
    ),
    (
        "中重要度の一般脅威",
        {
            "importance": "medium",
            "category": "incident",
            "article_type": "breaking",
            "is_security_relevant": True,
        },
    ),
    ("週次まとめ (recap)", {"importance": "high", "category": "apt", "article_type": "recap"}),
    ("オピニオン記事", {"importance": "medium", "category": "other", "article_type": "opinion"}),
    (
        "プロパガンダ",
        {
            "importance": "medium",
            "category": "geopolitical",
            "article_type": "breaking",
            "editorial_stance": "propaganda",
            "is_security_relevant": True,
        },
    ),
    (
        "低信頼 (all-low)",
        {
            "importance": "medium",
            "category": "incident",
            "article_type": "breaking",
            "confidence_all_low": True,
            "is_security_relevant": True,
        },
    ),
    (
        "地政学 (非cyber)",
        {
            "importance": "high",
            "category": "geopolitical",
            "article_type": "breaking",
            "is_security_relevant": False,
        },
    ),
    (
        "無関係 (fallback)",
        {
            "importance": "low",
            "category": "other",
            "article_type": "breaking",
            "is_security_relevant": False,
        },
    ),
]


def evaluate_routing_rules(
    signals: RoutingSignals,
    sq: SourceQualityConfig,
    snap: dict[str, object],
    *,
    rules: list[dict[str, Any]] | None = None,
) -> RoutingDecision | None:
    """順序付きルールを first-match 評価して RoutingDecision を返す。

    どのルールにも一致しなければ None (呼び出し側で fallback。通常は R7.fallback が
    always:true で必ず一致する)。
    """
    ruleset = rules if rules is not None else load_routing_rules()
    for rule in ruleset:
        channel = rule.get("channel")
        when = rule.get("when", {})
        if not channel:
            continue
        if _eval_condition(when, signals, sq):
            return RoutingDecision(
                channel=channel,
                rule_id=str(rule.get("id") or "rule"),
                reason=f"rules-engine: {rule.get('id')}",
                signals_snapshot=snap,
            )
    return None

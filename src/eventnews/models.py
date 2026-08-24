"""事象単位ニュースの共有型と定数 (interface pin)。

実装分担 (grouping / identifier_gate / state / generator / repo) はすべて
この module の型で会話する。閾値は docs/event_news_design.md §5/§7/§9 の値。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# ---------- 群化 (§5) ----------

# 結合エッジの埋込コサイン下限
COS_THRESHOLD = 0.70
# 結合信号に使う entity 種別 (actor_provisional は汚染前科のため明示除外)
JOIN_ENTITY_TYPES: tuple[str, ...] = ("cve", "victim_org", "actor", "malware_family")
# entity 値の頻出ガード: 窓内でこれを超える記事に出る値は結合信号に使わない
ENTITY_FREQ_CAP = 12
# 頻出ガードの分母を数える窓。**参加窓 (WINDOW_HOURS) とは独立**に持つ — 二つを
# 束ねると「参加窓を広げると分母も広がって cap に掛かる値が増え、広げた効果が
# 相殺される」という無関係な結合が生まれる。アイテムが成長しうる最長期間
# (dormant 期限) を基準にする。336h と 720h の比較で結果はほぼ不変 (multi 167 vs 165)。
ENTITY_FREQ_WINDOW_HOURS = 14 * 24  # = DORMANT_AFTER_DAYS * 24
# 参加窓: 既存アイテム last_reported_at からの時間 (rolling)
#
# 72h → 168h (2026-08-24)。30 日の実データで窓幅を掃引した結果:
#   窓    multi アイテム  記事被覆
#    24h        108         6.2%
#    72h        167         9.5%
#   168h        204        12.0%
#   336h        218        12.9%
# 168h で頭打ちに近づく (以降 +7%)。168h で初めて成立する群を全件目視したところ、
# すべて同一事象の続報だった (Hugging Face 侵害の 11 日間・VMware 重大脆弱性の
# 日本語媒体による後追い・Jewelbug の追加分析等)。**続報で更新される**ことが
# この機能の主目的なので、精度を落とさずに拾えるなら広げる方が正しい。
WINDOW_HOURS = 168
# アイテムのメンバー上限 (超過は新アイテム「第 2 部」+ related_to)
MEMBER_CAP = 12
# dormant アイテムへの再参加の厳条件
DORMANT_REJOIN_COS = 0.80
DORMANT_REJOIN_SHARED = 2
# 失効: last_reported_at からこの日数で dormant (ENTITY_FREQ_WINDOW_HOURS と同基準)
DORMANT_AFTER_DAYS = 14

# ---------- 生成 (§9) ----------

# プロンプトへ渡すメンバー上限 (tier・時刻で選抜、「他 N 件」を明示)
PROMPT_MEMBER_CAP = 8
# 版の保持上限 (v1 = 初版は常に保持)
VERSION_CAP = 20

# ---------- 状態機械 (§7) ----------

# updated を駆動する entity 種別 (ioc_*/tool/ttp は LLM 揺れで常時 true になるため除外)
UPDATE_DRIVER_TYPES: tuple[str, ...] = ("cve", "victim_org", "actor")


@dataclass(frozen=True)
class MemberArticle:
    """群化・生成の入力となる記事 (畳み込み済み・錨時刻確定済み)。

    anchor_ts は event_time.EVENT_TS_EXPR の錨 (公開時刻、取込で上限、欠損は取込)。
    entities は JOIN_ENTITY_TYPES + 頻出ガード適用済みの (type, 正規化値) 集合。
    victim_org は normalize_for_match で正規化してから入れる (free-form のため)。
    """

    article_id: str
    title: str
    url: str
    feed_title: str
    feed_url: str
    host: str
    importance: str
    category: str
    status: str
    anchor_ts: datetime
    summary: str
    body: str  # 空可 (purge 済み。照合不能は「保持」)
    entities: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class ItemState:
    """既存アイテムの参加判定に必要な最小状態。"""

    item_id: str
    first_reported_at: datetime
    last_reported_at: datetime
    status: str  # 'new' | 'updated' | 'reinforced' | 'dormant'
    importance: str
    current_version: int
    member_ids: tuple[str, ...]


@dataclass(frozen=True)
class Assignment:
    """1 記事の参加判定の結果。target_item_id が None なら新アイテムを起こす。"""

    article_id: str
    target_item_id: str | None
    max_cos: float
    shared_entities: tuple[tuple[str, str], ...]
    # 参加を退けた理由の監査記録 (member_cap / invariant / dormant_strict など)
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceBreakdown:
    """独立媒体数の 3 値 (§8)。未分類を 0 と見せない。"""

    independent: int
    state_media: int
    unclassified: int
    best_tier: str  # source_basis.SourceTier の値


@dataclass(frozen=True)
class StateDecision:
    """新メンバー到着時の決定論判定 (§7)。"""

    kind: str  # 'updated' | 'reinforced'
    change_kind: str | None  # 'add' | 'correct' | None
    reasons: tuple[str, ...]  # 'new_cve' / 'media_increase' / 'tier_rise' / 'importance_rise' ...
    new_facts: dict[str, object] = field(default_factory=dict)  # new_facts_json の中身


# ---------- LLM structured 出力 (§9。int|None 禁止 — 0 が未指定の番兵) ----------


class FactItem(BaseModel):
    """[N] 参照つきの 1 行。source_index は候補一覧の 1-based 番号、0 = 未指定。

    ``paragraph`` は表示層で散文に組むときの段落番号 (1-based)。**保存は 1 行 =
    1 出典のまま**で、読み物としての体裁は表示側が組み立てる (docs/event_news_design.md
    §9 — 出典の検証可能性と可読性を両立させるための分離)。``int | None`` は
    structured 生成で LLM が値を返さなくなるため使わない (2026-08-22 の確立解)。
    """

    model_config = ConfigDict(frozen=True)

    text: str
    source_index: int = 0
    paragraph: int = 1


class EventNewsDraft(BaseModel):
    """LLM の structured 出力。散文は表示層で組む。"""

    model_config = ConfigDict(frozen=True)

    headline: str
    bluf: str
    facts: list[FactItem] = Field(default_factory=list)
    # 相違・不在の主張は [N] を要求しない (関門は識別子のみ適用)
    discrepancies: list[FactItem] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class GateResult:
    """識別子関門 + [N] 関門を通した後の生成物。

    ``repaired_ids`` はカタログ番号 ``{In}`` から実値へ解決した数 (= LLM が転記せず
    番号で書いた数)。``substituted_ids`` は実値直書き かつ カタログ外の厳密型 +
    カタログに無い番号。詳細内訳は ``stats``。
    """

    draft: EventNewsDraft
    dropped_lines: int  # source_index 0/範囲外で落とした facts 行数
    repaired_ids: int  # {In} → 実値に解決した数
    substituted_ids: int  # 「(原文参照)」に置換 + 創作番号の除去
    verified: bool  # 照合を実施できたか
    stats: object | None = None  # identifier_catalog.ResolveStats (詳細内訳)

"""事象単位ニュースの共有型と定数 (interface pin)。

実装分担 (grouping / identifier_gate / state / generator / repo) はすべて
この module の型で会話する。閾値は docs/event_news_design.md §5/§7/§9 の値。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from src.tools.identifier_catalog import ResolveStats
from src.tools.llm_schema import require_all_properties

# ---------- 群化 (§5) ----------

# 結合エッジの埋込コサイン下限
COS_THRESHOLD = 0.70
# 結合信号に使う entity 種別 (actor_provisional は汚染前科のため明示除外)
# tool は 2026-08-31 に追加。動機: 同じアクターの**別作戦**はアクター名しか共有せず、
# それだけで繋ぐと活動が活発な攻撃者ほど 1 事象へ潰れる (実測 Kimsuky で 6 記事が
# 1 つになり、うち 3 件は別作戦だった)。作戦を分けるのは「何を使ったか」で、
# ``AnyDesk + Chrome Remote Desktop`` のような組み合わせが指紋として効く。
# 汎用サービス名 (Dropbox / OneDrive 等) は頻出ガードが落とす — 実測 14 日で
# 252 種のうち cap 超は 3 種のみ、179 種は 1 記事だけ。
JOIN_ENTITY_TYPES: tuple[str, ...] = ("cve", "victim_org", "actor", "malware_family", "tool")
# entity 値の頻出ガード: 窓内でこれを超える記事に出る値は結合信号に使わない
ENTITY_FREQ_CAP = 12
# 頻出ガードを **適用しない** 型。CVE ID は脆弱性 1 件を指す大域一意な識別子で、
# 何媒体が報じても指す対象は変わらない (= 頻度で情報量が薄まらない)。むしろ
# 「よく出る CVE」= 大きな事案であり、まさに束ねたい対象。cap を掛けると
# **大きく報じられた事案ほど群化に失敗する**という逆転が起きる。
# victim_org / actor / malware_family は自由記述または再利用される名前なので
# cap を維持する (別事案どうしを繋いでしまうため)。
FREQ_CAP_EXEMPT_TYPES: frozenset[str] = frozenset({"cve"})
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
# ⚠ メンバー上限 (旧 MEMBER_CAP = 12) は 2026-09-26 に廃止。事象どうしの統合 (eventnews-merge) は
# 上限を見ずに吸収するため、毎時の群化だけが弾いて「リンクの無い新アイテム → 次の統合で吸収」を
# 空回りさせていた (上限超の事象は既に 24 件・最大 51 記事)。巨大化の歯止めは上限でなく、
# ML の合議 (_quorum_blocks) と不変条件 (全メンバーが entity を 1 つ以上共有) が担う。
# dormant アイテムへの再参加の厳条件
# CVE を **主題として** 共有するペアに限って緩める cos 閾値。
#
# 同じ CVE を主題にする記事でも、日本語の短い注意喚起と英語記事では埋込が離れる
# (実測 CVE-2026-73570 のペアで cos=0.512)。一方 ANSSI のような一括アドバイザリは
# 1 記事で最大 278 個の CVE を列挙しており、「CVE を共有すれば繋ぐ」にすると
# 無関係な事案を接着する。そこで **両方の記事が CVE を数個しか持たない**
# (= その CVE が主題) ときだけ閾値を下げる。
#
# 実測 (14 日 / 同一 CVE を共有するペア、2026-08-25):
#   両方 CVE<=1 件: cos>=0.70 が 49.8% / 0.55-0.70 に 35.3% が滞留
#   全ペア        : cos>=0.70 が 27.9% / 中央値 0.612
# 記事あたり CVE 数は 中央値 2 / p75 3 / p90 6 なので、境目は p75 に置く。
FOCAL_CVE_MAX = 3
FOCAL_CVE_COS = 0.55

# 特異な名前を複数共有するときの緩和 (2026-08-31)。焦点 CVE の例外と同じ発想を、
# CVE 以外の結合信号へ広げたもの。
#
# ⭐ 動機: 同一事象なのに cos が僅かに届かず割れる。実測 (2026-08-31、直近 14 日の
# 単独事象ペア 783 組) では 0.60-0.70 の帯に 128 組が滞留しており、報告のあった
# Fire Ant の 2 記事 (Sygnia の技術ブログとプレスリリース) は **cos=0.6915** で
# 0.0085 足りずに割れていた。Cisco Crosswork は 4 事象に割れていた。
#
# ⚠ **名前の「数」で数える。entity の数ではない。** 同じ名前が actor と
# malware_family の両方に出ると (kimsuky が実例)、1 つの根拠が 2 つに見える。
# 素の共有数で緩めると Kimsuky の別キャンペーン 2 件が誤って繋がった。
#
# ⚠ **1 名だけの共有では緩めない。** ランサム流出サイトの投稿は「同じグループ・
# 別の被害者」で actor 1 名を共有し、cos も 0.68-0.70 に来る (実測 Dark Project /
# Play / Arcusmedia)。全体の閾値を 0.68 へ下げる案がこれで却下になった。
#
# 実測 (この規則で新たに繋がる組、14 日): 16 組 / 標本 12 組を目視して 11 組が同一事象。
SHARED_NAMES_MIN = 2
SHARED_NAMES_COS = 0.62

# 本文の節。**この語彙だけ**を LLM に選ばせ、表示名は frontend が解決する。
# 対象がサイバー全般 (脆弱性・侵害・マルウェア・地政学) なので、該当が無い節は
# 出さない前提 (地政学記事に「利用者が取るべき対応」は無い)。
SECTION_KEYS: tuple[str, ...] = (
    "what",  # 何が起きたか
    "scope",  # 影響範囲
    "how",  # 攻撃の手口
    "response",  # 対応・緩和
    "context",  # 背景・経緯
    "action",  # 利用者が取るべき対応
)

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
    # 発信者種別 (Grok/X の account_class)。tier 判定に使う (空 = 未分類 / 非 X)
    account_class: str = ""


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


# 全必須化は structured 出力共通の手当 (2026-09-15 に src/tools/llm_schema.py へ移設 —
# synthesis render も同じ罠を踏んでいたため 1 箇所に集約した)。
_require_all_properties = require_all_properties


# 配列の上限 — **文法に閉じを強制させる** (2026-09-20)。
# 上限が宣言されていなければ、構造化出力の文法は要素をいくらでも許す。要素ごとに
# 「続ける / 閉じる」の賭けを繰り返すので、確率がわずかに偏るだけで長い連続が出る
# (反復の自己強化: Fu ら 2021、DITTO)。実例: 1 窓で facts 84 件 (うち 64 件が重複)。
# Ollama は maxItems を文法へコンパイルするので、モデルが続けたくても閉じる (実機確認済)。
#
# ⚠⚠ **上限は暴走を止めるが重複は止めない** (上限 8 でも同じ行を 8 個出せる)。
# ⚠⚠ **超過を例外にしてはいけない**。文法が守るのは生成時だけで、JSON 修復経路・
#     外部 LLM・保存済みデータからは超過が届く。pydantic の max_length は検証なので
#     本番が落ちる (2026-09-20 に既存テスト 2 件がこれで落ちて気付いた) → json_schema_extra
#     で schema にだけ出す。
# ⚠ **切り捨ては model でやらない**。`list_dedup.dedup_draft` が重複を畳む**前**に切ると、
#     後ろの正当な要素が消える (「u1 × 300 + u2」が「u1」だけになる)。畳んだ後に切る。
# uniqueItems は文脈自由文法で表現できず原理的に不可 — 重複は別の seam で落とす。
#
# 値は**重複のない 155 窓の実測**から置く (正当な出力を切らない余裕を取る):
#   facts 中央 8 / 99% 34 / 最大 49 → 60   相違 99% 4 → 12   unknowns 99% 9 → 20
KEY_POINTS_MAX = 12
FACTS_MAX = 60
DISCREPANCIES_MAX = 12
CAVEATS_MAX = 12
UNKNOWNS_MAX = 20


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
    # 節の種類 (SECTION_KEYS のいずれか)。**表示名はコード側が持つ** —
    # 自由記述の見出しを許すと表記が揺れる (ラベルは SSoT を参照、CLAUDE.md §7)。
    # 既定は "what"。未知の値は表示側が「前の節の続き」として扱う (勝手に節を作らない)。
    section: str = "what"

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return _require_all_properties(dict(handler(core_schema)))


class EventNewsDraft(BaseModel):
    """LLM の structured 出力。散文は表示層で組む。"""

    model_config = ConfigDict(frozen=True)

    headline: str
    bluf: str
    # 冒頭に置く **要点** (箇条書き 3-4 項目)。BLUF は一覧のプレビュー用に残す
    # (箇条書きは一覧に向かない)。要点は出典番号を持たない — 本文の事実行と違い
    # 「1 文 = 1 事実 = 1 出典」の検証単位ではないため。
    key_points: list[str] = Field(
        default_factory=list, json_schema_extra={"maxItems": KEY_POINTS_MAX}
    )
    facts: list[FactItem] = Field(default_factory=list, json_schema_extra={"maxItems": FACTS_MAX})
    # 相違・不在の主張は [N] を要求しない (関門は識別子のみ適用)
    discrepancies: list[FactItem] = Field(
        default_factory=list, json_schema_extra={"maxItems": DISCREPANCIES_MAX}
    )
    # 原文が自ら付けた但し書き。**要約すると最初に落ちる種類の情報**で、落ちると
    # 読み手が数字を誤読する。実測 (2026-08-26、生成済み 196 件): 留保を示す語の
    # 密度は 31B が Sonnet の 55% しかなく、「相違」欄に至っては 0.1 対 0.7 だった。
    # 例: 原文「週 80 件超は公開サンドボックスへの投稿数であり被害組織数とは別の指標」
    # → 生成が「週 80 件超が確認された」だけになると、観測量が被害規模に化ける。
    # 散文の指示では 2 度とも効かなかったため、**構造 (独立した欄) で保持させる**。
    caveats: list[FactItem] = Field(
        default_factory=list, json_schema_extra={"maxItems": CAVEATS_MAX}
    )
    unknowns: list[str] = Field(default_factory=list, json_schema_extra={"maxItems": UNKNOWNS_MAX})

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return _require_all_properties(dict(handler(core_schema)))


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
    stats: ResolveStats | None = None  # 解決の詳細内訳

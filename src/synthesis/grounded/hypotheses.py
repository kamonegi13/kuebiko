"""ACH 用 canonical 仮説メニュー (固定の分析フレームワーク・ドメイン別)。

事案の「帰属・性質・戦略的意図」に関する競合仮説の固定語彙。LLM が自作するワラ人形でなく、
このメニューから関連仮説を立て ACH (競合仮説分析) で反証採点する。

**ドメイン別**: サイバー事案は「組織的か/日和見か/偶発か」(帰属・性質) を、地政学/政策事案は
「強要か/抑止か/対抗か/通常措置か/誇張か」(戦略的意図・framing 信頼性) を競わせる。輸出規制等の
政策行動をサイバー作戦仮説 (organized_state_op) に押し込めないため (現行 intent 分類の弁別子)。
固定 taxonomy ゆえコード SSoT (SocioPoliticalIntent enum や PROPERTY_CATALOG と同思想)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hypothesis:
    """1 つの競合仮説。supports/refutes は ACH プロンプトに注入する典型証拠ヒント。"""

    id: str
    label: str
    description: str
    supports: str
    refutes: str


# ---- サイバー事案: 帰属・性質 ----
CYBER_CORE: tuple[Hypothesis, ...] = (
    Hypothesis(
        "organized_state_op",
        "組織的国家作戦",
        "国家が指示/実行する標的型作戦 (諜報・事前配置・破壊)。",
        "公式/ベンダによる標的帰属、被害組織の特定的な選定、C2 や窃取の実証、TTP の一貫性。",
        "コモディティツールの広域流通、標的選定の不在、窃取・外部通信の証拠なし、帰属がツール類似のみ。",
    ),
    Hypothesis(
        "opportunistic_commodity",
        # 「標的型」の対概念として日本の CTI 実務 (IPA/JPCERT) で定着した「ばらまき型」を使う。
        "ばらまき型",
        "市販・流通する汎用 malware/tooling が特定組織を狙わず無差別 (日和見的) に展開された事象。",
        "市販/偽造品の広域流通、無差別拡散、複数の無関係組織での同時観測。",
        "特定組織への精密な標的選定、カスタムツール、長期の潜伏作戦。",
    ),
    Hypothesis(
        "criminal_financial",
        "金銭目的犯罪",
        "金銭を主目的とするサイバー犯罪 (ランサム・窃盗・詐欺)。",
        "身代金要求、暗号資産窃取、リークサイト掲載、金銭的動機の明示。",
        "金銭要求の不在、機密情報のみを標的、国家戦略目標との一致。",
    ),
    Hypothesis(
        "accidental_negligence",
        "偶発・過失",
        "誤設定・供給網汚染・運用過失が原因の事象 (攻撃意図なし or 二次的)。",
        "調達/設定/管理の不備の明示、検知後の自己申告、攻撃者の能動性の欠如。",
        "意図的侵入の証拠、能動的な横展開/窃取、標的型の初期アクセス。",
    ),
    Hypothesis(
        "hacktivism_influence",
        "主義主張・影響工作",
        "イデオロギー/抗議/認知戦を動機とする活動。",
        "声明・主張の公開、政治的タイミング、defacement/DDoS/情報操作。",
        "秘匿性の高い長期作戦、金銭/諜報目的、主張の不在。",
    ),
)

# ---- 地政学/政策事案: 戦略的意図・framing 信頼性 ----
GEO_CORE: tuple[Hypothesis, ...] = (
    Hypothesis(
        "strategic_coercion",
        "戦略的威圧",
        "標的の行動/政策を変えさせる意図的圧力 (制裁・輸出規制・経済/外交圧力)。",
        "圧力の目的の明示表明、標的の特定政策への言及、報復予告、標的の戦略的選定。",
        "定期更新・無差別適用、相手の行動と無関係、技術的/事務的性格。",
    ),
    Hypothesis(
        "deterrence_signaling",
        "抑止シグナリング",
        "能力/決意を誇示し相手の行動を思いとどまらせる (防御的・compel でなく止めさせる)。",
        "防御的文脈、能力誇示、エスカレーション回避の言及。",
        "能動的に行動変更を迫る、攻撃的目的の明示。",
    ),
    Hypothesis(
        "reciprocal_response",
        "対抗・報復",
        "他者の先行行為への対抗措置/報復 (起点でなく応答)。",
        "先行する相手の措置への明示言及、「対抗措置」「報復」表現、時系列で後続。",
        "先行行為の不在、一方的な起点、相手の行動と無関係。",
    ),
    Hypothesis(
        "routine_or_administrative",
        "通常・行政措置",
        "通常の政策/規制/行政措置 (戦略的エスカレーションでない)。",
        "定期的手続き、技術的・事務的性格、戦略的文脈の欠如、慣例。",
        "異例の規模/タイミング、標的の戦略的選定、政治的言明、報復文脈。",
    ),
    Hypothesis(
        "territorial_assertion",
        "領土・主権主張",
        "領土/主権/係争海域の主張・越境・実効支配の試み。",
        "領土・主権・境界への言及、越境/示威の物理行動。",
        "領土と無関係、経済/サイバーのみの手段。",
    ),
    Hypothesis(
        "domestic_political",
        "国内政治起因",
        "国内政治/正統性が主因の行動 (対外側面は副次・口実)。",
        "国内世論/選挙/体制安定への言及、対外口実の性格。",
        "明確な対外標的、国際的調整、国内文脈の欠如。",
    ),
    Hypothesis(
        "propaganda_or_overstated",
        "誇張・宣伝",
        "戦略的意義が国営/メディアの framing で誇張され実態と乖離。",
        "国営メディア主体、検証可能な実体の乏しさ、誇張的修辞、単一 framing。",
        "一次資料/公式文書の裏付け、独立した確認、検証可能な実体。",
    ),
)

# ---- 常設情報要求 (standing prepositioning posture): 現在の posture の競合仮説 ----
# 「国家 N は日本の重要インフラへの事前配置を進めているか」への ACH フレーム (設計
# docs/prepositioning_posture_ledger_design.md §4.1)。方向中立: 脅威過大 (H-P1 過確信)
# も穏当過小 (観測ゼロ=H-P3 支持) も禁忌。
POSTURE_CORE: tuple[Hypothesis, ...] = (
    Hypothesis(
        "posture_active_prepositioning_jp",
        "日本CIへの事前配置が進行中",
        "当該国家のアクターが日本の重要インフラへの足場確保 (事前配置) を進めている。",
        "日本の CI 事業者/分野での帰属済み観測、公的勧告の日本名指し、JP 標的の潜伏・LOTL 活動。",
        "日本での帰属済み観測の不在が継続、活動が他地域限定、観測の性質が窃取/収益で完結。",
    ),
    Hypothesis(
        "posture_global_no_jp_evidence",
        "世界的に活動・日本標的の直接証拠なし",
        "当該国家は他国 CI への事前配置を進めているが、日本標的の直接証拠は現時点で無い"
        " (doctrine 上のリスクは残る — 観測の不在は不在の証明ではない)。",
        "同盟国 (US/TW/KR 等) CI での帰属済み事前配置、公的勧告、日本観測の不在。",
        "日本での直接観測の出現、当該国の CI 標的活動の全面的不在。",
    ),
    Hypothesis(
        "posture_other_motive",
        "観測活動は別動機",
        "観測されている当該国活動は諜報・金銭等が目的で、事前配置と評価する根拠がない。",
        "窃取・収益化の実証で活動が完結、OT/境界系への関心の不在、標的が CI 外。",
        "CI の OT/境界系への関心、実害なき長期潜伏の実証、有事対応系の標的選定。",
    ),
)

# ---- 全ドメイン共通 ----
SHARED: tuple[Hypothesis, ...] = (
    Hypothesis(
        "reporting_artifact",
        "報道アーティファクト",
        "新規活動でなく、旧事案の表面化・再報道・分析公開。",
        "過去事案の振り返り、検知能力向上による遡及把握、分析レポートの公開。",
        "新規の初期アクセス/被害/行動の発生、リアルタイムの活動継続。",
    ),
    Hypothesis(
        "unverified_or_false",
        "未実証・虚偽",
        "主張が実証されていない / 国営 framing / 噂レベル。",
        "単一・低信頼ソース、国営メディアの framing、裏取りの不在、推測語。",
        "複数の独立 official/research による裏取り、一次証拠の存在。",
    ),
)

CYBER_HYPOTHESES: tuple[Hypothesis, ...] = CYBER_CORE + SHARED
GEO_HYPOTHESES: tuple[Hypothesis, ...] = GEO_CORE + SHARED
# standing (常設情報要求) 専用: 呼出側が hypotheses_override で明示指定する (domain 選択外)。
POSTURE_HYPOTHESES: tuple[Hypothesis, ...] = POSTURE_CORE + SHARED
# 全仮説 (id 解決・is_known 用)。POSTURE は event の domain 選択には出さない (下記 union)。
HYPOTHESIS_MENU: tuple[Hypothesis, ...] = CYBER_CORE + GEO_CORE + POSTURE_CORE + SHARED
# event 用 domain 不明時の union (POSTURE を含めない — 常設専用フレームの漏出防止)
_EVENT_UNION: tuple[Hypothesis, ...] = CYBER_CORE + GEO_CORE + SHARED

_BY_ID: dict[str, Hypothesis] = {h.id: h for h in HYPOTHESIS_MENU}

# domain → 使う仮説セット (cyber=帰属/性質、geo=戦略的意図、不明=union)。
_CYBER_DOMAINS: frozenset[str] = frozenset(
    {"cyber", "cyber_incident", "infrastructure", "tech", "technology", "information"}
)
_GEO_DOMAINS: frozenset[str] = frozenset(
    {"geopolitical", "political", "military", "economic", "social", "diplomatic", "policy"}
)


def hypotheses_for_domain(domain: str) -> tuple[Hypothesis, ...]:
    """nominate の domain から ACH に使う仮説セットを選ぶ。

    サイバー事案 → 帰属/性質セット、地政学/政策事案 → 戦略的意図セット、不明 → union
    (LLM が applicable を選択)。部分一致でも判定 ("cyber" を含む等)。
    """
    d = domain.strip().lower()
    if d in _CYBER_DOMAINS or "cyber" in d:
        return CYBER_HYPOTHESES
    if d in _GEO_DOMAINS or "geo" in d:
        return GEO_HYPOTHESES
    return _EVENT_UNION  # 不明 → event 用全仮説から LLM が applicable を選ぶ


def get_hypothesis(hid: str) -> Hypothesis | None:
    return _BY_ID.get(hid)


def hypothesis_ids() -> tuple[str, ...]:
    return tuple(h.id for h in HYPOTHESIS_MENU)


def is_known_hypothesis(hid: str) -> bool:
    return hid in _BY_ID

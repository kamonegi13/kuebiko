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

# ---- 型 E 趨勢 (2026-09-16): 「〜は悪化しているか」への ACH フレーム ----
# 設計 docs/pir_brief_design.md §6c。**方向中立** — 悪化だけを置くと脅威過大に倒れる
# (POSTURE_CORE と同じ規律)。観測の変化を一級の競合仮説として必ず競わせる
# (CLAUDE.md §7「収集量を重要性の代理にしない」が直撃する型のため)。
TREND_CORE: tuple[Hypothesis, ...] = (
    Hypothesis(
        "trend_worsening",
        "悪化している",
        "当該対象への当該脅威は、比較期間に対して実態として強まっている。",
        "同一フィードコホート上の構成比の上昇、被害の重篤化、新規標的分野への拡大、"
        "独立した複数の一次観測。",
        "構成比が横ばい、上昇が単一事案の連続報道に由来、母集団が薄い。",
    ),
    Hypothesis(
        "trend_flat",
        "横ばい (変動の範囲内)",
        "増減は見られるが、過去の変動幅の内側で、趨勢と呼べる変化ではない。",
        "構成比の変化が小さい、過去窓でも同程度の揺れがある、母集団が薄い。",
        "過去の変動幅を明確に超える構成比の移動が複数期間続く。",
    ),
    Hypothesis(
        "trend_improving",
        "改善している",
        "当該脅威は実態として弱まっている (対処の奏功・アクターの関心移動等)。",
        "構成比の低下、既知アクターの活動停止・摘発、対処策の普及。",
        "低下が観測の縮小で説明できる、他分野への移動にすぎない。",
    ),
    Hypothesis(
        "trend_observation_change",
        "観測の変化 (実態でなく集計の歪み)",
        "**集計側**の変化で増減して見えている。収集網の増減、あるいは世界の報道量の変動。"
        " (個別主張の再報道は `reporting_artifact` が担う — こちらは集計全体の歪み)",
        "比較期間でフィード構成が変わった、コホートの記事占有率が低い、母集団が薄い、"
        "同一コホートに限ると差が消える。",
        "同一コホート・十分な母集団の上でも構成比が動いている。",
    ),
)

# ---- 型 H 閾値・段階 (2026-09-16): 「〜は平時の水準を越えたか」への ACH フレーム ----
# E (傾き) と対。決心 (警報を出すか・態勢を上げるか) に直結する。
# **帰無仮説は「平時の変動内」** — 越えたと言うには証拠が要る (fail-closed)。
THRESHOLD_CORE: tuple[Hypothesis, ...] = (
    Hypothesis(
        "threshold_within_normal",
        "平時の変動内",
        "観測されている活動は、当該主体の平常の活動水準の内側にある (既定の見立て)。",
        "手口・標的選定が従来の範囲、構成比が過去の揺れの内側、質的な新しさの不在。",
        "従来見られなかった種類の活動、標的分野の質的な拡大、作戦テンポの明確な変化。",
    ),
    Hypothesis(
        "threshold_crossed",
        "段階が上がった",
        "活動が平時の水準を越え、質的に異なる段階に入っている"
        " (偵察から足場確保へ、あるいは足場確保から影響行使へ等)。",
        "従来と異なる種類の行為の出現、OT・境界系など新分野への到達、"
        "複数の独立観測が同時期に集中、公的機関の警戒度の変更。",
        "変化が単一事案・単一ソースに依存、従来手口の範囲内、観測側の変化で説明可能。",
    ),
    Hypothesis(
        "threshold_third_party",
        "第三者・便乗",
        "水準の上昇に見えるものは、当該主体ではない別アクター (非国家・便乗犯・"
        "自称勢力) の活動が混ざった結果である。",
        "帰属の不在または弱さ、自称のみ、手口の不一致、金銭目的の痕跡。",
        "辞書ゲート済みの帰属、当該主体の既知インフラ・手口との一致。",
    ),
    Hypothesis(
        "threshold_detection_improved",
        "検知能力の向上 (前からあったものが見えた)",
        "活動の水準は変わっておらず、**我々または報告者の検知能力が上がった**ために"
        " いま見えている。",
        "新しい検知手法・調査公開の直後に集中、遡及的な過去事案の発見、"
        "同一コホートでは構成比が動いていない。",
        "リアルタイムの新規活動として観測、検知手法に変化がない期間での増加。",
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
# ---- 型 X: エスカレーション軌道 (2026-09-19) ----
# 決心「次の段階に備えるか」を支える。閾値 (H) が線を越えたかを問うのに対し、X は
# **応酬が次の段階へ進むか**を問う。積み重ね型の情勢 (米イラン / ロシア-NATO) 用。
ESCALATION_CORE: tuple[Hypothesis, ...] = (
    Hypothesis(
        "escalation_contained",
        "現段階に留まる",
        "応酬は現在の段階 (経済・情報・限定的な実力行使) の内側に留まる (既定の見立て)。",
        "双方の行為が既存の手段の範囲、対話・仲介の経路が維持、報復が対称的で限定的。",
        "新しい手段の投入、標的分野の質的拡大、仲介経路の断絶。",
    ),
    Hypothesis(
        "escalation_advancing",
        "次の段階へ進む",
        "応酬が質的に新しい段階へ移行しつつある (経済→軍事、代理→直接、限定→全面等)。",
        "従来用いなかった手段の投入、直接的な実力行使、動員・配備の変化、"
        "複数の独立観測が同時期に集中。",
        "変化が単一事案・単一ソースに依存、レトリックのみで実行が伴わない。",
    ),
    Hypothesis(
        "escalation_deescalating",
        "収束へ向かう",
        "応酬は収束方向にあり、次の段階へは進まない。",
        "停戦・交渉の進展、報復の見送り、第三国の仲介の成立。",
        "交渉と並行した実力行使の継続、合意の不履行。",
    ),
    Hypothesis(
        "escalation_signaling",
        "示威にとどまる",
        "観測される動きは実行意図の伴わない示威・抑止の信号である。",
        "公開の場での誇示、実害を避けた標的選定、事前の予告。",
        "秘匿された準備、実害の発生、否認の試み。",
    ),
)

# ---- 型 P: 波及 (2026-09-19) ----
# 決心「自国の産業・供給網に手当てするか」を支える。他国で始まった政策・規制・侵害が
# 日本へ及ぶかを問う。積み重ね型 (MATCH 法 / 中国の海外港湾) 用。
PROPAGATION_CORE: tuple[Hypothesis, ...] = (
    Hypothesis(
        "propagation_not_reached",
        "未到達",
        "当該の政策・事象は対象国・対象分野へまだ及んでいない (既定の見立て)。",
        "対象国の事業者・制度への言及の不在、適用範囲の明示的な限定。",
        "対象国の事業者名・制度名の出現、適用範囲の拡大の公表。",
    ),
    Hypothesis(
        "propagation_reaching",
        "波及しつつある",
        "対象国・対象分野へ及び始めている。",
        "対象国の事業者・製品・制度への具体的な言及、当局の対応の公表、"
        "業界団体の反応、複数の独立報道。",
        "言及が推測・論説にとどまる、当局の否定、適用除外の明示。",
    ),
    Hypothesis(
        "propagation_blocked",
        "遮断される",
        "制度的・技術的な理由で対象国へは及ばない。",
        "適用除外の明示、代替供給の確立、対抗措置の成立。",
        "除外の撤回、代替の不成立。",
    ),
)

POSTURE_HYPOTHESES: tuple[Hypothesis, ...] = POSTURE_CORE + SHARED
TREND_HYPOTHESES: tuple[Hypothesis, ...] = TREND_CORE + SHARED
THRESHOLD_HYPOTHESES: tuple[Hypothesis, ...] = THRESHOLD_CORE + SHARED
ESCALATION_HYPOTHESES: tuple[Hypothesis, ...] = ESCALATION_CORE + SHARED
PROPAGATION_HYPOTHESES: tuple[Hypothesis, ...] = PROPAGATION_CORE + SHARED
# 全仮説 (id 解決・is_known 用)。POSTURE は event の domain 選択には出さない (下記 union)。
HYPOTHESIS_MENU: tuple[Hypothesis, ...] = (
    CYBER_CORE
    + GEO_CORE
    + POSTURE_CORE
    + TREND_CORE
    + THRESHOLD_CORE
    + ESCALATION_CORE
    + PROPAGATION_CORE
    + SHARED
)
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

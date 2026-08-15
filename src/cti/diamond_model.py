"""Diamond Model の 2 つの meta-feature 軸 (Phase Diamond-Axes, 2026-06-06)。

Diamond Model (Caltagirone+ 2013) は侵入分析を 4 頂点
``Adversary / Capability / Infrastructure / Victim`` で表す。本 pipeline は
既にこの 4 頂点を抽出・永続化している:

    - Adversary       → primary_actor_id + actor_normalizer
    - Capability      → mitre_techniques + malware_families + tools
    - Infrastructure  → iocs (ip / domain / hash / url)
    - Victim          → victim_sector + victim_country (taxonomy_normalizer)

本モジュールが追加するのは、原典が定義する **2 つの meta-feature 軸**:

    - **Socio-Political axis** (Adversary ⇄ Victim): 攻撃者が被害者に対して
      持つ **意図/動機**。集計可能な closed enum (将来予測の材料)。
    - **Technical axis** (Capability ⇄ Infrastructure): Capability が
      Infrastructure をどう用いるかの **技術的結線** (短い narrative)。

設計方針:
    - intent は editorial_stance と同じ **閉じた語彙** のため、開語彙の sector
      (taxonomy_normalizer の yaml) と違いモジュール定数で持つ (KISS)。
    - LLM が ``briefing/summarizer.j2`` の同一パスで 4 頂点と同時に 2 軸を要約する
      (per-article の追加 LLM 呼び出しは無し)。
    - 欠落 / 不正値に強い防御的パーサ (parse_routing_flags と同形式)。
    - intent → STIX ``attack-motivation-ov`` への近似クロスウォークを提供
      (STIX export で threat-actor.primary_motivation に使う)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]

# ---------- Socio-Political axis (Adversary ⇄ Victim = 意図/動機) ----------

# 統一 strategic-intent 軸 (Phase Geopolitical-Intent, 2026-06-21)。
# サイバー動機 (espionage..hacktivism) と地政学/国家動機 (coercion..diplomacy) を 1 軸で持つ。
# PMESII (どの領域か = 分類) とは **直交** する「何を達成しようとしているか = 動機」。
# 直交性ゆえ「領域で表せる動機」(制裁=coercion×経済 / 軍事示威=coercion×軍事) は値にしない。
SocioPoliticalIntent = Literal[
    # --- サイバー寄り (両用) ---
    "espionage",  # 諜報・情報窃取 (国家系 APT の主目的)
    "financial",  # 金銭目的 (ransomware / 窃取 / 詐欺)
    "prepositioning",  # 事前配置 (重要インフラへの足場確保。Volt Typhoon 型)
    "disruption",  # 破壊・妨害 (機能を損なうこと自体が目的。wiper / sabotage / DDoS)
    "influence",  # 影響工作・認知戦 (認識・世論を変える。InfoOps / disinformation)
    "hacktivism",  # 主義主張・抗議活動
    # --- 地政学/国家 動機 ---
    "coercion",  # 威圧・強要 (行動・政策を変えさせる。制裁 / 示威 / グレーゾーン)
    "deterrence",  # 抑止 (行動を思いとどまらせる。防御的シグナリング)
    "territorial",  # 領土・主権 (領土/主権/係争海域の主張・奪取・越境)
    "subversion",  # 体制動揺・転覆 (内部の安定・正統性を内側から崩す。代理 / 騒擾扇動)
    "diplomacy",  # 外交・同盟 (同盟 / 条約 / 正常化 / 連携の協調行動)
    "unknown",  # 判定不能
]

# canonical intent の集合 (検証用)。
SOCIO_POLITICAL_INTENTS: frozenset[str] = frozenset(
    {
        "espionage",
        "financial",
        "prepositioning",
        "disruption",
        "influence",
        "hacktivism",
        "coercion",
        "deterrence",
        "territorial",
        "subversion",
        "diplomacy",
        "unknown",
    },
)

# 日本語表示ラベル (UI / Discord 表示用)。
INTENT_LABELS_JA: dict[str, str] = {
    "espionage": "諜報・情報窃取",
    "financial": "金銭目的",
    "prepositioning": "事前配置",
    "disruption": "破壊・妨害",
    "influence": "影響工作・認知戦",
    "hacktivism": "ハクティビズム",
    "coercion": "威圧・強要",
    "deterrence": "抑止",
    "territorial": "領土・主権",
    "subversion": "体制動揺・転覆",
    "diplomacy": "外交・同盟",
    "unknown": "不明",
}

# actor 辞書 nation コード (小文字) → 日本語ラベル。overview / geo_cyber_map /
# standing_posture が共有する SSoT (有機的結合監査 M3: 4 箇所複製と収録国差異の解消)。
# victim 国 (108 ヶ国) の SSoT は config/cti/countries.yaml で別物 — こちらは actor 帰属国のみ。
NATION_LABELS_JA: dict[str, str] = {
    "cn": "中国",
    "ru": "ロシア",
    "kp": "北朝鮮",
    "ir": "イラン",
    "us": "米国",
    "kr": "韓国",
    "il": "イスラエル",
    "vn": "ベトナム",
    "in": "インド",
    "gb": "英国",
    "pk": "パキスタン",
}

# チップ/バッジ用の短縮ラベル SSoT (frontend/src/utils/diamond.ts の INTENT_META と対)。
# 有機的結合監査 M3: jp_ci_board「事前潜伏」/「工作」等の方言を排除し、短縮形もここに一本化。
INTENT_LABELS_JA_SHORT: dict[str, str] = {
    "espionage": "諜報",
    "financial": "金銭",
    "prepositioning": "事前配置",
    "disruption": "破壊",
    "influence": "影響工作",
    "hacktivism": "ハクティビズム",
    "coercion": "威圧",
    "deterrence": "抑止",
    "territorial": "領土",
    "subversion": "体制転覆",
    "diplomacy": "外交",
    "unknown": "不明",
}

# LLM が canonical 以外の語を返したときの alias 吸収辞書 (lower-case key)。
# Recall 重視: 表記ゆれ・同義語を canonical に寄せ、未知語のみ unknown へ倒す。
_INTENT_ALIASES: dict[str, str] = {
    "espionage": "espionage",
    "spying": "espionage",
    "cyberespionage": "espionage",
    "cyber-espionage": "espionage",
    "intelligence": "espionage",
    "intelligence-gathering": "espionage",
    "data-theft": "espionage",
    "information-theft": "espionage",
    "surveillance": "espionage",
    "諜報": "espionage",
    "情報窃取": "espionage",
    "financial": "financial",
    "financial-gain": "financial",
    "financially-motivated": "financial",
    "monetary": "financial",
    "ransomware": "financial",
    "extortion": "financial",
    "fraud": "financial",
    "theft": "financial",
    "金銭": "financial",
    "金銭目的": "financial",
    "prepositioning": "prepositioning",
    "pre-positioning": "prepositioning",
    "preposition": "prepositioning",
    "foothold": "prepositioning",
    "persistence": "prepositioning",
    "staging": "prepositioning",
    "事前配置": "prepositioning",
    "disruption": "disruption",
    "disruptive": "disruption",
    "destruction": "disruption",
    "destructive": "disruption",
    "sabotage": "disruption",
    "wiper": "disruption",
    "denial-of-service": "disruption",
    "dos": "disruption",
    "ddos": "disruption",
    "破壊": "disruption",
    "妨害": "disruption",
    "influence": "influence",
    "influence-operation": "influence",
    "influence-operations": "influence",
    "information-operations": "influence",
    "infoops": "influence",
    "disinformation": "influence",
    "misinformation": "influence",
    "propaganda": "influence",
    "cognitive": "influence",
    "影響工作": "influence",
    "認知戦": "influence",
    "hacktivism": "hacktivism",
    "hacktivist": "hacktivism",
    "activism": "hacktivism",
    "ideological": "hacktivism",
    "protest": "hacktivism",
    "ハクティビズム": "hacktivism",
    # --- 地政学/国家 動機 (Phase Geopolitical-Intent) ---
    "coercion": "coercion",
    "coerce": "coercion",
    "compellence": "coercion",
    "compel": "coercion",
    "pressure": "coercion",
    "intimidation": "coercion",
    "gray-zone": "coercion",
    "grayzone": "coercion",
    "sanctions": "coercion",
    "威圧": "coercion",
    "強要": "coercion",
    "恫喝": "coercion",
    "deterrence": "deterrence",
    "deter": "deterrence",
    "deterrent": "deterrence",
    "抑止": "deterrence",
    "territorial": "territorial",
    "territory": "territorial",
    "sovereignty": "territorial",
    "irredentism": "territorial",
    "annexation": "territorial",
    "incursion": "territorial",
    "領土": "territorial",
    "主権": "territorial",
    "越境": "territorial",
    "subversion": "subversion",
    "subvert": "subversion",
    "destabilization": "subversion",
    "destabilize": "subversion",
    "destabilisation": "subversion",
    "regime-change": "subversion",
    "insurgency": "subversion",
    "proxy": "subversion",
    "転覆": "subversion",
    "体制転覆": "subversion",
    "扇動": "subversion",
    "diplomacy": "diplomacy",
    "diplomatic": "diplomacy",
    "alliance": "diplomacy",
    "alignment": "diplomacy",
    "treaty": "diplomacy",
    "normalization": "diplomacy",
    "normalisation": "diplomacy",
    "cooperation": "diplomacy",
    "coalition": "diplomacy",
    "外交": "diplomacy",
    "同盟": "diplomacy",
    "連携": "diplomacy",
}

# intent → STIX 2.1 ``attack-motivation-ov`` の近似クロスウォーク。
# STIX の語彙は粗いため厳密対応ではなく「最も近い」値を選ぶ。
# unknown は motivation を付与しない (None)。
_INTENT_TO_STIX_MOTIVATION: dict[str, str | None] = {
    "espionage": "organizational-gain",
    "financial": "personal-gain",
    "prepositioning": "dominance",
    "disruption": "coercion",
    "influence": "ideology",
    "hacktivism": "ideology",
    # 地政学動機: STIX attack-motivation-ov は cyber-attacker 専用語彙のため近似 or None。
    # 値が無い (deterrence / diplomacy = 防御的・協調的) のは STIX が攻撃動機しか持たない証左。
    "coercion": "coercion",
    "deterrence": None,
    "territorial": "dominance",
    "subversion": "dominance",
    "diplomacy": None,
    "unknown": None,
}


def normalize_intent(raw: object) -> SocioPoliticalIntent:
    """LLM 出力 (任意文字列 / 欠落) を canonical な intent に正規化する。

    canonical に一致 → そのまま。alias 辞書に一致 → canonical へ寄せる。
    いずれにも一致しない / 非 str → ``"unknown"``。
    """
    if not isinstance(raw, str):
        return "unknown"
    key = raw.strip().lower().replace("_", "-")
    if key in SOCIO_POLITICAL_INTENTS:
        return key  # type: ignore[return-value]
    mapped = _INTENT_ALIASES.get(key)
    if mapped is not None:
        return mapped  # type: ignore[return-value]
    return "unknown"


def intent_label_ja(intent: str) -> str:
    """canonical intent の日本語ラベルを返す (未知は「不明」)。"""
    return INTENT_LABELS_JA.get(intent, INTENT_LABELS_JA["unknown"])


def intent_to_stix_motivation(intent: str) -> str | None:
    """intent を STIX ``attack-motivation-ov`` 値に写像する (該当なしは None)。"""
    return _INTENT_TO_STIX_MOTIVATION.get(intent)


# ---------- Diamond axes 構造化スキーマ ----------


class SocioPoliticalAxis(BaseModel):
    """Adversary ⇄ Victim 軸: 攻撃者の意図/動機 (frozen)。

    フィールド:
        intent: closed enum の意図 (集計可能)。
        rationale: 1 行の日本語根拠 (≤80 字、任意)。
        confidence: LLM の自己評価。``low`` は集計で除外する用途。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    intent: SocioPoliticalIntent = "unknown"
    rationale: str = Field(default="", max_length=80)
    confidence: Confidence = "low"


class DiamondAxes(BaseModel):
    """Diamond Model の 2 meta-feature 軸をまとめた構造 (frozen)。

    LLM が ``briefing/summarizer.j2`` の ``diamond`` オブジェクトとして出力した値を
    ``parse_diamond_axes`` で正規化・型検証した結果。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    socio_political: SocioPoliticalAxis = Field(default_factory=SocioPoliticalAxis)
    technical: str = Field(default="", max_length=120)

    @property
    def has_signal(self) -> bool:
        """意図 (unknown 以外) または technical narrative のいずれかがあるか。"""
        return self.socio_political.intent != "unknown" or bool(self.technical.strip())


def parse_diamond_axes(raw: object) -> DiamondAxes:
    """LLM 出力 (``diamond`` dict もしくは欠落/不正値) から安全に生成する。

    LLM が ``diamond`` を欠落させた / 型不一致を返したケースでもクラッシュ
    させず安全側 (intent="unknown" / 空 narrative) のデフォルトを返す。
    parse_routing_flags と同形式の防御的パーサ。
    """
    if not isinstance(raw, dict):
        return DiamondAxes()

    sp_raw = raw.get("socio_political")
    if isinstance(sp_raw, dict):
        intent = normalize_intent(sp_raw.get("intent"))
        rationale = _clean_oneliner(sp_raw.get("rationale"), max_length=80)
        conf = sp_raw.get("confidence")
        confidence: Confidence = conf if conf in ("high", "medium", "low") else "low"
        socio_political = SocioPoliticalAxis(
            intent=intent,
            rationale=rationale,
            confidence=confidence,
        )
    else:
        # socio_political を string で返す LLM もありうる (intent だけ)
        socio_political = SocioPoliticalAxis(intent=normalize_intent(sp_raw))

    technical = _clean_oneliner(raw.get("technical"), max_length=120)

    try:
        return DiamondAxes(socio_political=socio_political, technical=technical)
    except Exception:  # noqa: BLE001
        return DiamondAxes()


def _clean_oneliner(value: object, *, max_length: int) -> str:
    """自由文を 1 行・印字可能文字のみ・長さ上限に整える (非 str は空)。"""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(c for c in value if c.isprintable() or c == " ")
    cleaned = " ".join(cleaned.split())  # 連続空白/改行 → 単一空白
    return cleaned[:max_length].strip()

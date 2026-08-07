"""salience — 「最重要」の決定論ランキング (headline を LLM の暗黙感覚から奪還する)。

台帳蓄積の実測で、CTI ブリーフの headline を kinetic 戦況報道 (キーウ攻撃) が占有した
(段C)。さらに 2026-07-05 の実測で、ソース本文がボット対策画面のみ・leading が
unverified_or_false の判定が、実証拠 7 件を持つ同 delta の判定を差し置いて headline を
占有した。後者の病巣は **ACH の誠実な較正結果 (確度・仮説クラス・反証) が配置決定に
流れ込む経路がなかった**こと。

ここでの定式化: **salience = 関連性 × 認識論的重み**。

- 関連性 (relevance) = 何についてか (使命序列 domain / 日本 / PIR) × 何が変わったか (delta)
- 認識論的重み (epistemic weight) = どれだけ知っているか (確度 / leading 仮説クラス / 反証)

インテリジェンスの筆頭価値は「意思決定関連性 × 接地された知識量」の積であり、
片方が空洞なら価値も空洞になる (乗法)。掲載順はこの乗算で決める。

headline の選定だけは pool を 2 段に分ける (pick_headline, 2026-07-12): 当初は
「変化優先は delta の重みから創発する (接地された変化は常に standing に勝つ)」と
仮定していたが実測で偽 — 日本×最上位PIR×高確度の standing が接地された高確度の変化を
連日抑え、同文 headline が続いた。daily の筆頭は「今日読者が新しく知るべきこと」
(= 変化) を答えるため、接地された変化は standing と競合させない。
関連性が桁違い (日本×多 PIR×高 delta) の未実証情報は、接地された変化が無い日に限り
減衰つきで浮上**できる** (ハード除外の誤爆を避ける)。噂クラスの Situation の
追跡自体は正しい (確認されれば flip する = I&W の設計通り) — 誤りは追跡でなく配置。

**LLM は順位を選ばず、選ばれたものの散文だけを書く** (設計 §4.2)。重みは定数
(マジックナンバー禁止)。確度や判定は動かさない — 掲載順のみ。
"""

from __future__ import annotations

from functools import lru_cache

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import KeyJudgment

_log = get_logger(__name__)

# ---- 関連性 (何について・何が変わったか) ----

# delta の大きさ: 転換 > 新規/拡大/再燃 > 強化/後退/更新 > 継続。
_DELTA_MAGNITUDE: dict[str, float] = {
    "hypothesis_flip": 3.0,
    "opened": 2.0,
    "escalated": 2.0,
    "reopened": 2.0,
    "strengthened": 1.0,
    "weakened": 1.0,
    "claim_revised": 0.5,
    "closing": 1.0,
    "no_change": 0.0,
    "": 0.0,
}
# 使命序列 (CTI ブリーフ): サイバー事象 > サイバー隣接 (技術/インフラ/情報) >
# 経済安全保障 > 地政学文脈 (追跡はするが headline は原則サイバー先行)。
_DOMAIN_WEIGHT: dict[str, float] = {
    "cyber_incident": 1.0,
    "tech": 0.8,
    "infrastructure": 0.8,
    "information": 0.8,
    "economic": 0.6,
    "geopolitical": 0.5,
    "political": 0.5,
    "military": 0.5,
    "social": 0.5,
}
_DOMAIN_WEIGHT_DEFAULT = 0.6

# 重み: domain (使命) を最重、次に delta (変化)、日本関連。
_W_DOMAIN = 3.0
_W_DELTA = 2.0
_W_JP = 2.0
# 2026-07-05 L2/L3: PIR は加算項でなく relevance の **乗算的増幅器**。上位ミッション PIR に
# マッチした判定の relevance を最大 (1+_W_PIR_BOOST) 倍する (0 で PIR 無効化)。件数でなく
# **最重要 PIR の優先度** を使う (promiscuous PIR = disinfo/critical_infra の件数インフレ排除)。
# 効果: 動いた高優先 PIR 判定は headline を取り、standing 高優先も押し上げるが、genuine な
# 大 delta には負ける (陳腐化・戦況 hijack を防ぐ)。domain 重み (段C の反 hijack) は不変。
_W_PIR_BOOST = 1.0
# PIR 優先度 = config リスト順 (SSoT、先頭=最優先) の指数減衰。上位 ~5 PIR
# (nation APT + JP + critical infra = ミッションの核) に boost を集中させ、周辺 PIR
# (minor_* / general_alert 等) の寄与を無視できる大きさにする。
_PIR_PRIORITY_HALFLIFE_RANKS = 5.0

# ---- 認識論的重み (どれだけ知っているか) ----

# 確度は較正済み (ACH + source_basis cap + 帰属 cap) — その結果を配置に反映する。
_CONF_EPISTEMIC: dict[str, float] = {"high": 1.0, "moderate": 0.85, "low": 0.6}
_CONF_EPISTEMIC_DEFAULT = 0.85
# leading が「主張の真偽/報道の性質」系 = 実証された脅威事象ではなく、噂・誇張・報道人工物。
# 判定の主題が「脅威」でなく「主張の疑わしさ」であるエピステミックに特殊なクラス。
_CLAIM_VERACITY_HYPOTHESES: frozenset[str] = frozenset(
    {"unverified_or_false", "reporting_artifact", "propaganda_or_overstated"}
)
_RUMOR_CLASS_FACTOR = 0.35
# 敵対的検証で反証が成立した判定は、その確度のまま筆頭に立てない
_REFUTED_FACTOR = 0.6


def epistemic_weight(j: KeyJudgment) -> float:
    """判定の認識論的重み (0..1]: 較正確度 × 仮説クラス × 反証の積。"""
    weight = _CONF_EPISTEMIC.get(j.confidence, _CONF_EPISTEMIC_DEFAULT)
    if j.leading_hypothesis in _CLAIM_VERACITY_HYPOTHESES:
        weight *= _RUMOR_CLASS_FACTOR
    if j.adversarial_refuted:
        weight *= _REFUTED_FACTOR
    return weight


@lru_cache(maxsize=1)
def _pir_priority_map() -> dict[str, float]:
    """PIR id → 優先度 weight (0,1]。config リスト順 (先頭=最優先) を指数減衰させる。

    件数でなく優先度で PIR を評価するための重み表 (L2)。失敗/不在は空 dict =
    PIR boost が単に効かなくなる fail-safe。PIR 編集で順序が変わると priority が
    変わるため、``integration.invalidate_cache`` が本 cache を clear する。
    """
    try:
        from src.pir.integration import get_pir_config

        pirs = get_pir_config().priorities
    except Exception as e:  # noqa: BLE001 — config 不在/障害で salience を殺さない
        _log.warning("pir_priority_map_load_failed", error=str(e))
        return {}
    return {p.id: 0.5 ** (rank / _PIR_PRIORITY_HALFLIFE_RANKS) for rank, p in enumerate(pirs)}


def _pir_priority(j: KeyJudgment) -> float:
    """マッチした PIR の **最大** 優先度 weight [0,1]。

    件数 (旧実装) でなく最重要 PIR の優先度を使う: promiscuous PIR (pir_disinfo 等) を
    多数マッチしても、最優先 PIR にマッチしていなければ boost は小さい。
    """
    if not j.pir_ids:
        return 0.0
    weights = _pir_priority_map()
    return max((weights.get(pid, 0.0) for pid in j.pir_ids), default=0.0)


def relevance(j: KeyJudgment) -> float:
    """判定の関連性 = (活動/変化の基底) × (1 + PIR ミッション優先度 boost)。"""
    domain = _DOMAIN_WEIGHT.get(j.domain, _DOMAIN_WEIGHT_DEFAULT)
    delta = _DELTA_MAGNITUDE.get(j.delta_type, 0.0)
    base = _W_DOMAIN * domain + _W_DELTA * delta + _W_JP * (1.0 if j.japan_related else 0.0)
    return base * (1.0 + _W_PIR_BOOST * _pir_priority(j))


def salience(j: KeyJudgment) -> float:
    """判定 1 件の salience (掲載順・headline 選定用の決定論スコア)。"""
    return relevance(j) * epistemic_weight(j)


def rank_judgments(judgments: tuple[KeyJudgment, ...]) -> list[KeyJudgment]:
    """salience 降順 (同点は id 昇順で決定論)。"""
    return sorted(judgments, key=lambda j: (-salience(j), j.id))


def is_headline_grounded(j: KeyJudgment) -> bool:
    """headline の接地ゲート: 噂クラス (主題が「主張の真偽」) と反証成立は筆頭に立てない。

    2026-07-05 に確立した門 (bot 対策画面ソースの unverified flip が実証拠 7 件の判定を
    差し置いて headline を乗っ取った病理) を、pool 分離後も明示的な述語として保存する。
    """
    return not j.adversarial_refuted and j.leading_hypothesis not in _CLAIM_VERACITY_HYPOTHESES


def pick_headline(judgments: tuple[KeyJudgment, ...]) -> KeyJudgment | None:
    """headline に据える判定 = **接地された変化の最上位**、無ければ standing の最上位。

    旧実装は moved∪standing の単一ランキング最上位を採り「接地された変化は delta 重みで
    必ず standing に勝つ」と仮定していたが、実測で偽と判明 (2026-07-12: 日本×最上位PIR×
    高確度の standing = relevance ~10 が、拡大・高確度の moved ~8-10 を連日抑え、変化が
    実在する日に「決定的な変化は観測されていない」の quiet headline を出して
    weight_section と矛盾、同文 headline が数日続いた)。

    daily 報告の筆頭が答えるべき問いは「いま最重要の判定は何か」(状態) ではなく
    「今日読者が新しく知るべき最重要事項は何か」(状態射影の render 軸 = 変化)。よって:
      1. 接地ゲート (噂クラス/反証除外) を通る moved があれば、その salience 最上位が
         headline。standing とは競合させない (pool 分離)。standing 最重要は cog_section が
         常設で担う (役割分離)。
      2. 無ければ従来どおりの単一ランキング。未実証/反証済みの変化は epistemic 減衰つきで
         standing と競合する — 日本×多 PIR の未実証情報が弱い standing を上回って
         浮上**できる** 設計 (I&W: ハード除外の誤爆を避ける、2026-07-05 確立) は不変。
    順位づけ自体は salience 乗算のまま (pool の分離であって重みのハードルールではない)。
    """
    if not judgments:
        return None
    moved_grounded = tuple(
        j for j in judgments if j.delta_type not in ("", "no_change") and is_headline_grounded(j)
    )
    if moved_grounded:
        return rank_judgments(moved_grounded)[0]
    return rank_judgments(judgments)[0]

"""記事 → Situation の決定論割当 (Situation Ledger 段A、LLM 不使用)。

選定を「K 件の一発勝負」から「割当 + 新規開設判断」に分解する第一の部品
(docs/synthesis_situation_ledger_design.md §3.1)。割当規則:

1. 強 anchor (cve/victim_org/malware_family/actor/actor_provisional) を 1 つでも共有 → 同一事案。
2. 国 anchor を 2 つ以上共有 (国ペア) → 同一情勢 (地政学)。
3. 国 anchor 1 つ + タイトル token 重なり >= 2 → 同一情勢。

国は entity (involved_country) と gazetteer 導出 (nation_gazetteer) の和集合。
token 照合は grounded clustering の日本語対応 tokenizer を再利用する。
over-merge を避けるため国 1 つだけの共有では割当てない (US だけで何でも繋がるのを防ぐ)。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from src.assessment.situation_store import AssignedBy, SituationRow
from src.cti.nation_gazetteer import nations_in_text
from src.synthesis.grounded.clustering import _tokens


@lru_cache(maxsize=1)
def _nation_name_tokens() -> frozenset[str]:
    """国名 alias 由来の token 集合 (話題 token から除外する)。

    国名は nations 軸で既に照合しており、token にも残すと「ロシア」の共有だけで
    無関係な戦況報道が topical 一致に見える (discrete_event ゲートが実測で突破された)。
    """
    from src.cti.taxonomy_normalizer import load_normalizer

    toks: set[str] = set()
    for alias in load_normalizer().country_lookup:
        toks |= _tokens(alias)
    return frozenset(toks)


def topic_tokens(text: str) -> frozenset[str]:
    """話題 token (国名 token を除外した弁別的 token)。割当/claim 照合の共通導出。"""
    return frozenset(_tokens(text)) - _nation_name_tokens()


# 同一「事案」を強く示す entity type (clustering._ANCHOR_TYPES + 暫定アクター + 命名作戦)。
STRONG_ANCHOR_TYPES: frozenset[str] = frozenset(
    {"cve", "victim_org", "malware_family", "actor", "actor_provisional", "campaign"}
)
# 国キー: 当事国 (LLM 抽出) と言及国 (mention_tagger 決定論) の和集合を国 anchor として扱う。
_NATION_TYPES: frozenset[str] = frozenset({"involved_country", "mentioned_country"})
# 割当規則の閾値 (§3.1)。マジックナンバー禁止のため命名。
_MIN_NATION_PAIR = 2
_MIN_TOKEN_OVERLAP_WITH_NATION = 2


@dataclass(frozen=True)
class ArticleKeys:
    """割当判定に使う記事側のキー (決定論導出済み)。"""

    article_id: str
    title: str
    strong: frozenset[str]  # "type:value"
    nations: frozenset[str]  # ISO alpha-2
    tokens: frozenset[str]


@dataclass(frozen=True)
class SituationKeys:
    """割当判定に使う Situation 側のキー (anchors を分解 + title tokens)。

    claim_type は latest revision 由来 (段B)。discrete_event (単発事象) の Situation には
    国ペアだけで割当てない — 高頻度紛争ペア (RU-UA 等) の一般戦況報道が単発事象の
    Situation に吸着する over-merge (台帳蓄積で実測: キーウ攻撃に 16 件) を防ぐ。
    """

    row: SituationRow
    strong: frozenset[str]
    nations: frozenset[str]
    tokens: frozenset[str]
    claim_type: str = ""


@dataclass(frozen=True)
class Assignment:
    """割当結果 1 件。assigned_by は監査用 (どの規則で繋いだか)。"""

    article_id: str
    situation_id: str
    assigned_by: AssignedBy


def split_anchor_keys(keys: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """ "type:value" キー集合を (強anchor, 国ISO) に分解する。"""
    strong: set[str] = set()
    nations: set[str] = set()
    for k in keys:
        t, _, v = k.partition(":")
        if t in STRONG_ANCHOR_TYPES:
            strong.add(k)
        elif t in _NATION_TYPES and v:
            nations.add(v)
    return frozenset(strong), frozenset(nations)


def build_article_keys(*, article_id: str, title: str, entity_keys: frozenset[str]) -> ArticleKeys:
    """記事の割当キーを導出 (entity + gazetteer 国名補完 + タイトル token)。"""
    strong, nations = split_anchor_keys(entity_keys)
    return ArticleKeys(
        article_id=article_id,
        title=title,
        strong=strong,
        nations=frozenset(nations | nations_in_text(title)),
        tokens=topic_tokens(title),
    )


def build_situation_keys(row: SituationRow, *, claim_type: str = "") -> SituationKeys:
    strong, nations = split_anchor_keys(row.anchors)
    return SituationKeys(
        row=row,
        strong=strong,
        nations=frozenset(nations | nations_in_text(row.title)),
        tokens=topic_tokens(row.title),
        claim_type=claim_type,
    )


def _score(art: ArticleKeys, sit: SituationKeys) -> tuple[tuple[int, int, int], AssignedBy] | None:
    """割当規則の適合を判定。適合しなければ None、適合なら (順位タプル, 規則名)。

    **国は必要条件であって同一性ではない** (2026-07-04 実測で確立): 国ペアのみの割当は
    高頻度紛争トリオ ({IR,US,IL} 等) で無関係報道の吸着器を作る (イラン葬儀 Situation が
    62 証拠まで肥大し増分 ACH の暴走と pipeline timeout を誘発)。本物の続報は必ず主題語を
    共有するため、nation 系割当は**常に**話題 token の共有を要求する。claim_type 依存の
    ゲート (LLM 分類頼み = 保証なし) は廃止し、決定論の普遍規則に置き換えた。
    """
    strong_share = len(art.strong & sit.strong)
    nation_share = len(art.nations & sit.nations)
    token_share = len(art.tokens & sit.tokens)
    if strong_share >= 1:
        return (strong_share, nation_share, token_share), "anchor"
    if nation_share >= _MIN_NATION_PAIR and token_share >= 1:
        return (0, nation_share, token_share), "nation"
    if nation_share >= 1 and token_share >= _MIN_TOKEN_OVERLAP_WITH_NATION:
        return (0, nation_share, token_share), "token"
    return None


def match_situation(
    art: ArticleKeys, situations: list[SituationKeys]
) -> tuple[SituationKeys, AssignedBy] | None:
    """記事に最も適合する Situation を返す (決定論: score 降順 → id 昇順)。"""
    best: tuple[tuple[int, int, int], str, SituationKeys, AssignedBy] | None = None
    for sit in situations:
        scored = _score(art, sit)
        if scored is None:
            continue
        score, by = scored
        # 同点は situation_id 昇順 (負号なしで比較できるよう id を第2キーに)
        key = (score, sit.row.situation_id)
        if best is None or (key[0] > best[0]) or (key[0] == best[0] and key[1] < best[1]):
            best = (key[0], key[1], sit, by)
    if best is None:
        return None
    return best[2], best[3]


# claim (判定文) と Situation の照合は記事割当より高 recall が必要 (重複開設を防ぐ)。
# 国も強 entity も無い claim (NetNut 型、台帳蓄積で重複開設を実測) は token 重なりのみで
# 照合する — claim 同士の token 3 個一致は記事割当より誤結合リスクが低い (対象が K 件と少数)。
_MIN_TOKEN_ONLY_CLAIM = 3


def match_claim(
    art: ArticleKeys, situations: list[SituationKeys]
) -> tuple[SituationKeys, AssignedBy] | None:
    """判定 claim (または新規開設候補) に適合する既存 Situation を返す。

    match_situation の規則 + token のみ規則 (>= _MIN_TOKEN_ONLY_CLAIM)。
    """
    matched = match_situation(art, situations)
    if matched is not None:
        return matched
    best: tuple[int, str, SituationKeys] | None = None
    for sit in situations:
        token_share = len(art.tokens & sit.tokens)
        if token_share < _MIN_TOKEN_ONLY_CLAIM:
            continue
        if (
            best is None
            or token_share > best[0]
            or (token_share == best[0] and sit.row.situation_id < best[1])
        ):
            best = (token_share, sit.row.situation_id, sit)
    if best is None:
        return None
    return best[2], "token"

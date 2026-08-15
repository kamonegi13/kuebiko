"""単独では実質的に識別不能な actor 別名 (一般語) の SSoT。

実害 (2026-07-17 検出): APT10 の旧 Microsoft 名 ``POTASSIUM`` が化学元素カリウムを
扱う記事に誤帰属し、対象 actor の entity の 53% が汚染された。旧 Microsoft の元素名系
actor 名は単独使用が稀で実務では併記が通例のため、辞書 (config/cti/actor_aliases.yaml)
から除去した。

しかし MITRE ATT&CK は同じ名前を group の alias として保持しているため、週次同期
(``src/cti/mitre_sync.py``) が毎週それらを書き戻す**再発ループ**になっていた
(2026-07-21 に 11 件の復活を検出)。除去の意図を 1 箇所に持たせ、同期の取込 filter と
辞書の guard test の双方がこれを参照することで再発を止める。

**例外**: ``ambiguous: true`` (文脈 cue ゲート) を持つ actor は一般語名を保持してよい
(例: Tick)。cue なしでは帰属しないため誤検出しない。ゲートの有無の判定は呼び出し側が行う。
"""

from __future__ import annotations

# 旧 Microsoft actor naming の元素名。単独出現は元素・一般名詞である可能性が高い。
_ELEMENT_NAMES: frozenset[str] = frozenset(
    {
        "americium",
        "barium",
        "cerium",
        "chromium",
        "cobalt",
        "copper",
        "gadolinium",
        "krypton",
        "lithium",
        "manganese",
        "mercury",
        "neon",
        "nickel",
        "osmium",
        "palladium",
        "phosphorus",
        "platinum",
        "polonium",
        "potassium",
        "radium",
        "silicon",
        "sodium",
        "thallium",
        "zinc",
    }
)

# 頻出一般語。単独では実質識別不能 (動物・気象・匿名集団名など)。
_COMMON_WORDS: frozenset[str] = frozenset(
    {
        "anonymous",
        "bear",
        "cloud",
        "kitten",
        "panda",
        "spider",
        "tick",
    }
)

# サイバー犯罪/APT グループ名が一般英単語・固有名詞と衝突するもの (entity パイプライン棚卸し
# 2026-07-29, docs/entity_pipeline_inventory.md §6)。実データで非 cyber 記事に mention 混入が
# 実証された名前 (play=866中730件が非cyber, gallium=元素で100%非cyber, axiom=Axiom Space 等)。
# これらは ambiguous=true (文脈 cue ゲート) を必須とし、cue 未指定時は resolve_ambiguous_cues が
# CYBERCRIME_CONTEXT_CUES を割り当てる。単語 1 語の canonical を持つものだけ列挙する (2 語名は
# word-boundary 一致で衝突しないため対象外)。
_CYBERCRIME_GROUP_WORDS: frozenset[str] = frozenset(
    {
        "play",
        "deadlock",
        "lynx",
        "interlock",
        "everest",
        "anubis",
        "chimera",
        "axiom",
        "gallium",
        # 既に ambiguous 済だが SSoT 完全性のため列挙 (guard test / mitre filter を効かせる)
        "chaos",
        "cloak",
        "kairos",
        "warlock",
        "morpheus",
        # 2026-08-01 リコール層承認分: termite=シロアリ、akira=アニメ/人名 (いずれも ambiguous 済)
        "termite",
        "akira",
    }
)

GENERIC_ALIAS_WORDS: frozenset[str] = _ELEMENT_NAMES | _COMMON_WORDS | _CYBERCRIME_GROUP_WORDS


def is_generic_alias(name: str) -> bool:
    """``name`` が単独では actor を識別できない一般語か (大小文字・前後空白を無視)。"""
    return name.strip().lower() in GENERIC_ALIAS_WORDS

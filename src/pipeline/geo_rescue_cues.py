"""地政学の救済の判定の前段: 記事に書かれた手掛かりの決定論の検出 (GEO-RESCUE-JUDGED v2)。

意味の判定 (``geo_rescue_judge``) の前に、**記事の題と本文プレビューに** 次のどれかが
書かれているかを語で確かめる。どれも無ければ判定にかけず救わない (LLM を呼ばない)::

    nation:<ISO>   注視国 (中国・ロシア・北朝鮮・イラン) の国名・形容詞・首都
    proxy:<name>   注視国の側に立つ物理の勢力 (フーシ派・ヒズボラ・ワグネル 等)
    actor:<id>     アクター辞書で国籍が注視国の行為者 (APT・ハクティビスト)
    war:ukraine    注視国が当事者の戦争の相手側 (ロシアの対ウクライナの戦争)
    theatre:<name> 戦域 (台湾海峡・南シナ海・バルト海・NATO の東側・黒海・紅海 等)
    trigger:<w>    侵入・領空侵犯
    framework:<w>  抑止・対抗の枠組み (AUKUS・NATO の東側の抑止・ウクライナ支援の枠組み 等)
    cyber_geo:<g>  定義 D (サイバーと地政学の連関): 国家のサイバー戦略・影響工作・
                   ハイブリッドの脅威・重要インフラへの破壊工作 (注視国の名前が無くてもよい)

``cyber_geo:`` の手掛かりは定義 D 専用の問い (QD) に回し、それ以外の手掛かりは QW・QC に回す
(``is_cyber_geo_cue``)。

**なぜ語の関門か** (2026-10-30 の誤りの分析、data/eval/geo_rescue_judged_2026_10_30/
error_analysis.md): 誤った救済 85 件のうち 55 件は同盟国・同志国・米の一般の軍事・防衛産業の
話で、注視国の脅威との結び付きが**記事に書かれていない**のに、救済モデルが地理 (台湾・韓国・
欧州) から相手を推して Yes と答えていた。正解の定義 (事前登録の追補の C′) は手掛かりが
「記事に書かれていること。付け手の背景知識で補わない」を要件にしており、書かれているかは
語で決められる。55 件のうち 44 件がこの関門で落ちる (探索の標本での推定、汚染済み)。

語は定義 (事前登録の追補の A′・C′ の列) から取った。ラベルの SSoT は再利用する:
国名 = ``config/cti/countries.yaml`` (``nation_gazetteer.nations_in_text``)、注視国 =
``ingest_relevance.WATCHED_NATIONS``、戦域 = ``ingest_relevance.GEO_THEATRE_TERMS``
(ここではその差分だけを足す)、アクター = ``config/cti/actor_aliases.yaml``、照合 =
``keyword_match.keyword_in_text`` (ASCII は語境界なので複数形は別の語として書く)。

定義 D の語は既存の語彙を使わずここに書いた。近い語彙はあるが目的が違う: PIR の
``strong_signals`` (pir_geopolitical_cyber / pir_disinfo / pir_critical_infra) は運用者が DB で
編集する実行時の値で (測定の再現性がなく、編集で関門が黙って変わる)、``IO`` / ``OT`` 等の
短い略語を含み、ハイブリッドの脅威・海底ケーブルを持たない。``ingest_relevance._CYBER_POLICY_CORE``
は暗号・量子・AI を含む取り込みの保険で D ではない。``diamond_model`` の意図の表は分類ラベルの
正規化 (``dos`` / ``protest`` 等) で本文の照合に向かない。
"""

from __future__ import annotations

import re

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.ingest_relevance import GEO_THEATRE_TERMS, WATCHED_NATIONS
from src.cti.keyword_match import keyword_in_text
from src.cti.nation_gazetteer import nations_in_text
from src.logging_config import get_logger

_log = get_logger(__name__)

#: 国名の辞書 (countries.yaml) に無い形容詞・首都・軍や党の略称・日本語の略記の複合語
#: (ASCII は語境界、日本語は部分一致)。2026-10-30 改訂 (定義から、標本は見ていない):
#: - 裸の ``pla`` は 3D 印刷の材料 (PLA filament) にも当たるため、軍の呼び方の形だけにした
#: - ``ccp`` / 中国共産党を足した。日本語の略記は 露軍・露朝・露ウ・中台・日朝 を足した
#:   (日中 は「日中 (昼間)」と区別できないため入れない。中露・米中・米朝 等は gazetteer が持つ)
_WATCHED_EXTRA: dict[str, tuple[str, ...]] = {
    "CN": (
        "chinese",
        "beijing",
        "people's liberation army",
        "pla navy",
        "pla air force",
        "pla rocket force",
        "pla army",
        "pla's",
        "ccp",
        "chinese communist party",
        "中共",
        "人民解放軍",
        "中国共産党",
        "中台",
    ),
    "RU": (
        "russian",
        "russians",
        "moscow",
        "kremlin",
        "putin",
        "クレムリン",
        "露軍",
        "露朝",
        "露ウ",
        # 2026-10-30 改訂 2 (定義から): 「露」を含む曖昧でない複合語だけ
        "親露",
        "対露",
        "露外相",
        "露外務省",
        "露大統領",
        "露国防省",
        "露国防相",
        "露政府",
    ),
    "KP": ("north korean", "north koreans", "pyongyang", "kim jong un", "平壌", "露朝", "日朝"),
    "IR": ("iranian", "iranians", "tehran", "irgc", "革命防衛隊"),
}

#: 照合の前に消す語 (注視国の語を含むが注視国への言及ではない。部分一致・大小文字を問わない)。
#: 中国地方 (日本の地域)・中国電力 等は「中国」に、不正侵入 (サイバーの用語) は引き金の
#: 「侵入」に、PLA filament (印刷の材料) は形容の語に当たらないようにする
_PRE_REMOVE = re.compile(
    r"中国地方|中国電力|中国新聞|中国・四国|中四国|不正侵入|pla filament|pla plastic",
    re.IGNORECASE,
)

#: 注視国の側に立つ物理の勢力 (追補 A′ の名前の列。アクター辞書はサイバーの行為者だけを持つ)
_PROXIES: dict[str, tuple[str, ...]] = {
    "houthi": ("houthi", "houthis", "ansar allah", "ansarallah", "フーシ"),
    "hezbollah": ("hezbollah", "hizbollah", "ヒズボラ"),
    "iraq_militia": (
        "kataib hezbollah",
        "islamic resistance in iraq",
        "axis of resistance",
        "抵抗の枢軸",
    ),
    # 裸の wagner は人名 (作曲家・選手等) に当たるため、集団の呼び方の形だけ
    "wagner": (
        "wagner group",
        "wagner pmc",
        "wagner mercenaries",
        "wagner fighters",
        "africa corps",
        "ワグネル",
        "アフリカ軍団",
    ),
}

#: 注視国が当事者の戦争の相手側 (追補 A の明確化: ロシアの対ウクライナの戦争)
_WAR_COUNTERPARTS: dict[str, tuple[str, ...]] = {
    "ukraine": ("ukraine", "ukrainian", "ukrainians", "kyiv", "ウクライナ"),
}

#: 戦域のうち ``GEO_THEATRE_TERMS`` に無いもの (追補 C′ の 1)
_THEATRE_EXTRA: dict[str, tuple[str, ...]] = {
    "baltic": ("baltic", "baltic sea", "バルト海", "バルト三国"),
    "nato_east": (
        "eastern flank",
        "eastern-flank",
        "nato の東側",
        "natoの東側",
        "nato東側",
        "nato の東部",
        "nato東部",
    ),
    "black_sea": ("black sea", "黒海"),
    "gulf_of_aden": ("gulf of aden", "アデン湾"),
    "east_china_sea": ("east china sea", "東シナ海"),
    "persian_gulf": (
        "persian gulf",
        "arabian gulf",
        "gulf states",
        "gulf state",
        "ペルシャ湾",
        "湾岸諸国",
    ),
}

#: 引き金 (追補 C′ の 2 = 侵入・領空侵犯・無人機の飛来。「攻撃」は一般語のため入れない —
#: 注視国の名前と一緒なら nation で拾う)。英語の intrusion はサイバーの侵入の意味が主のため
#: 入れない (物理の侵入は incursion)。日本語の「不正侵入」は ``_PRE_REMOVE`` で消す
_TRIGGERS: tuple[str, ...] = (
    "incursion",
    "incursions",
    "drone sighting",
    "drone sightings",
    "drone incursion",
    "drone incursions",
    "airspace violation",
    "airspace violations",
    "violated airspace",
    "領空侵犯",
    "侵入",
    "ドローンの飛来",
    "ドローン飛来",
    "無人機の飛来",
)

#: 枠組み (追補 C′ の 3。定義の例「抑止のため」から抑止の語も含める)。
#: 既知の広すぎる当たり (問いに任せる、事前登録に記録): 犯罪の抑止 (crime deterrent) /
#: 一般名詞の quad (quad-core 等)。``purl`` は略語のままだと URL の用語にも当たるため正式名にした
_FRAMEWORKS: tuple[str, ...] = (
    "aukus",
    "eastern sentry",
    "baltic sentry",
    "forward presence",
    "forward-deployed",
    "forward deployed",
    "deterrence",
    "deterrent",
    "deter",
    "deters",
    "deterring",
    "ramstein",
    "ukraine defense contact group",
    "ukraine defence contact group",
    "prioritized ukraine requirements list",
    "quad",
    "日米比",
    "日米韓",
    "米韓",
    "日米豪印",
    # 2026-10-30 改訂 2 (定義 C′3 の 日米 / 米韓 / 日米比 の抑止の枠組みの英語と 日米)
    "日米",
    "日米同盟",
    "us-japan",
    "u.s.-japan",
    "japan-us",
    "japan-u.s.",
    "us-rok",
    "u.s.-rok",
    "rok-us",
    "us-south korea",
    "u.s.-south korea",
    "us-philippines",
    "u.s.-philippines",
    "philippines-us",
    "抑止",
    "前方展開",
)


#: 「三か国の」枠組み (trilateral) は貿易・首脳の会合にも使われるため、安全保障の語と同じ記事に
#: あるときだけ手掛かりにする (2026-10-30 改訂 2、定義 C′3 の 日米韓 / 日米比 の英語の言い方)
_TRILATERAL: tuple[str, ...] = ("trilateral", "三か国", "3か国", "三カ国")
_SECURITY_TERMS: tuple[str, ...] = (
    "security",
    "defense",
    "defence",
    "military",
    "deterrence",
    "drill",
    "drills",
    "exercise",
    "exercises",
    "安全保障",
    "防衛",
    "軍事",
    "演習",
    "抑止",
)


def _trilateral_reasons(lower: str) -> list[str]:
    if not any(keyword_in_text(t, lower) for t in _TRILATERAL):
        return []
    if not any(keyword_in_text(t, lower) for t in _SECURITY_TERMS):
        return []
    return ["framework:trilateral_security"]


#: 定義 D (サイバーと地政学の連関) の語。事前登録 §5.1 (D) の 3 つの中身に対応する
_CYBER_GEO: dict[str, tuple[str, ...]] = {
    "strategy": (
        "cyber strategy",
        "national cyber strategy",
        "cybersecurity strategy",
        "cyber security strategy",
        "cyber doctrine",
        "cyber command",
        "offensive cyber",
        "cyber operations",
        "cyber force",
        "サイバー戦略",
        "サイバーセキュリティ戦略",
        "サイバー安全保障",
        "能動的サイバー防御",
        "サイバー作戦",
        "サイバー軍",
    ),
    "influence": (
        "influence operation",
        "influence operations",
        "information operation",
        "information operations",
        "disinformation",
        "disinformation campaign",
        "propaganda campaign",
        "cognitive warfare",
        "election interference",
        "影響工作",
        "影響力工作",
        "偽情報",
        "認知戦",
        "情報戦",
        "選挙干渉",
    ),
    "hybrid": (
        "hybrid threat",
        "hybrid threats",
        "hybrid warfare",
        "hybrid attack",
        "hybrid attacks",
        "gray zone",
        "grey zone",
        "ハイブリッド脅威",
        "ハイブリッド戦",
        "グレーゾーン",
    ),
    "infrastructure_sabotage": (
        "undersea cable",
        "undersea cables",
        "subsea cable",
        "subsea cables",
        "submarine cable",
        "submarine cables",
        "sabotage",
        "critical infrastructure",
        "power grid",
        "energy grid",
        "海底ケーブル",
        "破壊工作",
        "重要インフラ",
        "電力網",
        "送電網",
    ),
}

#: 定義 D の手掛かりの接頭辞 (QD に回す)
CYBER_GEO_PREFIX = "cyber_geo:"


def is_cyber_geo_cue(reason: str) -> bool:
    """手掛かりが定義 D (QD に回す) のものか。"""
    return reason.startswith(CYBER_GEO_PREFIX)


def _named(groups: dict[str, tuple[str, ...]], prefix: str, lower: str) -> list[str]:
    return [
        f"{prefix}:{name}"
        for name, terms in groups.items()
        if any(keyword_in_text(t, lower) for t in terms)
    ]


def _nation_reasons(text: str, lower: str) -> list[str]:
    hits = nations_in_text(text) & WATCHED_NATIONS
    hits |= {
        n for n, terms in _WATCHED_EXTRA.items() if any(keyword_in_text(t, lower) for t in terms)
    }
    return [f"nation:{n}" for n in sorted(hits)]


def _actor_reasons(text: str) -> list[str]:
    try:
        registry = load_actor_aliases()
    except Exception as e:  # noqa: BLE001 — 辞書の破損で救済全体を止めない (この手掛かりだけ欠ける)
        _log.warning("geo_rescue_cue_actor_dict_load_failed", error=str(e)[:200])
        return []
    return [
        f"actor:{a.id}"
        for a in registry.find_all(text)
        if (a.nation or "").strip().upper() in WATCHED_NATIONS
    ]


def geo_cue_reasons(title: str, text: str) -> tuple[str, ...]:
    """題 + 本文プレビューに書かれた手掛かり (無ければ空)。決定論・LLM 不使用。

    ``text`` は判定と同じ入力 (triage の本文プレビュー = 冒頭 1,500 字) を渡す。
    """
    joined = _PRE_REMOVE.sub(" ", f"{title}\n{text}")
    lower = joined.lower()
    reasons = [
        *_nation_reasons(joined, lower),
        *_named(_PROXIES, "proxy", lower),
        *_actor_reasons(joined),
        *_named(_WAR_COUNTERPARTS, "war", lower),
        *_named({**GEO_THEATRE_TERMS, **_THEATRE_EXTRA}, "theatre", lower),
        *(f"trigger:{t}" for t in _TRIGGERS if keyword_in_text(t, lower)),
        *(f"framework:{t}" for t in _FRAMEWORKS if keyword_in_text(t, lower)),
        *_trilateral_reasons(lower),
        *_named(_CYBER_GEO, CYBER_GEO_PREFIX.rstrip(":"), lower),
    ]
    return tuple(dict.fromkeys(reasons))


__all__ = ["CYBER_GEO_PREFIX", "geo_cue_reasons", "is_cyber_geo_cue"]

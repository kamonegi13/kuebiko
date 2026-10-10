"""取り込み時点の関連性ヒント (M4、docs/importance_relevance_redesign.md §6)。

s23 (平たい triage、``TRIAGE_FLAT=1``) は日本・SIR・注視国で重要度を上げ下げしない
(2026-10-08 利用者決定)。そのため取り込みの判断を平たい triage 一本に乗せる前に、
「関連性が高いのに平たい判定が low で落ちる」ことが無いかを確かめる安全網が要る。

このモジュールはタイトル + 概要 (title + summary_preview) だけから **粗く・再現率優先**
(過剰検出歓迎、取り込みを 1 件でも多く残す側に倒す) で関連性の見込みを判定する。
確定した関連性 (§4.2、`jp` / `nations` / `sir_ids` の旗) は要約後の entity から導くもので、
ここはその代用ではない — 「段1 (取り込み) は落とさない保証にだけ使う」(§5) の実装。

判定する 3 種:
    1. 日本関連 (地名・組織名の言及) — ``src.cti.nation_gazetteer`` の国名ガゼッタ (JP)
    2. 注視国 (中国・ロシア・北朝鮮・イラン) — 国名ガゼッタ + 既知 APT アクターの国籍
       (``config/cti/actor_aliases.yaml`` の SSoT を再利用、複製辞書を作らない)
    3. 関連性の核 8 SIR (``RELEVANCE_CORE_SIRS``、``src.cti.importance_v2``) — PIR の
       ``strong_signals.keywords`` を題+概要に当てる軽量版 (``src.cti.keyword_match`` の
       語境界照合 SSoT を再利用)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.importance_v2 import RELEVANCE_CORE_SIRS
from src.cti.keyword_match import keyword_in_text
from src.cti.nation_gazetteer import nations_in_text
from src.logging_config import get_logger

_log = get_logger(__name__)

#: 注視国 (ISO 3166-1 alpha-2)。CLAUDE.md §4 の中華系 LLM 禁止とは別論点 —
#: ここは CTI の監視対象国の意味 (PIR の pir_china_apt 等と同じ対象)。
WATCHED_NATIONS: frozenset[str] = frozenset({"CN", "RU", "KP", "IR"})

JAPAN_ISO = "JP"


@dataclass(frozen=True)
class RelevanceHint:
    """ingest_relevance_hint の結果。``reasons`` は発火理由の内訳 (診断・影子記録用)。"""

    fired: bool
    reasons: tuple[str, ...]


@lru_cache(maxsize=1)
def _core_sir_keywords() -> dict[str, tuple[str, ...]]:
    """関連性の核 8 SIR の keyword を PIR config (DB 正) から集める。

    PIR 編集 (persist_pir_config) で cache は invalidate されないため in-process
    lru_cache のみ (triage の PIR-driven prompt 側の既存 cache 挙動と同じ粒度 —
    コンテナ再起動で再読込。頻繁な PIR 編集直後に厳密反映が必要な用途ではない)。
    """
    try:
        from src.pir.integration import get_pir_config

        cfg = get_pir_config()
    except Exception as e:  # noqa: BLE001 — PIR 障害で ingest 判定全体を殺さない
        _log.warning("ingest_relevance_pir_load_failed", error=str(e))
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for pir in cfg.priorities:
        if pir.id not in RELEVANCE_CORE_SIRS or not pir.enabled:
            continue
        kws = tuple(k for k in pir.strong_signals.keywords if k and k.strip())
        if kws:
            out[pir.id] = kws
    return out


def invalidate_core_sir_keyword_cache() -> None:
    """PIR 編集後に明示的に cache を破棄したいテスト/呼び出し元向け。"""
    _core_sir_keywords.cache_clear()
    _jp_feed_names.cache_clear()


#: 47 都道府県 (「県」「府」「都」を除いた名前も含めて見る)。国内の地名だけの見出しを拾う
_PREFECTURES = [
    "北海道",
    "青森",
    "岩手",
    "宮城",
    "秋田",
    "山形",
    "福島",
    "茨城",
    "栃木",
    "群馬",
    "埼玉",
    "千葉",
    "東京",
    "神奈川",
    "新潟",
    "富山",
    "石川",
    "福井",
    "山梨",
    "長野",
    "岐阜",
    "静岡",
    "愛知",
    "三重",
    "滋賀",
    "京都",
    "大阪",
    "兵庫",
    "奈良",
    "和歌山",
    "鳥取",
    "島根",
    "岡山",
    "広島",
    "山口",
    "徳島",
    "香川",
    "愛媛",
    "高知",
    "福岡",
    "佐賀",
    "長崎",
    "熊本",
    "大分",
    "宮崎",
    "鹿児島",
    "沖縄",
]
#: 国内の自治体 (「さいたま市」「長和町」「浦和区」) と国内を示す語。
#: 再現率優先 (取り込みで落とさないため)
_JP_DOMESTIC = re.compile(
    # 後ろに漢字が続く語 (市場・区別・町内) は自治体ではないので除く
    r"(?:[\u3041-\u309f\u30a1-\u30fa\u4e00-\u9fff]{1,6}(?:市|町|村|区)(?:役所|役場|立|(?![\u4e00-\u9fff])))"
    r"|国内|日本|全国の"
)
#: 国内の媒体の分類 (購読ソースの folder)。国内の媒体は海外の話も載せるが、
#: 取り込みの保険として広めに取る
_JP_FOLDERS = frozenset({"news_jp"})


@lru_cache(maxsize=1)
def _jp_feed_names() -> frozenset[str]:
    try:
        from src.tools.direct_rss_source import load_feeds_config

        return frozenset(f.name for f in load_feeds_config().feeds if f.folder in _JP_FOLDERS)
    except Exception as e:  # noqa: BLE001 — 設定の読み込み失敗で取り込み判定全体を殺さない
        _log.warning("ingest_relevance_feed_config_load_failed", error=str(e))
        return frozenset()


def _japan_reason(text: str, feed: str = "") -> str | None:
    """日本の手がかり (2026-10-08 に国内の地名・組織・媒体を追加)。

    国名ガゼッタは「日本」「Japan」の語が無いと日本と分からず、国内の自治体・企業の名前だけの
    見出し (平たい triage で low になる国内の小さな事案) を落としていた。
    """
    if JAPAN_ISO in nations_in_text(text):
        return "jp"
    if any(p in text for p in _PREFECTURES) or _JP_DOMESTIC.search(text):
        return "jp:domestic"
    if feed and feed in _jp_feed_names():
        return "jp:feed"
    return None


#: 注視国の首都・中枢の名前。国名の辞書が持たないため
#: 「CIA 長官のモスクワ訪問」を取りこぼしていた (2026-10-08)
_WATCHED_CAPITALS: dict[str, tuple[str, ...]] = {
    "CN": ("北京", "中南海", "beijing", "zhongnanhai"),
    "RU": ("モスクワ", "クレムリン", "moscow", "kremlin"),
    "KP": ("平壌", "ピョンヤン", "pyongyang"),
    "IR": ("テヘラン", "tehran"),
}


#: 安全保障・地政学の文脈の語。国名だけだと経済・文化の記事
#: (中国企業の業績・公演・自然環境) にも反応し、取り込みに雑音が増えた
#: (並走の記録 91 件中 5 件、2026-10-08)
_SECURITY_CONTEXT = re.compile(
    r"攻撃|侵害|ハッカ|サイバー|マルウェア|ランサム|諜報|スパイ|工作|軍|兵器|ミサイル|核|制裁|"
    r"安全保障|国防|防衛|外交|紛争|戦争|侵攻|偵察|情報機関|当局|政府|輸出規制|訪問|首脳|会談|"
    r"CIA|FBI|NSA|GCHQ|MI6|"
    r"attack|hack|breach|cyber|malware|ransom|espionage|spy|intelligence|military|army|navy|"
    r"missile|nuclear|weapon|sanction|security|defen[cs]e|diplomat|war\b|invasion|government|"
    r"export control|summit|talks|visit",
    re.IGNORECASE,
)


#: サイバー情勢につながる政策・重要インフラ・技術安全保障の語。平たい triage が low にしても
#: 落とさない (2026-10-10 利用者決定: 落ちる High はサイバー情勢に繋がる情報だった)。
#: 単独で発火する狭い語 (_CYBER_POLICY_CORE) と、注視国・アクターの手がかりを有効にする
#: 文脈としてだけ働く広い語 (_CYBER_POLICY_CONTEXT) に分ける
_CYBER_POLICY_CORE = re.compile(
    r"critical infrastructure|bulk[- ]power|power (?:grid|system)|encryption|quantum|"
    r"frontier (?:ai|model)|ai (?:safety|security|agent)|"
    r"重要インフラ|電力系統|暗号|量子|資安",
    re.IGNORECASE,
)
_CYBER_POLICY_CONTEXT = re.compile(
    r"semiconductor|chip(?:s|maker)?\b|ai (?:standard|governance)|(?:chinese|russian|rogue) ai|"
    r"disinformation|propaganda|bots?\b|drone|nato|半導体|偽情報|情報戦",
    re.IGNORECASE,
)


_PREPRINT_FEED = re.compile(r"arxiv|eprint|iacr", re.IGNORECASE)


def _is_preprint_feed(feed: str) -> bool:
    return bool(_PREPRINT_FEED.search(feed))


def _has_security_context(text: str) -> bool:
    return bool(
        _SECURITY_CONTEXT.search(text)
        or _CYBER_POLICY_CORE.search(text)
        or _CYBER_POLICY_CONTEXT.search(text)
    )


def _watched_nation_reasons(text: str) -> tuple[str, ...]:
    if not _has_security_context(text):
        return ()
    lower = text.lower()
    capital_hits = {n for n, names in _WATCHED_CAPITALS.items() if any(x in lower for x in names)}
    hits = (nations_in_text(text) & WATCHED_NATIONS) | capital_hits
    return tuple(f"nation:{n}" for n in sorted(hits))


def _watched_apt_reasons(text: str) -> tuple[str, ...]:
    """既知 APT アクター名 (国籍が注視国のもののみ) の言及。"""
    try:
        registry = load_actor_aliases()
    except Exception as e:  # noqa: BLE001 — 辞書破損で ingest 判定全体を殺さない
        _log.warning("ingest_relevance_actor_dict_load_failed", error=str(e))
        return ()
    if not _has_security_context(text):
        return ()
    reasons: list[str] = []
    for actor in registry.find_all(text):
        nation = (actor.nation or "").strip().upper()
        if nation in WATCHED_NATIONS:
            reasons.append(f"actor:{actor.id}")
    return tuple(reasons)


def _core_sir_reasons(text_lower: str) -> tuple[str, ...]:
    reasons: list[str] = []
    for pir_id, keywords in _core_sir_keywords().items():
        if any(keyword_in_text(kw, text_lower) for kw in keywords):
            reasons.append(f"sir:{pir_id}")
    return tuple(reasons)


def ingest_relevance_hint(*, feed: str, title: str, summary_preview: str) -> RelevanceHint:
    """タイトル + 概要から関連性ヒントを判定する (title+preview のみ、本文は見ない)。

    ``feed`` は国内の媒体 (購読ソースの news_jp) の判定に使う。再現率優先: 日本 / 注視国 /
    関連性の核 SIR のいずれか 1 つでも当たれば fired=True。
    """
    text = f"{title}\n{summary_preview}"
    reasons: list[str] = []

    jp = _japan_reason(text, feed)
    if jp:
        reasons.append(jp)
    reasons.extend(_watched_nation_reasons(text))
    reasons.extend(_watched_apt_reasons(text))
    reasons.extend(_core_sir_reasons(text.lower()))
    # 暗号・量子などの語は学術論文の投稿サイトに大量に当たるため、そのフィードでは単独発火させない
    if _CYBER_POLICY_CORE.search(text) and not _is_preprint_feed(feed):
        reasons.append("cyber_policy")

    # 重複除去 (順序保持)
    deduped = tuple(dict.fromkeys(reasons))
    return RelevanceHint(fired=bool(deduped), reasons=deduped)


__all__ = [
    "JAPAN_ISO",
    "WATCHED_NATIONS",
    "RelevanceHint",
    "ingest_relevance_hint",
    "invalidate_core_sir_keyword_cache",
]

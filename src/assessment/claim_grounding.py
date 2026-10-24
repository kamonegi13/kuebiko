"""claim の固有名詞が塊の記事に載っているかの決定論の関門 (DD の ``ungrounded_terms`` の後継)。

DD 判定 (2026-10-24) で違反 19 件の過半が関門の誤検出だった。直した点:

- **正規化** (全角半角・大文字小文字・空白/記号除去) した部分一致で照合する
  (``DarkWebInformer`` = ``Dark Web Informer``)。
- **カタカナ ⇔ 英字の表記ゆれ** は子音の骨格の類似度で拾う
  (``マサビスイッチ`` = ``MASABI SWITCH``)。
- **一般語** (アクセス・フィッシング・API 等) は固有名詞として数えない。カタカナの複合語は
  一般語を除いた残りだけを照合する (``ローソンアプリ`` → ``ローソン``)。
- **照合範囲**は呼び出し側が見出し・要約・本文冒頭を ``text`` に連結して渡す。
- **出典名** (``attribution`` = 媒体名・投稿者名) にだけある語は違反に数えず ``attributions``
  に分けて返す (事実の捏造ではなく帰属。ただし本文の裏付けにもならない)。

見出しにも本文冒頭にも無い CVE・機関名・製品名は引き続き違反になる (本物の混入を止める)。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

#: 固有名詞らしい語: 英数字の語で大文字か数字を含むもの (CVE-2026-1234・LockBit・APT29 等) と、
#: 3 字以上のカタカナ語。一般語 (小文字だけの英単語) と漢字語は決定論では判定しない
_LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")
_KATAKANA_RUN = re.compile(r"[ァ-ヴー]+")
_LATIN_WORD = re.compile(r"[A-Za-z0-9]+")
_MIN_KATAKANA_CHARS = 3

#: 一般語 (固有名詞として扱わない)。マルウェアの種別 (ワイパー等) は「記事に無い属性を足す」
#: 捏造の典型なので入れない。英字は NFKC + 小文字化 + 記号除去後の形で持つ
_GENERIC_KATAKANA: tuple[str, ...] = (
    "アクセス", "フィッシング", "ソフトウェア", "スキャン", "リソース", "デバイス",
    "サーバー", "サーバ", "インターネット", "ゼロデイ", "ゲートウェイ", "リモート",
    "コード", "アプリケーション", "アプリ", "システム", "ネットワーク", "サービス",
    "データベース", "データ", "セキュリティ", "サイバー", "クラウド", "アカウント",
    "ログイン", "パスワード", "メッセージ", "メール", "ウェブサイト", "ウェブ", "サイト",
    "ユーザー", "ユーザ", "プラットフォーム", "リスク", "インシデント", "ブラウザ",
    "サプライチェーン", "ハッカー", "ソース", "ツール", "プログラム", "ファイル",
    "ソフト", "ハードウェア", "ログ", "レポート", "ニュース", "アップデート", "ダウンロード",
)  # fmt: skip
_GENERIC_KATAKANA_RE = re.compile("|".join(_GENERIC_KATAKANA))
_GENERIC_LATIN: frozenset[str] = frozenset(
    {"api", "sns", "sms", "url", "web", "app", "http", "https", "html", "json", "cloud"}
)

#: 表記ゆれ照合: 骨格 (母音・w/h/y を落とした子音列) の類似度の下限と最小長
_SKELETON_MIN_LEN = 3
_SKELETON_MIN_RATIO = 0.85
_MAX_WINDOW_WORDS = 3

_KANA_CONSONANT = {
    **dict.fromkeys("カキクケコ", "k"),
    **dict.fromkeys("ガギグゲゴ", "g"),
    **dict.fromkeys("サシスセソ", "s"),
    **dict.fromkeys("ザジズゼゾ", "z"),
    **dict.fromkeys("タテト", "t"),
    "ツ": "t",
    "チ": "#",
    **dict.fromkeys("ダデド", "d"),
    **dict.fromkeys("ナニヌネノン", "n"),
    **dict.fromkeys("マミムメモ", "m"),
    **dict.fromkeys("ラリルレロ", "r"),
    **dict.fromkeys("バビブベボヴ", "b"),
    **dict.fromkeys("パピプペポ", "p"),
    "フ": "f",
}  # 母音・ヤ/ワ/ハ行・小書き・促音・長音は骨格に出さない
_LATIN_REWRITES = (("tch", "#"), ("ch", "#"), ("sh", "s"), ("ph", "f"))
_LATIN_LETTER_MAP = str.maketrans({"j": "z", "l": "r", "q": "k", "v": "b", "c": "k"})
_SKELETON_DROP = str.maketrans("", "", "aeiouwhy")


@dataclass(frozen=True)
class GroundingSources:
    """照合の材料。``text`` = 見出し・要約・本文冒頭の連結、``attribution`` = 媒体名・投稿者名。"""

    text: str
    attribution: str = ""


@dataclass(frozen=True)
class GroundingReport:
    violations: tuple[str, ...]  # 本文のどこにも無い (捏造・別記事の混入の疑い)
    attributions: tuple[str, ...]  # 出典欄にだけある (帰属。違反には数えない)


def _normalize(s: str) -> str:
    """NFKC・小文字化・文字と数字以外を除去 (空白・記号・ピリオド・ハイフンの差を吸収)。"""
    folded = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in folded if ch.isalnum() or "ぁ" <= ch <= "ヿ")


def _kana_skeleton(run: str) -> str:
    return "".join(_KANA_CONSONANT.get(ch, "") for ch in unicodedata.normalize("NFKC", run))


def _latin_skeleton(word: str) -> str:
    s = word.casefold()
    for old, new in _LATIN_REWRITES:
        s = s.replace(old, new)
    return s.translate(_LATIN_LETTER_MAP).translate(_SKELETON_DROP)


def _similar(a: str, b: str) -> bool:
    if len(a) < _SKELETON_MIN_LEN or len(b) < _SKELETON_MIN_LEN:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _SKELETON_MIN_RATIO


def _latin_windows(text: str) -> tuple[str, ...]:
    words = _LATIN_WORD.findall(unicodedata.normalize("NFKC", text))
    return tuple(
        _latin_skeleton("".join(words[i : i + n]))
        for n in range(1, _MAX_WINDOW_WORDS + 1)
        for i in range(len(words) - n + 1)
    )


def _katakana_by_sound(term: str, text: str) -> bool:
    """カタカナ語の骨格が似た英字の語 (連続する最大 3 語) が text にあるか。"""
    skel = _kana_skeleton(term)
    return any(_similar(skel, w) for w in _latin_windows(text))


def _latin_words_by_sound(claim: str, text: str) -> frozenset[str]:
    """claim の英字の連続語 (最大 3 語) が text のカタカナ語と骨格で似ていれば、その語群を返す。"""
    runs = [_kana_skeleton(r) for r in _KATAKANA_RUN.findall(unicodedata.normalize("NFKC", text))]
    words = _LATIN_WORD.findall(unicodedata.normalize("NFKC", claim))
    hit: set[str] = set()
    for n in range(1, _MAX_WINDOW_WORDS + 1):
        for i in range(len(words) - n + 1):
            skel = _latin_skeleton("".join(words[i : i + n]))
            if any(_similar(skel, r) for r in runs):
                hit.update(_normalize(w) for w in words[i : i + n])
    return frozenset(hit)


def _terms(claim: str) -> list[str]:
    """claim の固有名詞候補 (出現順、重複なし)。一般語は除く。"""
    out: list[str] = []
    for m in _LATIN_TERM.finditer(claim):
        term = m.group(0).rstrip("._-")
        if not any(c.isupper() or c.isdigit() for c in term):
            continue
        if _normalize(term) in _GENERIC_LATIN:
            continue
        out.append(term)
    for m in _KATAKANA_RUN.finditer(unicodedata.normalize("NFKC", claim)):
        for piece in _GENERIC_KATAKANA_RE.split(m.group(0)):
            if len(piece.strip("ー")) >= _MIN_KATAKANA_CHARS:
                out.append(piece)
    return list(dict.fromkeys(out))


def extract_terms(text: str) -> list[str]:
    """``text`` の固有名詞候補 (出現順、重複なし、一般語は除く)。他の関門が部品として使う。"""
    return _terms(text)


def check_grounding(claim: str, sources: GroundingSources) -> GroundingReport:
    """claim の固有名詞を ``sources`` に照らし、違反 (本文に無い) と出典名だけの語に分ける。"""
    text_norm = _normalize(sources.text)
    attr_norm = _normalize(sources.attribution)
    sound_latin = _latin_words_by_sound(claim, sources.text)
    violations: list[str] = []
    attributions: list[str] = []
    for term in _terms(claim):
        key = _normalize(term)
        if key in text_norm or key in sound_latin:
            continue
        if _KATAKANA_RUN.fullmatch(term) and _katakana_by_sound(term, sources.text):
            continue
        if key in attr_norm:
            attributions.append(term)
        else:
            violations.append(term)
    return GroundingReport(violations=tuple(violations), attributions=tuple(attributions))

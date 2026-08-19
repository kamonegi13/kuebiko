"""州・県 (admin1) 名の正規化 — 記事の表記を gazetteer のキーに寄せる (2026-08-19)。

victim_city の 39% (247 中 97 件) が地図に出ていなかった原因は、値が **州・県** で
geo_cities (都市) を引けなかったこと。判定基準は「所在都市 / **地域**」と書き例に
「カリフォルニア州」を挙げているので、**LLM は指示どおり**であり受け手を直す。

⚠ GeoNames の admin1 名は英語 (Tokushima) だが記事は「徳島県」と書く。都道府県は
**47 件の固定集合**なので長尾にならず、辞書で持って良い (countries.yaml の 117→244
のような際限ない拡張にはならない)。
"""

from __future__ import annotations

import unicodedata

# 日本の行政区画の接尾辞。「徳島県」→「徳島」に落として英語名と突き合わせる。
_JP_SUFFIXES: tuple[str, ...] = ("都", "道", "府", "県")

# 都道府県の日本語 → GeoNames admin1 の英語名 (47 件固定)。
# ⚠ GeoNames 側はマクロン付き (hyōgo / ōsaka) のため、正規化で ASCII に寄せる。
_JP_PREFECTURES: dict[str, str] = {
    "北海道": "hokkaido",
    "青森": "aomori",
    "岩手": "iwate",
    "宮城": "miyagi",
    "秋田": "akita",
    "山形": "yamagata",
    "福島": "fukushima",
    "茨城": "ibaraki",
    "栃木": "tochigi",
    "群馬": "gunma",
    "埼玉": "saitama",
    "千葉": "chiba",
    "東京": "tokyo",
    "神奈川": "kanagawa",
    "新潟": "niigata",
    "富山": "toyama",
    "石川": "ishikawa",
    "福井": "fukui",
    "山梨": "yamanashi",
    "長野": "nagano",
    "岐阜": "gifu",
    "静岡": "shizuoka",
    "愛知": "aichi",
    "三重": "mie",
    "滋賀": "shiga",
    "京都": "kyoto",
    "大阪": "osaka",
    "兵庫": "hyogo",
    "奈良": "nara",
    "和歌山": "wakayama",
    "鳥取": "tottori",
    "島根": "shimane",
    "岡山": "okayama",
    "広島": "hiroshima",
    "山口": "yamaguchi",
    "徳島": "tokushima",
    "香川": "kagawa",
    "愛媛": "ehime",
    "高知": "kochi",
    "福岡": "fukuoka",
    "佐賀": "saga",
    "長崎": "nagasaki",
    "熊本": "kumamoto",
    "大分": "oita",
    "宮崎": "miyazaki",
    "鹿児島": "kagoshima",
    "沖縄": "okinawa",
}


# 米国州のカタカナ表記 → GeoNames の英語名。⚠ 実データに「カリフォルニア州」9 件、
# 「ミネソタ州」3 件、「ワシントン州」1 件。50 州の固定集合なので長尾にならない。
# 実測で出現したものから順次足す (全 50 州を先回りで書かない = YAGNI)。
_US_STATES_JA: dict[str, str] = {
    "カリフォルニア": "california",
    "ミネソタ": "minnesota",
    "ワシントン": "washington",
    "ジョージア": "georgia",
    "ミシガン": "michigan",
    "テキサス": "texas",
    "ニューヨーク": "new york",
    "フロリダ": "florida",
    "オハイオ": "ohio",
    "イリノイ": "illinois",
    "ペンシルベニア": "pennsylvania",
    "マサチューセッツ": "massachusetts",
    "バージニア": "virginia",
    "コロラド": "colorado",
    "オレゴン": "oregon",
    "アリゾナ": "arizona",
    "ネバダ": "nevada",
    "ユタ": "utah",
}


def strip_accents(text: str) -> str:
    """マクロン等の合成記号を落とす (hyōgo → hyogo)。gazetteer 側の表記ゆれ対策。

    ⚠ **ASCII 由来の文字だけを対象にする**。日本語に当てると濁点・半濁点まで分解され
    「ジョージア」→「ショーシア」と壊れる (実データの複数州列挙で発生した)。
    """
    out: list[str] = []
    for ch in text:
        decomposed = unicodedata.normalize("NFD", ch)
        base = decomposed[0]
        # ASCII 由来の文字だけ合成記号を落とす。日本語は 1 文字そのまま通す
        # (NFD を通すと濁点 U+3099 が分離し「ジョージア」→「ショーシア」になる)。
        out.append(
            "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
            if base.isascii()
            else ch
        )
    return "".join(out)


def normalize_admin1_key(name: str, country_iso: str | None) -> str | None:
    """記事中の州・県表記を gazetteer の索引キー (小文字 ASCII) に正規化する。

    ``None`` = admin1 として解釈できない (都市名や不明値)。
    """
    if not name or not name.strip():
        return None
    cleaned = name.strip()
    country = (country_iso or "").strip().upper()

    if country == "JP" or any(cleaned.endswith(s) for s in _JP_SUFFIXES):
        base = cleaned
        for suffix in _JP_SUFFIXES:
            if base.endswith(suffix) and base != "北海道":
                base = base[: -len(suffix)]
                break
        en = _JP_PREFECTURES.get(base)
        if en is not None:
            return en

    # 米国州のカタカナ表記 (「カリフォルニア州」)。⚠ 「〜州」を落として辞書で引く。
    if cleaned.endswith("州"):
        en_us = _US_STATES_JA.get(cleaned[:-1])
        if en_us is not None:
            return en_us

    # 英語表記はそのまま (アクセント除去 + 小文字化)。
    return strip_accents(cleaned).lower() or None

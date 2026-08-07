"""ランサムグループ辞書 (ransomware.live 由来・self-updating) と news 照合。

ransomware.live が 3h ごとに取り込む group 名を「実在ランサムグループ辞書」として使い、
ニュース記事 (title+summary) に distinctive な group 名が出れば is_ransomware とみなす
(registry family=ransom_group 判定の **補完**。新グループも自動で網羅される)。

誤検知対策: ``play`` / ``medusa`` / ``royal`` / ``lynx`` 等の **一般語と紛らわしい名前**は
最小長 + denylist で除外する (語境界一致でも一般文脈で誤爆するため)。これらに帰属する被害自体は
ransomware.live 側で is_ransomware=1 になるので、地図の主データは網羅性を失わない。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# distinctive とみなす最小長 (play/nova/lynx 等の 4 文字一般語を機械的に除外)。
_MIN_LEN = 5

# 5 文字以上でも一般語/紛らわしいランサムグループ名 (auto-dict から除外、registry 側で扱う)。
# 一般のセキュリティ記事で誤爆する語 (payload/genesis/aurora/underground 等) を保守的に列挙。
_DENYLIST: frozenset[str] = frozenset(
    {
        # 既知の一般語グループ名
        "cloak",
        "royal",
        "medusa",
        "everest",
        "cactus",
        "hunters",
        "spider",
        "warlock",
        "worldleaks",
        "devman",
        "nova",
        "akira",
        # corpus 辞書に出た一般語/曖昧名 (誤検知源)
        "payload",
        "payloadbin",
        "genesis",
        "aurora",
        "underground",
        "knight",
        "abyss",
        "anubis",
        "icarus",
        "tengu",
        "termite",
        "sarcoma",
        "netrunner",
        "monti",
        "argonauts",
        "auditteam",
        "ransomed",
        "skira",
        "direwolf",
        "darkrace",
        "lamashtu",
        "cephalus",
        # 汎用トークン
        "raas",
        "group",
        "ransom",
        "ransomware",
        "leaks",
        "world",
        "data",
        "team",
        "gang",
        "crew",
        "locker",
        "ransomgroup",
    }
)

# auto-dict 一致を採用する条件: 同一テキストにランサム文脈語が共起すること (誤検知の二重防御)。
_CONTEXT_RE = re.compile(
    r"ransom|ランサム|身代金|恐喝|被害組織|リークサイト|leak site|data leak|"
    r"extort|二重恐喝|暗号化",
    re.IGNORECASE,
)


def has_ransom_context(text: str) -> bool:
    """text にランサムウェア文脈語が含まれるか (auto-dict 採用の必須条件)。"""
    return bool(text and _CONTEXT_RE.search(text))


def distinctive_names(raw_names: Iterable[str]) -> frozenset[str]:
    """ransomware.live の生 group 名から、誤検知しにくい distinctive 名のみ抽出 (小文字)。"""
    out: set[str] = set()
    for n in raw_names:
        s = (n or "").strip().lower()
        if len(s) >= _MIN_LEN and s not in _DENYLIST:
            out.add(s)
    return frozenset(out)


def _compile(names: Iterable[str]) -> re.Pattern[str] | None:
    """group 名集合を語境界つきの 1 本の正規表現に (英数の途中一致を避ける)。"""
    esc = sorted({re.escape(n) for n in names if n}, key=len, reverse=True)
    if not esc:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(esc) + r")(?![a-z0-9])", re.IGNORECASE)


def find_mention(text: str, names: Iterable[str]) -> str | None:
    """text に distinctive ランサムグループ名が語境界一致すれば、その名前を返す。"""
    if not text:
        return None
    pat = _compile(names)
    if pat is None:
        return None
    m = pat.search(text)
    return m.group(1).lower() if m else None

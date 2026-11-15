"""被害組織自身の正規ドメイン・URL が IOC として保存されるのを止める (2026-11-15)。

ランサムウェアのリーク掲載記事や侵害の告知では、被害企業の公式サイトが IOC 配列に
混ざる。被害組織の正規サイトは攻撃者のインフラではなく IOC ではない (Opus 裁定)。
被害の記録は victim_org が担う。出典ホストの除外は ``ioc_source_filter`` が担当し、
ここは「被害組織の host」だけを扱う。

⚠ 例外: 被害サイトが**改ざんされて悪性コードの配布・リダイレクトに使われた**記事
(水飲み場型) では、その host は攻撃インフラ。要約・見出しに配布/改ざんの語があれば
落とさない (``_HOSTING_CUE``)。語の網羅は保証できないため既定は shadow。

``IOC_VICTIM_HOST_FILTER``: ``0`` / ``shadow`` (既定、ログのみ) / ``1`` (保存しない)。
"""

from __future__ import annotations

import os
import re
from typing import Literal

from src.cti.ioc_source_filter import normalize_host
from src.cti.org_normalizer import org_match_key, org_names_compatible

GateMode = Literal["off", "shadow", "on"]
ENV_FLAG = "IOC_VICTIM_HOST_FILTER"

#: 登録ドメインの手前に 2 段目ラベル (co/com/…) が入る ccTLD の 2 段目
_SECOND_LEVEL = frozenset(
    {"co", "com", "or", "ne", "ac", "go", "ed", "gr", "ad", "org", "net", "gov", "edu"}
)
_MIN_LABEL_LEN = 4
_MIN_TLD_LEN_FOR_SLD = 2

#: 被害サイトが配布・リダイレクト・改ざんに使われたことを示す語 (見つかれば落とさない)
_HOSTING_CUE = re.compile(
    r"watering[\s-]?hole|drive[\s-]?by|compromised (?:web)?sites?|hosted (?:the )?malware|"
    r"host(?:ed|ing) (?:a )?(?:payload|malicious)|serv(?:e|ed|ing) malware|malicious redirect|"
    r"supply[\s-]?chain|trojani[sz]ed|update (?:server|mechanism)|third-party script|"
    r"injected (?:script|code)|defac|サプライチェーン|トロイ|水飲み場|改ざん|"
    r"マルウェア配布|不正なリダイレクト|ドライブバイ",
    re.IGNORECASE,
)


def victim_filter_mode() -> GateMode:
    raw = (os.environ.get(ENV_FLAG) or "shadow").strip().lower()
    if raw in ("0", "off", "false"):
        return "off"
    if raw in ("1", "on", "true"):
        return "on"
    return "shadow"


def registrable_label(host: str) -> str:
    """host の登録ドメインの名前部分 (``shop.acme.co.jp`` → ``acme``)。取れなければ空。"""
    parts = [p for p in host.split(".") if p]
    if len(parts) < _MIN_TLD_LEN_FOR_SLD:
        return ""
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return parts[-3]
    return parts[-2]


def registrable_domain(host: str) -> str:
    """登録ドメイン (``shop.acme.co.jp`` → ``acme.co.jp``)。"""
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


_ORG_AS_HOST = re.compile(r"^[a-z0-9][a-z0-9\-.]*\.[a-z]{2,}$")


def _org_as_host(org: str) -> str:
    """組織名がドメイン表記 (``foo[.]com`` ・パス付き含む) ならその host、でなければ空。"""
    cand = org.strip().casefold().replace("[.]", ".").replace("(.)", ".")
    cand = normalize_host(cand) if "/" in cand else cand.removeprefix("www.")
    return cand if " " not in cand and _ORG_AS_HOST.match(cand) else ""


def _label_key(label: str) -> str:
    return "".join(ch for ch in label.casefold() if ch.isalnum())


def host_matches_org(host: str, org: str) -> bool:
    """host の登録ドメイン名が被害組織名 ``org`` と同じ組織を指すか (表記ゆれ・略称まで)。"""
    label = _label_key(registrable_label(host))
    if len(label) < _MIN_LABEL_LEN:
        return False
    org_host = _org_as_host(org)
    if org_host:
        # 組織名がドメインそのもの (リーク掲載) なら登録ドメインまで一致を要求する。
        # TLD 違いは typosquat (攻撃者の登録) の可能性があり被害サイトではない
        return registrable_domain(host) == registrable_domain(org_host)
    key = org_match_key(org)
    if not key:
        return False
    # 日本語名など ASCII でない組織名は照合できない (誤落ちより取りこぼしを選ぶ)
    return org_names_compatible(label, key)


def has_hosting_cue(*texts: str | None) -> bool:
    """配布・改ざんの文脈か。見つかれば被害サイトでも攻撃インフラの可能性がある。"""
    return any(t and _HOSTING_CUE.search(t) for t in texts)


def is_victim_own_host(
    value: str,
    *,
    victim_orgs: list[str],
    context_texts: tuple[str | None, ...] = (),
) -> str | None:
    """``value`` が被害組織の正規サイトなら一致した組織名を返す (純粋判定)。なければ None。"""
    host = normalize_host(value)
    if not host or not victim_orgs:
        return None
    if has_hosting_cue(*context_texts):
        return None
    for org in victim_orgs:
        if org and host_matches_org(host, org):
            return org
    return None

"""記事本文から IOC (Indicator of Compromise) を機械的に抽出する (Phase 4)。

設計方針:
    - 正規表現ベースの **決定的抽出** (LLM の気分に依存しない、再現性あり)
    - **Defang 解除**: ``evil[.]example[.]com`` / ``hxxp://`` / ``1[.]2[.]3[.]4``
      のような防御的記法を正規化
    - **ホワイトリスト**: 解説系ドメイン (cisa.gov, attack.mitre.org 等) と
      RFC1918 等のプライベート IP を除外
    - LLM 由来の IOC とマージして重複除去

抽出対象:
    - CVE: CVE-YYYY-NNNN[N]
    - MITRE ATT&CK: T1234 / T1234.001
    - MITRE Group: G0016 等
    - IPv4 / IPv6 (グローバルのみ)
    - ドメイン (有効 TLD のみ)
    - URL (http/https/hxxp/hxxps)
    - Hash: MD5 / SHA-1 / SHA-256
    - Email アドレス

CTI 業務の現場では Grok や Inoreader の記事本文に IOC が記述されているが、
LLM 要約だけでは抽出漏れが多い。この層で網羅性を担保する。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

_log = get_logger(__name__)

# ---------- 正規表現 ----------

# CVE: CVE-YYYY-NNNN[N] (1999-2099 想定)
_CVE_RE = re.compile(r"CVE-(?:19|20)\d{2}-\d{4,7}", re.IGNORECASE)

# MITRE ATT&CK Technique: T1234 / T1234.001 (大文字小文字非区別)
_MITRE_TECH_RE = re.compile(r"\bT\d{4}(?:\.\d{3})?\b", re.IGNORECASE)

# Phase 1 Q2: ATT&CK technique ID の妥当性検証 (format + 現実的レンジ)。
# 実 ATT&CK 全集合 (~650件) の bundle は鮮度保守の負担が大きいため、format
# (T#### / T####.###) + base が ICS(T08xx)/enterprise・mobile(T1xxx) の現実的レンジ
# [800,1699] にあるかで egregious な hallucination (T9999 / T0001 / 桁数違い) を排除する。
# in-range の typo (実在する別 ID) までは捕捉しない (将来 real-set 化の余地)。
_TECH_ID_RE = re.compile(r"^T(\d{4})(?:\.(\d{3}))?$")
_TECH_BASE_MIN = 800
_TECH_BASE_MAX = 1699


def is_plausible_technique_id(tid: str) -> bool:
    """ATT&CK technique ID (T####[.###]) が format + 現実的 base レンジを満たすか。"""
    m = _TECH_ID_RE.fullmatch(tid.upper().strip())
    if not m:
        return False
    return _TECH_BASE_MIN <= int(m.group(1)) <= _TECH_BASE_MAX


# MITRE ATT&CK Group ID: G0016 等 (大文字小文字非区別)
_MITRE_GROUP_RE = re.compile(r"\bG\d{4}\b", re.IGNORECASE)

# Hash (16進、独立した token、前後がハッシュ文字でないこと)
_MD5_RE = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{32}(?![a-fA-F0-9])")
_SHA1_RE = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{40}(?![a-fA-F0-9])")
_SHA256_RE = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{64}(?![a-fA-F0-9])")

# IPv4 (4 オクテット)
_IPV4_RE = re.compile(
    r"(?<![\d.])"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\."
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\."
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\."
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?![\d.])",
)

# IPv6 (簡略、8 グループまたは :: 圧縮)
_IPV6_RE = re.compile(
    r"\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b"
    r"|"
    r"\b(?:[A-Fa-f0-9]{1,4}:){1,7}:(?:[A-Fa-f0-9]{1,4}:){0,6}[A-Fa-f0-9]{1,4}\b",
)

# URL (http(s):// または defang 形式 hxxp(s)://)
_URL_RE = re.compile(
    r"(?:https?|hxxps?)://"
    r"[A-Za-z0-9._%+\-]+(?:\[?\.\]?|\.)[A-Za-z]{2,24}"
    r"(?::\d{1,5})?"
    r"(?:/[^\s<>\"')]*)?",
    re.IGNORECASE,
)

# ドメイン (URL 抽出と部分被るが、bare domain も拾う)
# defang 形式 ``evil[.]com`` も最初に refang してから適用される前提。
_DOMAIN_RE = re.compile(
    r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+"
    r"(?:com|net|org|info|biz|co|io|tech|dev|jp|us|uk|de|fr|cn|ru|kr|tw|hk"
    r"|gov|mil|edu|int|me|tv|app|cloud|tools|systems|today|news"
    r"|onion|xyz)\b",
    re.IGNORECASE,
)

# Email アドレス
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,24}\b",
)


# ---------- ホワイトリスト ----------

# CTI 報告で頻出するが IOC ではない解説系・公式機関ドメイン。
# substring 一致 (例: "blog.cisa.gov" も "cisa.gov" でヒット)。
_BENIGN_DOMAIN_SUBSTRINGS: tuple[str, ...] = (
    # 公式機関
    "cisa.gov",
    "nist.gov",
    "fbi.gov",
    "cyber.gov.au",
    "ncsc.gov.uk",
    "jpcert.or.jp",
    "nisc.go.jp",
    "police.pref.",
    "boj.or.jp",
    "kantei.go.jp",
    "mod.go.jp",
    # 攻撃手法フレームワーク
    "attack.mitre.org",
    "mitre.org",
    "stix.mitre.org",
    # 大手セキュリティベンダ・メディア (記事中の引用先として頻出)
    "thehackernews.com",
    "bleepingcomputer.com",
    "krebsonsecurity.com",
    "darkreading.com",
    "scmagazine.com",
    "securityweek.com",
    "wired.com",
    "arstechnica.com",
    "theregister.com",
    # CTI ベンダー研究系 (Phase 5F: 出典 URL の IOC 誤抽出防止)
    "tenable.com",
    "sentinelone.com",
    "mandiant.com",
    "crowdstrike.com",
    "eset.com",
    "welivesecurity.com",
    "kaspersky.com",
    "securelist.com",
    "trendmicro.com",
    "research.trendmicro.com",
    "paloaltonetworks.com",
    "unit42.paloaltonetworks.com",
    "talosintelligence.com",
    "blog.talosintelligence.com",
    "fireeye.com",
    "sophos.com",
    "news.sophos.com",
    "checkpoint.com",
    "research.checkpoint.com",
    "withsecure.com",
    "labs.withsecure.com",
    "zscaler.com",
    "recordedfuture.com",
    "intel471.com",
    "flashpoint.io",
    "cybereason.com",
    "secureworks.com",
    "huntress.com",
    # ブログプラットフォーム (Phase 5F)
    "medium.com",
    "substack.com",
    "qiita.com",
    "note.com",
    "hatena.ne.jp",
    "dev.to",
    # ニュース系 CTI メディア (Phase 5J-3: 観測ベース拡張)
    "gbhackers.com",
    "socradar.io",
    "darkwebinformer.com",
    "infosecurity-magazine.com",
    "hackread.com",
    "securityaffairs.com",
    "cyberscoop.com",
    "therecord.media",
    "helpnetsecurity.com",
    "csoonline.com",
    "zdnet.com",
    "computerworld.com",
    "techcrunch.com",
    # 中国語 CTI / 大手メディア
    "secrss.com",
    "scmp.com",
    "xinhuanet.com",
    "people.com.cn",
    # 日本語ニュース・CTI 系
    "nikkei.com",
    "itmedia.co.jp",
    "atmarkit.itmedia.co.jp",
    "scan.netsecurity.ne.jp",
    "watch.impress.co.jp",
    # 一般大手メディア (誤抽出防止)
    "reuters.com",
    "bloomberg.com",
    "cnn.com",
    "bbc.co.uk",
    "bbc.com",
    "ft.com",
    "wsj.com",
    "nytimes.com",
    # メジャー OSS / クラウド (記事中の参照先)
    "github.com",
    "github.io",
    "gitlab.com",
    "stackoverflow.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "linkedin.com",
    "wikipedia.org",
    "google.com",
    "microsoft.com",
    "apple.com",
    "amazon.com",
    "aws.amazon.com",
    "azure.com",
    "cloudflare.com",
    # Grok / xAI 関連 (本パイプラインの出典)
    "grok.com",
    "x.ai",
    # SOC ツール / 業界標準
    "virustotal.com",
    "shodan.io",
    "censys.io",
    "abuse.ch",
)

# プライベート / ループバック IPv4 レンジ (IOC として無価値)
# CIDR 表現は使わず "1.2." 等の前方一致で軽量に判定
_BENIGN_IPV4_PREFIXES: tuple[str, ...] = (
    "0.",
    "10.",
    "127.",
    "169.254.",
    "192.168.",
    "224.",
    "255.",
    # Phase 5T-N: RFC 5737 ドキュメンテーション用予約レンジ
    "192.0.2.",  # TEST-NET-1
    "198.51.100.",  # TEST-NET-2
    "203.0.113.",  # TEST-NET-3
)
_BENIGN_IPV4_172_RE = re.compile(r"^172\.(1[6-9]|2\d|3[01])\.")

# Phase 5T-N: 著名 public DNS (悪用文脈なしでは IoC 扱いしない)
_BENIGN_PUBLIC_DNS: frozenset[str] = frozenset(
    {
        "1.1.1.1",
        "1.0.0.1",  # Cloudflare
        "8.8.8.8",
        "8.8.4.4",  # Google
        "9.9.9.9",
        "149.112.112.112",  # Quad9
        "208.67.222.222",
        "208.67.220.220",  # OpenDNS
    },
)

# Phase 5T-N: version 番号誤抽出対策の文脈キーワード
# IPv4 候補の前後 60 文字に該当キーワードがあれば「ソフトウェアバージョン」と判定して drop
# 注意: 単語境界 \b で囲むことで "server" 内の "ver" 誤マッチを防ぐ
_VERSION_CONTEXT_RE = re.compile(
    r"(?i)(?:\bversion[s]?\b|\bver\.?\s|\bbuild\b|\brelease\b|\bpatched\b|\bfixed\s+in\b"
    r"|\bupdate\b|\bfirmware\b|\bsdk\b"
    r"|\bcisco\b|\bfortinet\b|\bpan-?os\b|\bfortios\b|\bsonicos\b|\bjunos\b|\bivanti\b"
    r"|\besxi\b|\bvmware\b|\bnutanix\b|\bcitrix\b|\bapache\b|\bnginx\b|\bopenssh\b"
    r"|\bsonicwall\b|\bbarracuda\b|\bf5\s+big-ip\b|\bhakko\b|\belecom\b|\bbuffalo\b"
    r"|\bfuji\s+electric\b|\bhitachi\b|\bmitsubishi\b|\byokogawa\b|\bomron\b"
    r"|\bsiemens\b|\bschneider\b"
    r"|バージョン|未満|以前|より前|から修正|より以前)",
)

# range 表現 (X.Y.Z.W の "後" に出やすい): "and prior" / "以前" 等
_VERSION_RANGE_AFTER_RE = re.compile(
    r"(?i)\b(?:and\s+(?:prior|earlier|later|newer|above|below)"
    r"|or\s+(?:prior|earlier|later|newer))"
    r"|以前|以降|以下|より前|未満",
)

# IoC 文脈強キーワード: 全 octet 小でも IPv4 として keep する根拠
_IPV4_IOC_CONTEXT_RE = re.compile(
    r"(?i)(?:source\s+ip|dest(?:ination)?\s+ip|client\s*ip|c2|c&c|cnc|"
    r"attacker|exfil(?:trat)?|originat|callback|reach(?:ed|ing)\s+out\s+to"
    r"|communicat(?:ing|ed|es)\s+with|connect(?:ed|ing|s)\s+to|"
    r"resolved?\s+to|payload\s+from|spawn(?:ed)?\s+by|"
    r"download(?:ed|s)?\s+from|talking\s+to|"
    r"\bIPv?\.?\s*[:：]|\bIP\s*address[:：]?)",
)

# Phase 5T-N: 自記事ドメインや CDN/画像サブドメインを benign 判定する prefix list
_CDN_SUBDOMAIN_RE = re.compile(
    r"^(?:cdn|img|image[s]?|static|assets?|media|files?|video|streams?)\d*\.",
    re.IGNORECASE,
)

# Phase 5T-N: ライブラリ/ツール名で domain 誤抽出されるもの (TLD 一致するが domain でない)
_LIBRARY_PSEUDO_DOMAINS: frozenset[str] = frozenset(
    {
        "socket.io",
        "node.js",
        "next.js",
        "nuxt.js",
        "vue.js",
        "react.js",
        "ember.js",
        "express.js",
        "rxjs.dev",  # RxJS (dev TLD)
        "tailwindcss.com",  # documentation
        "rollup.js",
        "vite.dev",
        "esbuild.github.io",
    },
)

# Phase 5T-N: 追加 benign domain (sample analysis で検出されたもの)
_ADDITIONAL_BENIGN_DOMAINS: tuple[str, ...] = (
    # ジオポリ系 article で citation source として頻出する Telegram
    "t.me",
    # ジオポリ系 article の自社サイト (self-ref 防止)
    "understandingwar.org",
    "isw.org",
    "sputnikglobe.com",
    "sputniknews.com",
    "rt.com",
    "tass.com",
    "interfax.com",
    "kremlin.ru",
    "globaltimes.cn",
    "scmp.com",
    "thediplomat.com",
    "atlanticcouncil.org",
    "csis.org",
    "rusi.org",
    "crisisgroup.org",
    "rand.org",
    "jamestown.org",
    "lowyinstitute.org",
    "fdd.org",
    "aspistrategist.org.au",
    "cepa.org",
    "longwarjournal.org",
    "warontherocks.com",
    "navalnews.com",
    "spacenews.com",
    "defensenews.com",
    "breakingdefense.com",
    "usni.org",
    "news.usni.org",
    "fbi.gov",  # 念のため再掲
    # AWS / Microsoft / Google 公式サービス (記事中で言及される)
    "amazonses.com",
    "amazonaws.com",
    "azure.com",
    "azurewebsites.net",
    "azurestaticprovider.net",  # 本物のサービス
    "googleapis.com",
    "googleusercontent.com",
    "ip-api.com",  # IP lookup 公式
    # RFC 2606 ドキュメンテーション TLD/ドメイン
    "example.com",
    "example.org",
    "example.net",
    "test.com",  # 注意: 真の test.com も少数あり
    "localhost",
    "invalid",
    # Phase Diamond verify: 実データで観測された誤検出 (出典 / webmail / 政府 / vendor advisory)
    # 一般 webmail (citation context で頻出、IoC ではない)
    "163.com",
    "126.com",
    "qq.com",
    "yeah.net",
    "sina.com",
    "sina.com.cn",
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "yahoo.co.jp",
    "icloud.com",
    "protonmail.com",
    "tutanota.com",
    # 中国一般サイト (citation only)
    "weibo.com",
    "wechat.com",
    "baidu.com",
    # Vendor 公式 advisory / contact (出典 URL、IoC ではない)
    "siemens.com",
    "abb.com",
    "hitachienergy.com",
    "hitachi.com",
    "schneider-electric.com",
    "rockwellautomation.com",
    "cisco.com",
    "microsoft.com",
    "apple.com",
    "adobe.com",
    "oracle.com",
    "ibm.com",
    "vmware.com",
    "redhat.com",
    "broadcom.com",
    "fortinet.com",
    "paloaltonetworks.com",
    "checkpoint.com",
    "trendmicro.com",
    "kaspersky.com",
    "crowdstrike.com",
    "mandiant.com",
    "recordedfuture.com",
    "sentinelone.com",
    "splunk.com",
    # CERT / advisory body
    "cert.pl",
    "cert.org",
    "kb.cert.org",
    "cert.gov.uk",
    "cert-ee.eu",
    "us-cert.cisa.gov",
    "www.cisa.gov",
    "cisa.gov",
    "ncsc.gov.uk",
    "jpcert.or.jp",
    "first.org",
    "icasi.org",
    # 政府ドメイン (citation context — actor 言及 article で誤抽出)
    "gov.cn",
    "12339.gov.cn",
    "gov.jp",
    "go.jp",
    "gov.uk",
    "gov.au",
    "europa.eu",
    "eu.int",
    # 技術名 (TLD 形式に合致するが実体は library / framework)
    "asp.net",
    "system.net",
    "dot.net",
    "vb.net",
    "git-tanstack.com",  # tanstack はライブラリ名
    # security vendor 名 (citation)
    "stealthmole.com",
    # R-B (2026-06-16): CVE データベース / NVD — ioc_url の 57% (cve.org 1132件) を占めた
    # 出典参照 URL。LLM が summary.iocs に CVE 解説 URL を混入させても benign で除外する。
    "cve.org",
    "cve.mitre.org",
    "nvd.nist.gov",
    "nist.gov",
    # OSS プロジェクト公式 advisory / security ページ (citation、実データで観測)
    "mozilla.org",
    "suse.com",
    "source.android.com",
    "android.com",
    "httpd.apache.org",
    "apache.org",
    "postgresql.org",
    "kubernetes.io",
    "wordpress.org",
    "debian.org",
    "ubuntu.com",
    "python.org",
    "nodejs.org",
    "chromium.org",
    # ベンダ release notes / advisory blog (citation)
    "googleblog.com",
    "isc.sans.edu",
    "sans.org",
    "seclists.org",
    "openwall.com",
    # R-B (2026-06-16 残存浄化): 本文に実在する vendor PSIRT / advisory / release notes。
    # 脆弱性記事が本文で advisory URL を引用するため regex も拾うが、IoC ではない citation。
    "sonicwall.com",
    "ricoh.com",
    "canon-its.jp",
    "mongodb.com",
    "pgadmin.org",
    "zeropath.com",
    "intel.com",
    "fortiguard.com",
    "spring.io",
    "tomcat.apache.org",
    "lists.debian.org",
    # web archive / URL 短縮 / 学術 / シンクタンク / 政府 (citation context)
    "archive.ph",
    "archive.is",
    "web.archive.org",
    "doi.org",
    "arxiv.org",
    "t.co",
    "criticalthreats.org",
    "cfr.org",
    "aei.org",
    "iea.org",
    "anthropic.com",
    "congress.gov",
    "federalregister.gov",
    "justice.gov",
    "cert.ssi.gouv.fr",
)


# ---------- データモデル ----------


class ExtractedIocs(BaseModel):
    """記事から機械抽出した IOC の集合 (frozen)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    cves: tuple[str, ...] = ()
    mitre_techniques: tuple[str, ...] = ()
    mitre_groups: tuple[str, ...] = ()
    ipv4: tuple[str, ...] = ()
    ipv6: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()
    md5: tuple[str, ...] = ()
    sha1: tuple[str, ...] = ()
    sha256: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()

    @property
    def all_iocs(self) -> tuple[str, ...]:
        """全 IOC を 1 つのフラットなリストに統合 (BriefingMessage.iocs 用)。"""
        return (
            *self.cves,
            *self.ipv4,
            *self.ipv6,
            *self.domains,
            *self.urls,
            *self.md5,
            *self.sha1,
            *self.sha256,
            *self.emails,
        )

    @property
    def all_techniques(self) -> tuple[str, ...]:
        """MITRE 関連の集合 (mitre_techniques フィールド用)。"""
        return (*self.mitre_techniques, *self.mitre_groups)


# ---------- 公開 API ----------


def refang(text: str) -> str:
    """defang 表記 (``[.]`` / ``hxxp``) を通常表記に戻す。

    CTI 報告では accidental click 防止のため URL/ドメインを
    ``evil[.]example[.]com`` や ``hxxp://...`` のように記す慣習がある。
    抽出前にこれを refang して通常パターンで拾えるようにする。
    """
    if not text:
        return ""

    # hxxp -> http (大文字小文字保持: 'X' なら 'T'、'x' なら 't')
    def _replace_hxxp(m: re.Match[str]) -> str:
        scheme = m.group("scheme")
        x_part = m.group("x")
        rest = m.group("rest")
        # x_part 内の各 x の大小に合わせて t に変換
        t_part = "".join("T" if ch == "X" else "t" for ch in x_part)
        return scheme + t_part + rest

    refanged = re.sub(
        r"(?P<scheme>[hH])(?P<x>[xX]{1,2})(?P<rest>[pP][sS]?)",
        _replace_hxxp,
        text,
    )
    # [.] / (.)/ {.} -> .
    refanged = re.sub(r"[\[\(\{]\.[\]\)\}]", ".", refanged)
    # [@] -> @
    refanged = refanged.replace("[@]", "@").replace("(@)", "@").replace("{@}", "@")
    return refanged


def extract_iocs(text: str, *, source_url: str = "") -> ExtractedIocs:
    """テキストから IOC を全種類抽出する (Phase 5T-N: 多階層 FP 抑制)。

    入力は HTML / プレーンテキストどちらでも可 (HTML タグはノイズになるが
    パターンマッチには影響しない)。

    Args:
        text: 解析対象テキスト
        source_url: 記事の元 URL (self-ref domain 抑制用、Phase 5T-N)。
            空文字なら self-ref 抑制を無効化。

    Phase 5T-N の改良:
        - IPv4: version 番号 / public DNS / RFC 5737 文脈フィルタ
        - URL: 末尾句読点除去 + self-ref ドメイン抑制 + CDN サブドメイン除外
        - Domain: library 名 (Socket.IO 等) + 追加 benign domain + CDN 除外
    """
    if not text:
        return ExtractedIocs()
    refanged = refang(text)

    cves = _unique_preserve(_CVE_RE.findall(refanged))
    techs = _unique_preserve(_MITRE_TECH_RE.findall(refanged))
    groups = _unique_preserve(_MITRE_GROUP_RE.findall(refanged))

    # Phase 5T-N: IPv4 の version/DNS/IoC-context 文脈フィルタ
    ipv4_candidates: list[str] = []
    for m in _IPV4_RE.finditer(refanged):
        ip = m.group(0)
        if _is_benign_ipv4(ip):
            continue
        if _is_benign_public_dns_in_context(refanged, m.start(), ip):
            continue
        if _is_version_not_ip(refanged, m.start(), ip):
            continue
        ipv4_candidates.append(ip)
    ipv4 = _unique_preserve(ipv4_candidates)

    ipv6 = _unique_preserve(_IPV6_RE.findall(refanged))

    # Phase 5T-N: URL 末尾句読点除去 + self-ref ドメイン抑制 + CDN 除外
    source_host = _extract_host(source_url) if source_url else ""
    url_candidates: list[str] = []
    for raw_url in _URL_RE.findall(refanged):
        url = _strip_url_trailing_punct(raw_url)
        if not url:
            continue
        if _is_benign_url(url):
            continue
        host = _extract_host(url)
        if source_host and host and host == source_host:
            continue  # self-reference
        if host and _CDN_SUBDOMAIN_RE.search(host):
            continue  # cdn.example.com / img.example.com 等
        if host and host.lower() in _LIBRARY_PSEUDO_DOMAINS:
            continue
        url_candidates.append(url)
    urls = _unique_preserve(url_candidates)

    # Phase 5T-N: domain の library 名 / CDN サブドメイン除外
    domain_candidates: list[str] = []
    for d in _DOMAIN_RE.findall(refanged):
        if _is_benign_domain(d) or _is_part_of_url(d, urls):
            continue
        d_lower = d.lower()
        if d_lower in _LIBRARY_PSEUDO_DOMAINS:
            continue
        if _CDN_SUBDOMAIN_RE.search(d_lower):
            continue
        if source_host and d_lower == source_host:
            continue
        domain_candidates.append(d)
    domains = _unique_preserve(domain_candidates)

    md5 = _unique_preserve(_MD5_RE.findall(refanged))
    sha1_candidates: list[str] = []
    for m in _SHA1_RE.finditer(refanged):
        h = m.group(0)
        if h in md5:
            continue
        # Phase 5T-N: Git commit hash 抑制 (github.com/.../commit/HASH 文脈)
        if _is_git_commit_context(refanged, m.start()):
            continue
        sha1_candidates.append(h)
    sha1 = _unique_preserve(sha1_candidates)

    sha256 = _unique_preserve(
        h for h in _SHA256_RE.findall(refanged) if h not in md5 and h not in sha1
    )

    emails = _unique_preserve(_EMAIL_RE.findall(refanged))

    return ExtractedIocs(
        cves=tuple(cves),
        mitre_techniques=tuple(techs),
        mitre_groups=tuple(groups),
        ipv4=tuple(ipv4),
        ipv6=tuple(ipv6),
        domains=tuple(domains),
        urls=tuple(urls),
        md5=tuple(md5),
        sha1=tuple(sha1),
        sha256=tuple(sha256),
        emails=tuple(emails),
    )


async def extract_iocs_with_context(
    text: str,
    *,
    llm: object,  # LLMClient (循環 import 回避のため untyped)
) -> ExtractedIocs:
    """機械抽出 + LLM 文脈補強による IOC 抽出 (Phase 5D / L4)。

    まず ``extract_iocs`` (regex) を実行し、その結果が極端に少ない (CVE / IP /
    domain / hash / URL の合計が 0 件) ときに限り LLM に問い合わせて
    補完する。結果は元の ``ExtractedIocs`` とマージして返す。

    LLM 失敗時は regex 結果のみを返す (graceful degradation)。
    """
    base = extract_iocs(text)
    total = (
        len(base.cves)
        + len(base.ipv4)
        + len(base.ipv6)
        + len(base.domains)
        + len(base.urls)
        + len(base.md5)
        + len(base.sha1)
        + len(base.sha256)
    )
    # regex で 1 件でも取れていれば LLM 呼ばない (コスト節約)
    if total > 0 or not text or not text.strip():
        return base

    prompt = (
        "次のテキストから侵害指標 (IOC) を抽出してください。"
        "正規表現で拾えない **コンテキスト依存の IOC** に注目:\n"
        "- 略称・通称で書かれた製品 / 脆弱性 (例: 「OpenSSL の新欠陥」)\n"
        "- 引用されているがフォーマットが崩れた CVE / ハッシュ\n"
        "- 攻撃チェーンに登場するインフラ名\n\n"
        f"本文:\n{text[:3000]}\n\n"
        "出力ルール:\n"
        "- カンマ区切り 1 行で IOC のみを列挙\n"
        "- 該当なしなら空文字列を出力\n"
        "- 説明文・引用符・改行・前置きは付けない\n"
    )
    try:
        response = await llm.generate(  # type: ignore[attr-defined]
            prompt,
            temperature=0.0,
            max_tokens=200,
            think=False,
        )
    except Exception:  # noqa: BLE001
        return base
    text_out = (getattr(response, "text", "") or "").strip()
    if not text_out:
        return base
    # カンマ区切りを再度 regex に通して構造化
    return extract_iocs(text_out)


def merge_iocs(
    llm_iocs: Iterable[str],
    extracted: ExtractedIocs,
) -> list[str]:
    """LLM が抽出した IOC と機械抽出 IOC を重複なくマージする。

    Phase 5J-3: LLM 出力の defanged 表記 (``evil[.]example[.]com`` /
    ``hxxp://...``) を **refang してから保存**。Discord 表示と STIX bundle で
    表記が一貫し、アナリストがコピペした時の挙動も統一される。
    重複判定は refang + 大文字統一で正規化してから比較。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in [*llm_iocs, *extracted.all_iocs]:
        if not raw:
            continue
        # Phase 5J-3: defang 表記を refang してから出力 (idempotent。
        # extracted.all_iocs は既に refang 済だが、再適用しても変化しない)
        canonical = refang(raw).strip()
        if not canonical:
            continue
        norm = _normalize_for_dedup(canonical)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(canonical)
    return out


def filter_benign(iocs: Iterable[str]) -> list[str]:
    """IOC 文字列リストから benign (公式機関 / 大手ベンダー / OSS / SNS 等) を除外する。

    Phase 5F / 問題 7: LLM が出典 URL を IOC として返した場合の二重防御。
    URL は host を抽出して ``_is_benign_url``、ドメイン文字列は
    ``_is_benign_domain`` で判定。それ以外 (CVE / IP / hash 等) はそのまま残す。
    """
    out: list[str] = []
    for raw in iocs:
        if not raw:
            continue
        text = raw.strip()
        if not text:
            continue
        if text.startswith(("http://", "https://")) and _is_benign_url(text):
            continue
        # IPv4 でない、ドット含む文字列 → ドメイン候補
        if (
            not text.startswith(("http://", "https://"))
            and "." in text
            and not text.replace(".", "").isdigit()
            and _is_benign_domain(text)
        ):
            continue
        out.append(text)
    return out


def merge_techniques(
    llm_techniques: Iterable[str],
    extracted: ExtractedIocs,
) -> list[str]:
    """LLM 抽出 + 機械抽出の MITRE techniques を重複なくマージ。

    Phase 1 Q2: technique 風 (T+数字) だが ATT&CK の現実的レンジ外/不正形式の ID
    (LLM hallucination の T9999 / T0001 / 桁数違い) は drop する。G#### (group) や
    他種別はそのまま通す。
    """
    seen: set[str] = set()
    out: list[str] = []
    dropped: list[str] = []
    for raw in [*llm_techniques, *extracted.all_techniques]:
        if not raw:
            continue
        # T1234 と t1234 は同一視
        norm = raw.upper().strip()
        if norm in seen:
            continue
        seen.add(norm)
        # technique 風 (先頭が T + 数字) で妥当性を満たさないものは hallucination として drop
        if (
            len(norm) >= 2
            and norm[0] == "T"
            and norm[1].isdigit()
            and not is_plausible_technique_id(norm)
        ):
            dropped.append(norm)
            continue
        out.append(norm)
    if dropped:
        _log.info("mitre_technique_dropped_invalid", dropped=dropped[:10], count=len(dropped))
    return out


# ---------- internal helpers ----------


def _unique_preserve(items: Iterable[str]) -> list[str]:
    """登場順を保ちながら重複除去。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _is_benign_domain(domain: str) -> bool:
    d = domain.lower().strip(".")
    if any(b in d for b in _BENIGN_DOMAIN_SUBSTRINGS):
        return True
    # Phase 5T-N: 追加 benign list
    return any(b in d for b in _ADDITIONAL_BENIGN_DOMAINS)


def _is_benign_url(url: str) -> bool:
    u = url.lower()
    if any(b in u for b in _BENIGN_DOMAIN_SUBSTRINGS):
        return True
    return any(b in u for b in _ADDITIONAL_BENIGN_DOMAINS)


def _is_benign_ipv4(ip: str) -> bool:
    if any(ip.startswith(prefix) for prefix in _BENIGN_IPV4_PREFIXES):
        return True
    return bool(_BENIGN_IPV4_172_RE.match(ip))


def _is_benign_public_dns_in_context(text: str, match_start: int, ip: str) -> bool:
    """Phase 5T-N: well-known public DNS (1.1.1.1, 8.8.8.8 等) は
    周辺に IoC 文脈 (C2/attacker 等) が無ければ benign 扱い。
    """
    if ip not in _BENIGN_PUBLIC_DNS:
        return False
    prefix = text[max(0, match_start - 80) : match_start]
    suffix = text[match_start + len(ip) : match_start + len(ip) + 80]
    # 攻撃文脈ありなら keep (= benign 判定でない)
    return not (_IPV4_IOC_CONTEXT_RE.search(prefix) or _IPV4_IOC_CONTEXT_RE.search(suffix))


def _is_version_not_ip(text: str, match_start: int, ip: str) -> bool:
    """Phase 5T-N: IPv4 候補が実はソフトウェアバージョン番号かを判定。

    判定優先順:
        1. 前後 60 文字に version キーワード (version/cisco/etc.) → version
        2. 直後 30 文字に range expression (and prior 等) → version
        3. 全 octet が極めて小さい (≤20) かつ IoC 文脈なし → version 寄り
    """
    octets = [int(x) for x in ip.split(".")]
    prefix = text[max(0, match_start - 60) : match_start]
    suffix = text[match_start + len(ip) : match_start + len(ip) + 60]

    if _VERSION_CONTEXT_RE.search(prefix) or _VERSION_CONTEXT_RE.search(suffix):
        return True

    if _VERSION_RANGE_AFTER_RE.search(suffix[:30]):
        return True

    # 全 octet ≤ 20 のとき: IoC 文脈なしなら version 寄り
    return all(o <= 20 for o in octets) and not (
        _IPV4_IOC_CONTEXT_RE.search(prefix) or _IPV4_IOC_CONTEXT_RE.search(suffix)
    )


def _is_git_commit_context(text: str, match_start: int) -> bool:
    """Phase 5T-N: SHA-1 候補が github/gitlab commit hash かを判定。

    周辺に ``github.com/.../commit/`` や ``commit `` キーワードがあれば
    Git commit hash と判定して malware hash 扱いしない。
    """
    prefix = text[max(0, match_start - 80) : match_start].lower()
    return any(
        marker in prefix
        for marker in (
            "github.com/",
            "gitlab.com/",
            "bitbucket.org/",
            "/commit/",
            "commit ",
            "commits/",
            "git log",
            "git show",
        )
    )


def _strip_url_trailing_punct(url: str) -> str:
    """Phase 5T-N: URL 末尾に残った句読点 (``;``, ``)``, ``.``, ``。``, ``、``等) を除去。

    例: ``https://t.me/x/123;`` → ``https://t.me/x/123``
    """
    if not url:
        return url
    # 末尾の typical な引用記号類を除去 (multi-char rstrip は char-set 動作)
    trailing_chars = set(";),\"'。、,.!?>＞〕】]｡｣")
    while url and url[-1] in trailing_chars:
        url = url[:-1]
    return url


def _extract_host(url: str) -> str:
    """URL から host だけを取り出す (lowercase)。失敗時は空文字。"""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        return host
    except (ValueError, AttributeError):
        return ""


def _is_part_of_url(domain: str, urls: list[str]) -> bool:
    """ドメインが既に抽出済 URL に含まれていれば bare domain としては出さない。"""
    d = domain.lower()
    return any(d in u.lower() for u in urls)


def _normalize_for_dedup(s: str) -> str:
    """IOC 同士の重複判定用の正規化 (refang + 大文字統一 + trailing punctuation 除去)。"""
    return refang(s).strip().lower().rstrip(".,;:!?")

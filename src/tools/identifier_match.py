"""識別子照合の単独所有モジュール (docs/event_news_design.md §9)。

CVE / IP / domain / hash / version / CVSS / actor_id を対象に、
「使ってよい識別子か」「原文に本当に書かれているか」を決定論で判定する。

**なぜ ``src.assessment.evidence_verify.normalize_for_match`` を使わないか**:
あちらはピリオド・ハイフンを含む記号を丸ごと落として部分文字列 ``in`` で照合する。
識別子ではこれが致命的で、``UNC70`` が ``UNC7005`` に含まれる (誤って一致する) のを
そのまま再現してしまう (レビュー A H4)。本モジュールは NFKC + casefold のみを適用し、
**ピリオド・ハイフンは保持**したうえで、一致はトークン境界 (前後が英数字でないこと) を
必須にすることで同じ事故を構造的に防ぐ。

CVE / IP / domain / hash の抽出は ``src.cti.ioc_extractor`` の公開関数
(``extract_iocs`` / ``refang``) を再利用する (defang 対応込み、regex の複製をしない)。
version / CVSS / actor_id はここで新規に定義する (ioc_extractor の対象外)。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from src.cti.ioc_extractor import extract_iocs, refang

IdentifierKind = Literal[
    "cve", "ip", "domain", "hash", "version", "cvss", "actor_id", "proper_noun"
]

# ---------- 新規 regex (ioc_extractor の対象外) ----------

# version: major.minor[.patch]。裸の数値連結の拾いすぎを防ぐため、前後が数字・ピリオドで
# ないことを要求する (これにより IPv4 のような長い連結の内部にはマッチしない)。
_VERSION_RE = re.compile(
    r"(?<![0-9A-Za-z_.])\d{1,4}\.\d{1,4}(?:\.\d{1,5}){0,2}(?![0-9A-Za-z_.])"
)
# 2 部の数値 (1.9 等) は日本語の数量表現 (約1.9万件) と衝突する — リプレイ実測で
# 関門が正当な数量を「(原文参照)」に置換し本文を破壊した (E1'' の副作用の実物)。
# 2 部は version 文脈語が近傍に在るときだけ識別子とみなし、数量接尾辞が続くものは除外。
_VERSION_CONTEXT_RE = re.compile(
    r"version|バージョン|ビルド|リリース|patch|update|build|以前|以降|未満|系列|\bv\d",
    re.IGNORECASE,
)
_QUANTITY_SUFFIX_RE = re.compile(r"^[万億千兆件人％%ドル円倍pt]")
_VERSION_WINDOW = 14

# CVSS ベクトル文字列 (例: CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)
_CVSS_VECTOR_RE = re.compile(r"CVSS:\d\.\d(?:/[A-Z]{1,3}:[A-Z]{1,3})+", re.IGNORECASE)
# CVSS スコア表記 (例: "CVSS スコアは 9.8" / "CVSSv3.1: 7.5")
# 版数部は "v3.1" のように **v 必須** とする。v 無しの `\s*\d` を許すと
# "CVSS 10.0" の先頭 "1" を版数と誤読し、スコアを "0.0" と切り出す (2026-08-23 実測)。
_CVSS_SCORE_RE = re.compile(
    r"CVSS(?:\s*v\d(?:\.\d)?)?[^0-9]{0,20}?(?<![0-9.A-Za-z])(\d{1,2}\.\d)(?![0-9.])",
    re.IGNORECASE,
)

# 関門用の広い CVE 網 (ioc_extractor は年を 19|20 に固定しており、転記破損で年桁が
# 化けた CVE-7026-* 等を認識できない — 関門は「CVE の形をしたもの」を全部掴む。
# E1' 注入試験 2026-08-23 で実測した穴)
_CVE_LOOSE_RE = re.compile(r"(?<![A-Za-z0-9])CVE-\d{4}-\d{3,8}(?![0-9])", re.IGNORECASE)

# 関門用の広い IPv4 網: 4 групп dot 区切りで、うち 3 групп 以上が純数字なら
# 「IP の形をしたもの」とみなす (16 進化けした 2CA.254.165.112 型と、ioc_extractor が
# 良性判定で落とす文書用レンジ 203.0.113.* の捏造の両方を掴む)
# 各 group は最大 3 桁 (IPv4 オクテットの上限)。4 桁を許すと Chrome 等の 4 部版数
# (151.0.7922.170) を IP と誤認する (2026-08-23 実測)。
_IP_LOOSE_RE = re.compile(
    r"(?<![0-9A-Za-z.])([0-9A-Fa-f]{1,3})\.([0-9A-Fa-f]{1,3})\.([0-9A-Fa-f]{1,3})\.([0-9A-Fa-f]{1,3})(?![0-9A-Za-z.])"
)

# actor_id: UNC1234 / APT41 / TA505 / STORM-1234 / UAT-5647 / CL-STA-0043 系。
# \b は CJK と ASCII の境界で機能しないため actor_normalizer.py と同じ lookaround 方式を使う。
_ACTOR_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:UNC|APT|TA|STORM-|UAT-|CL-)\d+(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# 製品・ベンダ名等の ASCII 固有名詞 (CamelCase / 全大文字 / 数字混じり)。
# **緩い型 (計数のみ・本文は壊さない)**。実測で `RingCentral` → `RingCRntal` の
# ような破損が出たが、固有名詞は文法が開いており置換対象にすると誤りが増える。
# 対称照合なので、抽出器が拾いすぎても両側で相殺され誤検出にならない。
# 条件は「語中に大文字がある ASCII 語」— 製品名の型 (RingCentral / CISA / BadIIS) を
# 拾い、通常の英単語 (Security) は拾わない。破損 (RingCRntal) も同じ形なので拾える。
_PROPER_NOUN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*(?![A-Za-z0-9])"
)

# find_repair_candidate の許容規律 (docs/event_news_design.md §9)
_REPAIR_MAX_LEN_DIFF = 3
_REPAIR_MIN_COMMON_RATIO = 0.5


@dataclass(frozen=True)
class Identifier:
    """1 つの識別子 (照合単位)。"""

    kind: IdentifierKind
    raw: str
    normalized: str


def normalize_identifier(s: str) -> str:
    """NFKC + casefold + defang 復元。ピリオド・ハイフンは保持する。

    全角文字・引用符の字体差を NFKC で吸収し、``[.]`` / ``hxxp`` 等の defang 表記を
    ``refang`` (ioc_extractor と共通) で通常表記へ戻してから casefold する。
    句読点を落とす ``normalize_for_match`` とは異なり、識別子の内部構造
    (ピリオド区切りの IP / ハイフン区切りの CVE 等) は破壊しない。
    """
    nfkc = unicodedata.normalize("NFKC", s or "")
    return refang(nfkc).casefold()


def _is_word_char(ch: str) -> bool:
    """識別子の一部になりうる文字か (境界判定用)。

    ``str.isalnum()`` は **CJK 文字にも True を返す**ため、そのまま使うと
    ``影響2.10.4以前版本`` / ``バージョン2.10.4以前`` のように日本語・中国語に
    隣接した識別子が「境界が取れない = 原文に無い」と誤判定される
    (2026-08-23 実測: 本文入力への切替後、置換の大半がこの型だった)。
    識別子は ASCII の英数字で構成されるので、**ASCII 英数字のみを語中文字**とみなし、
    CJK・記号・空白はすべて境界として扱う。
    """
    return ch.isascii() and ch.isalnum()


def contains_identifier(haystack: str, ident: Identifier) -> bool:
    """``haystack`` に ``ident`` がトークン境界つきで存在するか。

    haystack 側も ``ident.normalized`` と同じ正規化 (NFKC + casefold + defang 復元) を
    行ってから比較する。一致箇所の前後が英数字でないことを要求し、
    ``UNC70`` が ``UNC7005`` に含まれる誤判定を防ぐ。
    """
    needle = ident.normalized
    if not needle:
        return False
    hay = normalize_identifier(haystack)

    start = 0
    while True:
        idx = hay.find(needle, start)
        if idx == -1:
            return False
        before_ok = idx == 0 or not _is_word_char(hay[idx - 1])
        end = idx + len(needle)
        after_ok = end == len(hay) or not _is_word_char(hay[end])
        if before_ok and after_ok:
            return True
        start = idx + 1


def _make_identifier(kind: IdentifierKind, raw: str) -> Identifier:
    return Identifier(kind=kind, raw=raw, normalized=normalize_identifier(raw))


def _dedupe(idents: list[Identifier]) -> tuple[Identifier, ...]:
    """登場順を保ちながら (kind, normalized) で重複除去する。"""
    seen: set[tuple[str, str]] = set()
    out: list[Identifier] = []
    for ident in idents:
        key = (ident.kind, ident.normalized)
        if key in seen:
            continue
        seen.add(key)
        out.append(ident)
    return tuple(out)


def extract_identifiers(text: str) -> tuple[Identifier, ...]:
    """テキストから識別子を全種別抽出する (defang 対応込み)。"""
    if not text:
        return ()

    idents: list[Identifier] = []

    # domain/hash は ioc_extractor の抽出を再利用 (defang + TLD 検証込み)。
    extracted = extract_iocs(text)
    for domain in extracted.domains:
        idents.append(_make_identifier("domain", domain))
    for digest in (*extracted.md5, *extracted.sha1, *extracted.sha256):
        idents.append(_make_identifier("hash", digest))
    for ip6 in extracted.ipv6:
        idents.append(_make_identifier("ip", ip6))

    # cve/ipv4 は関門用の広い網 (上記コメント参照 — ioc_extractor の厳格 regex は
    # 破損識別子を認識できず検査から静かに外すため、転記忠実性の検査には使わない)。
    refanged = refang(text)
    for m in _CVE_LOOSE_RE.finditer(refanged):
        idents.append(_make_identifier("cve", m.group(0)))
    for m in _IP_LOOSE_RE.finditer(refanged):
        digit_groups = sum(1 for g in m.groups() if g.isdigit())
        if digit_groups >= 3:
            idents.append(_make_identifier("ip", m.group(0)))

    # version/cvss/actor_id は本モジュール固有の regex。defang 済みテキストに対して適用する。
    ip_spans = {(m.start(), m.end()) for m in _IP_LOOSE_RE.finditer(refanged)}
    for m in _VERSION_RE.finditer(refanged):
        tok = m.group(0)
        if (m.start(), m.end()) in ip_spans:
            continue  # IP として既に採ったトークンを version として二重計上しない
        after = refanged[m.end() : m.end() + 2]
        if _QUANTITY_SUFFIX_RE.match(after):
            continue  # 数量表現 (1.9万件 等) — 識別子ではない
        if tok.count(".") < 2:
            window = refanged[max(0, m.start() - _VERSION_WINDOW) : m.end() + _VERSION_WINDOW]
            if not _VERSION_CONTEXT_RE.search(window):
                continue  # 2 部数値は version 文脈語が無ければ拾わない
        idents.append(_make_identifier("version", tok))
    for m in _CVSS_VECTOR_RE.finditer(refanged):
        idents.append(_make_identifier("cvss", m.group(0)))
    for m in _CVSS_SCORE_RE.finditer(refanged):
        idents.append(_make_identifier("cvss", m.group(1)))
    for m in _ACTOR_ID_RE.finditer(refanged):
        idents.append(_make_identifier("actor_id", m.group(0)))
    for m in _PROPER_NOUN_RE.finditer(refanged):
        idents.append(_make_identifier("proper_noun", m.group(0)))

    return _dedupe(idents)


def find_repair_candidate(broken: Identifier, allowed: Sequence[Identifier]) -> Identifier | None:
    """``broken`` を ``allowed`` 内の一意な近傍識別子へ解決する (曖昧なら None)。

    同 kind の候補が **ちょうど 1 件** だけ存在し、かつ長さ差 3 以内 かつ
    共通部分 (連続一致文字数の合計) が長い方の半分を超える場合のみ解決する。
    誤った記事へ証拠を付けるより落とすほうがましなので、条件を満たさなければ None。
    """
    same_kind = [a for a in allowed if a.kind == broken.kind]
    if len(same_kind) != 1:
        return None

    candidate = same_kind[0]
    b, c = broken.normalized, candidate.normalized
    if not b or not c:
        return None
    if abs(len(b) - len(c)) > _REPAIR_MAX_LEN_DIFF:
        return None

    matcher = SequenceMatcher(None, b, c)
    common = sum(block.size for block in matcher.get_matching_blocks())
    if common <= max(len(b), len(c)) * _REPAIR_MIN_COMMON_RATIO:
        return None

    return candidate

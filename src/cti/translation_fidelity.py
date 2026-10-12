"""翻訳の文字化け (固有名詞・識別子・数字の 1〜数文字破損) の検出関門 (2026-10-12: 検出のみ)。

⚠ 2026-10-12 方針変更: 以下の「修復」は廃止した (語の修復の約 45% が誤修復だったため)。
検出 (``detect_garbling``) だけを使い、化けた訳は保存せず画面は原文を出す。
関数名の ``find_garbled_*`` は「原文の近傍語へ解決できる = 化けの疑い」の検出器として残る。


背景 (data/eval/c2c_x_report.md): 記事本文翻訳 (``src.cti.body_translator._translate_chunk``、
モデル ``kuebiko-sft:26b``) の「重い問題あり」判定の約 1/3 は、外来語の固有名詞・数字・
識別子が 1〜数文字だけ挿入/置換/脱落/エンコード破損する現象 (例: Gitea→Gite0,
Capacitor→Capac0itor, CISA→C2A, CVE-2026-76746→CVE-2016-76746, 71→「7な7」) が原因。
再現実験で temperature=0 (決定論デコード) でも同じ崩れが固定的に出ることを確認済み
(量子化由来のバイアス、サンプリングの運ではない) — つまり **同一モデルへの単純な
リトライでは直らない可能性がある種類の誤り**。CLAUDE.md §7 の既存方針
(「禁止は指示では止まらない、決定論の関門が要る」) と同型で、プロンプトの指示では
止まらないため、翻訳後の原文/訳文の対称照合で機械的に検出・修復する。

**既存モジュールとの役割分担**:
- CVE / IP / domain / hash / version / CVSS / actor_id の厳密文法を持つ識別子は
  ``src.tools.identifier_match`` (``extract_identifiers`` / ``find_repair_candidate``)
  をそのまま再利用する (``find_garbled_identifiers``)。
- 単純な Title-case の固有名詞 (Gitea / Capacitor / Zoho 等) や短い ALLCAPS 略語
  (CISA 等) は ``identifier_match._PROPER_NOUN_RE`` (CamelCase/全大文字限定) では
  拾えないため、本モジュール独自の緩い語トークン検出器 + 編集距離照合で補う
  (``find_garbled_word_tokens``)。
- 数字の桁誤りは **自動修復しない** (曖昧さが高く誤修復のリスクが大きい)。検出のみ行い、
  呼び出し側 (``body_translator``) が再試行や記録の判断をする (``find_suspicious_numbers``)。

曖昧な場合は常に「置き換えない」側に倒す — 誤った記事へ誤った値を書き込むより、
検出漏れのほうがましという判断 (``identifier_match.find_repair_candidate`` と同じ規律)。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.tools.identifier_match import contains_identifier, extract_identifiers

# ---------- 編集距離 (Levenshtein、依存追加を避けて自前実装) ----------


def _levenshtein(a: str, b: str) -> int:
    """標準的な Levenshtein 編集距離 (挿入/削除/置換、各コスト1)。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def _is_word_char(ch: str) -> bool:
    """トークン境界判定用 (identifier_match と同じ ASCII 英数字限定の定義)。"""
    return ch.isascii() and ch.isalnum()


# ---------- 1. 単純な語トークン (固有名詞・短い略語) ----------

#: 英字の語トークン。数字開始 (16 進ハッシュ等) も許すが、純数字トークンは別途除外する。
_WORD_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])[A-Za-z0-9][A-Za-z0-9-]{2,}(?![0-9A-Za-z])")

#: 語トークンの最小長 (3 文字未満は一般短縮語との区別がつかず誤検出源になる)。
_WORD_TOKEN_MIN_LEN = 3

#: 編集距離の上限が切り替わる長さ (このトークン長以下は上限1、超えると上限2)。
#: 「長さに応じた上限 (1〜2)」の実装 — 短いトークンほど1文字の崩れでも意味が
#: 変わりやすいので上限を厳しくし、長いトークンは上限2まで許容する。
_SHORT_TOKEN_MAX_LEN = 3
_MAX_EDIT_DISTANCE_SHORT = 1
_MAX_EDIT_DISTANCE_LONG = 2

#: 既存 identifier_match で厳密文法を持つ kind (word-token 側では扱わない)。
_STRICT_IDENT_KINDS: frozenset[str] = frozenset(
    {"cve", "ip", "domain", "hash", "version", "cvss", "actor_id"}
)


@dataclass(frozen=True)
class WordRepair:
    """語トークンの修復候補 (崩れた形 → 原文の正しい形)。"""

    broken: str
    repaired: str


@dataclass(frozen=True)
class IdentifierRepair:
    """識別子 (CVE/IP/domain/hash/version/cvss/actor_id) の修復候補。"""

    kind: str
    broken: str
    repaired: str


@dataclass(frozen=True)
class NumberFlag:
    """疑わしい数字 (自動修復はしない、検出のみ)。"""

    broken: str
    nearest_source: str | None


def extract_word_tokens(text: str) -> tuple[str, ...]:
    """テキストから語トークン (3 文字以上、純数字は除く) を順序どおり抽出する。"""
    if not text:
        return ()
    out: list[str] = []
    for m in _WORD_TOKEN_RE.finditer(text):
        tok = m.group(0).strip("-")
        _append_word_token(out, tok)
        if "-" in tok:
            # ハイフン複合語 ("GSA-managed" 等) はハイフン区切りの部分語も候補に
            # 加える。実測 (data/eval/p1_report.md): 原文に "GSA-managed" しか
            # 無く単独の "GSA" がトークン化されないため、訳文の正しい単独表記
            # "GSA" が「原文に無い」と誤判定され、無関係な語への誤修復を招いた。
            # 複合語全体 (SHADOW-EARTH-065 等の識別子) も別途保持するので安全側の追加。
            for part in tok.split("-"):
                _append_word_token(out, part)
    return tuple(out)


def _append_word_token(out: list[str], tok: str) -> None:
    if len(tok) < _WORD_TOKEN_MIN_LEN:
        return
    if not any(c.isalpha() for c in tok):
        return  # 純数字 (桁の問題は find_suspicious_numbers の管轄)
    out.append(tok)


def _max_edit_distance(len_a: int, len_b: int) -> int:
    longer = max(len_a, len_b)
    if longer <= _SHORT_TOKEN_MAX_LEN:
        return _MAX_EDIT_DISTANCE_SHORT
    return _MAX_EDIT_DISTANCE_LONG


#: 英語の一般的な語尾変化 (単数/複数・時制)。トークンがこの差しかない場合は
#: 「崩れ」ではなく正当な文法的変化とみなし、修復対象から外す。実測
#: (data/eval/p1_report.md): "transfer"→"transfers" のような複数形違いが
#: 編集距離1に収まり、誤って「修復」される事故が見つかったため導入。
_COMMON_SUFFIXES: tuple[str, ...] = ("s", "es", "ed", "ing")


def _is_inflection_variant(a: str, b: str) -> bool:
    """``a``/``b`` が語幹 + 一般的な語尾変化だけの差か (大文字小文字は無視)。"""
    a_cf, b_cf = a.casefold(), b.casefold()
    shorter, longer = (a_cf, b_cf) if len(a_cf) <= len(b_cf) else (b_cf, a_cf)
    return any(longer == shorter + suf for suf in _COMMON_SUFFIXES)


def _is_plain_lowercase_word(tok: str) -> bool:
    """``tok`` が数字・ハイフンを含まない全小文字の ASCII 語か (=普通の英単語らしい)。"""
    return tok.isascii() and tok.isalpha() and tok.islower()


def _unique_nearest(
    token: str, candidates: Sequence[str], *, exclude_plain_lowercase: bool = False
) -> str | None:
    """``token`` に最も近い原文候補を一意に返す (曖昧 — 最小距離が複数 — なら None)。

    長さに応じた上限 (``_max_edit_distance``) を超える距離・単純な語尾変化の差は
    候補から除外する。残った候補のうち編集距離が最小のものが **ちょうど1件** の
    場合のみ採用する — 「上限内に複数の候補が存在するが一方が明確に近い」場合を
    誤って曖昧判定しないための最小距離タイブレーク (実測で判明: 上限内の候補を
    全部集めて1件でなければ諦める方式だと、"Gite0" に対し "Gitea"(距離1) と
    "Git"(距離2) の両方が候補に入り、本来一意に解けるはずの "Gitea" への修復が
    "Git" の存在だけでブロックされていた)。

    ``exclude_plain_lowercase``: True なら全小文字・数字無しの候補 (普通の英単語)
    を除外する (語トークン経路専用。識別子経路の domain/hash は正当に小文字なので
    適用しない)。実測: この除外が無いと "TTP"→"top" のように、意図的に原文表記を
    残した短い略語が無関係な普通の英単語に「修復」される事故が多発した
    (固有名詞・識別子・ハッシュは実測で例外なく大文字/数字/ハイフンを含んでいた)。
    """
    token_cf = token.casefold()
    best_dist: int | None = None
    best: dict[str, str] = {}
    for cand in candidates:
        if exclude_plain_lowercase and _is_plain_lowercase_word(cand):
            continue
        cand_cf = cand.casefold()
        if cand_cf == token_cf or _is_inflection_variant(token, cand):
            continue
        limit = _max_edit_distance(len(token), len(cand))
        if abs(len(cand) - len(token)) > limit:
            continue
        dist = _levenshtein(token_cf, cand_cf)
        if dist == 0 or dist > limit:
            continue
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = {cand_cf: cand}
        elif dist == best_dist:
            best.setdefault(cand_cf, cand)
    if best_dist is None or len(best) != 1:
        return None
    return next(iter(best.values()))


def _strict_identifier_raw_strings(text: str) -> frozenset[str]:
    """既存 identifier_match が厳密文法で扱う kind の生文字列集合 (語トークン側で除外用)。"""
    return frozenset(i.raw for i in extract_identifiers(text) if i.kind in _STRICT_IDENT_KINDS)


def find_garbled_word_tokens(source: str, output: str) -> tuple[WordRepair, ...]:
    """訳文の語トークンのうち、原文に無く編集距離が近い原文トークンへ一意に解決できるものを返す。

    原文の語が訳文にそのまま現れないこと自体は問わない (正しいカタカナ化を咎めない)。
    候補が複数・曖昧な場合は置き換えない。
    """
    if not output:
        return ()
    source_tokens = extract_word_tokens(source)
    if not source_tokens:
        return ()
    source_cf = {t.casefold() for t in source_tokens}
    skip = _strict_identifier_raw_strings(output)

    repairs: list[WordRepair] = []
    seen_broken: set[str] = set()
    for tok in extract_word_tokens(output):
        cf = tok.casefold()
        if cf in source_cf or cf in seen_broken or tok in skip:
            continue
        candidate = _unique_nearest(tok, source_tokens, exclude_plain_lowercase=True)
        if candidate is None:
            continue
        seen_broken.add(cf)
        repairs.append(WordRepair(broken=tok, repaired=candidate))
    return tuple(repairs)


# ---------- 2. 識別子 (CVE/IP/domain/hash/version/cvss/actor_id) ----------


def _cve_number_bare_in(source: str, raw: str) -> bool:
    """CVE の連番が、原文に「前に数字・ハイフンの無い単独の数字列」として存在するか。

    原文が "CVE-2026-" と連番を別の行・セルに分けている場合、訳が接頭辞を復元して正しい
    CVE 番号になる (2026-10-12 実測: 14 日の識別子ヒットの誤検出型)。年だけ化けた訳
    (原文 CVE-2026-N → 訳 CVE-2020-N) は、原文の N が "-" の直後にあるため該当しない。
    """
    m = re.fullmatch(r"CVE-\d{4}-(\d{4,})", raw, re.IGNORECASE)
    if m is None:
        return False
    return re.search(rf"(?<![\d-]){m.group(1)}(?!\d)", source) is not None


def find_garbled_identifiers(source: str, output: str) -> tuple[IdentifierRepair, ...]:
    """訳文の識別子のうち、原文に無く同一 kind の一意な近傍識別子へ解決できるものを返す。

    ``identifier_match.find_repair_candidate`` は「同 kind がちょうど1件」を要求するが、
    これは CVE を 5〜10 件列挙する脆弱性アドバイザリ記事 (kuebiko の主要な記事種別) では
    ほぼ常に不成立になる (実測: data/eval/p1_report.md)。本モジュールは代わりに
    ``_unique_nearest`` (最小編集距離タイブレーク) を同 kind の候補集合に適用する —
    候補が複数あっても、そのうち編集距離が最小かつ一意なものがあれば解決できる。
    """
    if not output:
        return ()
    source_idents = extract_identifiers(source)
    if not source_idents:
        return ()
    by_kind: dict[str, list[str]] = {}
    for i in source_idents:
        by_kind.setdefault(i.kind, []).append(i.raw)

    repairs: list[IdentifierRepair] = []
    seen: set[str] = set()
    for ident in extract_identifiers(output):
        if ident.kind not in _STRICT_IDENT_KINDS:
            continue
        if ident.normalized in seen:
            continue
        if contains_identifier(source, ident):
            continue  # 原文にそのまま存在 → 化けではない
        if _cve_number_bare_in(source, ident.raw):
            continue  # 原文で接頭辞 "CVE-YYYY-" だけ欠落 (表の折り返し等) → 復元で化けではない
        candidate = _unique_nearest(ident.raw, by_kind.get(ident.kind, []))
        if candidate is None:
            continue
        seen.add(ident.normalized)
        repairs.append(IdentifierRepair(kind=ident.kind, broken=ident.raw, repaired=candidate))
    return tuple(repairs)


# ---------- 3. 数字 (自動修復はしない、検出のみ) ----------

#: カンマ区切り・パーセント記号付きの数字 (例: "3,125" / "36" / "250%")
_NUMBER_RE = re.compile(r"(?<![0-9.])\d[\d,]*%?(?![0-9.])")

#: 比較対象とする最小桁数。実測 (data/eval/p1_report.md): 単独の1桁数字は
#: 日本語の助数詞表現 ("1つ"/"3日間"/"2隻目" 等) で記事全体に大量に出現し、
#: たまたま編集距離1になる無関係な数字どうしを無差別に「疑わしい」と誤検出する
#: ノイズ源だった。2桁以上に絞ることでこの主要な誤検出源を除く。
_MIN_NUMBER_DIGITS = 3

#: 挿入/削除で長さが変わりうる許容範囲
_NUMBER_LEN_DIFF_MAX = 1
#: 同じ桁数の置換、または1桁挿入/削除のみを「近傍」とみなす (編集距離1)
_NUMBER_MAX_EDIT_DISTANCE = 1

#: ひらがな/カタカナが数字の間に 1 文字だけ挟まる破損パターン (例: "7な7")。
#: かな限定とすることで「3月4日」等の正当な日付表現 (漢字) を誤検出しない。
_DIGIT_INTERLEAVED_RE = re.compile(r"(?<!\d)\d[ぁ-んァ-ヶ]\d(?!\d)")


def _normalize_number(num: str) -> tuple[str, bool]:
    pct = num.endswith("%")
    digits = num[:-1] if pct else num
    return digits.replace(",", ""), pct


def extract_numbers(text: str) -> tuple[str, ...]:
    """テキストから数字トークン (カンマ区切り・%付き含む) を順序どおり抽出する。"""
    if not text:
        return ()
    return tuple(m.group(0) for m in _NUMBER_RE.finditer(text))


def _nearest_off_by_digit(num: str, source_numbers: Sequence[str]) -> str | None:
    """``num`` に最も近い (桁誤りの疑いがある) 原文の数字を返す。

    数字は自動修復しない (検出のみ) ため、語トークン・識別子の修復とは異なり
    一意性までは要求しない — 最小編集距離の候補を1つ返す (どれを指しているかの
    参考情報であり、確定した対応ではない)。実測 (data/eval/p1_report.md):
    一意性を要求すると、本来の対応先 (例 "3,125") と無関係な短い数字 (例 "120",
    別箇所の数値への偶然の近さ) が両方候補に入り、一意性が崩れて検出自体が
    できなくなるケースが大半だった。
    """
    digits, pct = _normalize_number(num)
    best: str | None = None
    best_dist: int | None = None
    for cand in source_numbers:
        cand_digits, cand_pct = _normalize_number(cand)
        if pct != cand_pct or cand_digits == digits:
            continue
        if abs(len(cand_digits) - len(digits)) > _NUMBER_LEN_DIFF_MAX:
            continue
        dist = _levenshtein(digits, cand_digits)
        if dist <= _NUMBER_MAX_EDIT_DISTANCE and (best_dist is None or dist < best_dist):
            best_dist = dist
            best = cand
    return best


def _digit_count(num: str) -> int:
    return len(_normalize_number(num)[0])


def find_suspicious_numbers(source: str, output: str) -> tuple[NumberFlag, ...]:
    """訳文の数字のうち、原文に無く桁誤りの疑い/かな混入破損があるものを検出する (修復はしない)。

    比較対象は 2 桁以上の数字に限る (``_MIN_NUMBER_DIGITS``) — 単独の1桁数字は
    助数詞表現のノイズが支配的なため。
    """
    if not output:
        return ()
    source_numbers = [n for n in extract_numbers(source) if _digit_count(n) >= _MIN_NUMBER_DIGITS]
    source_normalized = {_normalize_number(n) for n in source_numbers}

    flags: list[NumberFlag] = []
    seen: set[str] = set()
    for num in extract_numbers(output):
        if _digit_count(num) < _MIN_NUMBER_DIGITS:
            continue
        if _normalize_number(num) in source_normalized or num in seen:
            continue
        nearest = _nearest_off_by_digit(num, source_numbers)
        if nearest is not None:
            seen.add(num)
            flags.append(NumberFlag(broken=num, nearest_source=nearest))

    for m in _DIGIT_INTERLEAVED_RE.finditer(output):
        tok = m.group(0)
        if tok in seen:
            continue
        seen.add(tok)
        flags.append(NumberFlag(broken=tok, nearest_source=None))

    return tuple(flags)


# ---------- 統合 ----------


@dataclass(frozen=True)
class GarbleReport:
    """``detect_garbling`` の結果 (検出のみ。訳文は一切書き換えない)。"""

    word_hits: tuple[WordRepair, ...]
    identifier_hits: tuple[IdentifierRepair, ...]
    number_flags: tuple[NumberFlag, ...]

    @property
    def is_garbled(self) -> bool:
        return bool(self.word_hits or self.identifier_hits or self.number_flags)

    @property
    def total(self) -> int:
        return len(self.word_hits) + len(self.identifier_hits) + len(self.number_flags)


#: 原文側の CVE 表記ゆれ ("CVE-2026- 57941" のようにハイフン後へ空白が入る) を詰める。
#: 訳文では空白が詰まって出るため、詰めないと原文に無い識別子と誤検出する (2026-10-12、
#: 14 日の識別子検出 41 件中 1 件・ヒット 185 件中 4 件で実測)。
_CVE_SPACED_RE = re.compile(r"(CVE)\s*-\s*(\d{4})\s*-\s*(\d{4,})", re.IGNORECASE)


def _squeeze_cve_spacing(text: str) -> str:
    return _CVE_SPACED_RE.sub(r"\1-\2-\3", text)


def detect_garbling(source: str, output: str) -> GarbleReport:
    """原文 ``source`` を基準に訳文 ``output`` の化けを検出する (純粋関数、修復はしない)。

    2026-10-12: 語の修復は 29 件中 13 件が誤修復だったため「直す」をやめ、
    検出だけを使って化けた訳を保存させない (呼び出し側 ``body_translator``)。
    """
    source = _squeeze_cve_spacing(source)
    output = _squeeze_cve_spacing(output)
    return GarbleReport(
        word_hits=find_garbled_word_tokens(source, output),
        identifier_hits=find_garbled_identifiers(source, output),
        number_flags=find_suspicious_numbers(source, output),
    )

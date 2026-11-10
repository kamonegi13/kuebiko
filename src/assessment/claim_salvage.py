"""忠実さの関門で止まった claim の付け足しの節を削って照合し直す (DETECT-RECALL、既定 off)。

分けた形の書き手は入力の付帯情報 (媒体名・時系列の「再浮上」) を、2 文目・「〜が報じた」・
「再浮上」・根拠に無い月日として claim に写す (11-10 の分解)。止めた判断はほぼ正しいが、約 7 割は
付け足しの節を削れば正しい claim になる。ここは**削るだけ**で言い換えも補いもしない:

- 2 文目以降を落とす (``multi_sentence``)
- 出典名を含む節を落とす: 「…と〇〇が報じた」→「…と報じられている」(確度の留保は残す)・
  「〇〇によると、…」・括弧書きの出典 (``outlet:名``)
- 根拠に無い「再浮上/再燃」を含む括弧・節を落とす (``unsupported:語``)
- 根拠に無い月日の語を落とす (``date:M月D日``)

削った結果を**同じ関門** (忠実さ + 固有名詞) に通し直し、通ったときだけ返す。短すぎる・
主語が消えた・何も削れなかったときは None (採用しない)。出典名の節は、関門が出典名として
指摘した名前を含むものだけ削る (別の主体を誤って削らない)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.assessment.claim_fidelity import check_fidelity, first_sentence, fold_text
from src.assessment.claim_grounding import GroundingSources, check_grounding, extract_terms

#: 削った結果の最小字数 (句点を除く)。これ未満は主語や述部が消えたとみなして採用しない
MIN_SALVAGED_CHARS = 15
#: 文頭に来ない助詞・読点 (来たら主語が消えている)
_LEADING_PARTICLES = frozenset("はがをにでとのもへや、，,")
_HEDGE = re.compile(r"と報じ|と伝え|とされ|とみられ|とみて|可能性|とのこと|と指摘")
_HEDGE_TEXT = "と報じられている"
#: 出典名の語。「と」「が」「は」を含めない (含めると手前の節まで巻き込んで述部を壊す)
_WHO = r"[^、。，,（）()とがは]{1,40}?"
_REPORT_VERB = r"(?:報じ|伝え)"
_REPORT_END = r"(?:た|ている|ており|ています|ました|られている)"
#: 「…、と〇〇が報じた」→ 留保だけ残す
_TO_WHO_REPORTED = re.compile(rf"[、，,]?と(?P<who>{_WHO})(?:が|は|も){_REPORT_VERB}{_REPORT_END}")
#: 「〇〇によると、…」
_PREFIX_ACCORDING = re.compile(
    rf"^(?P<who>{_WHO})(?:の報道によると|の報道によれば|の報道では|によると|によれば|"
    r"が報じたところでは|の調べでは)[、，,]?"
)
#: 「〇〇は、…と報じた。」
_SUBJECT_REPORTED = re.compile(
    rf"^(?P<who>{_WHO})(?:は|が)[、，,]?(?P<body>[^。]+?)と{_REPORT_VERB}"
    r"(?:た|ている|ており|ました)。?$"
)
_PAREN = re.compile(r"[（(](?P<in>[^（）()]*)[）)]")
_MARKER_PREFIX = "unsupported:"
_DATE_PREFIX = "date:"
_OUTLET_PREFIX = "outlet:"


@dataclass(frozen=True)
class SalvageResult:
    claim: str
    removed: tuple[str, ...]  # 削った型 (second_sentence / outlet / unsupported:語 / date)


def _names(flags: tuple[str, ...], prefix: str) -> tuple[str, ...]:
    return tuple(f[len(prefix) :] for f in flags if f.startswith(prefix))


def _mentions(text: str, outlet_names: tuple[str, ...]) -> bool:
    folded = fold_text(text)
    return any(n in folded for n in outlet_names)


def _ensure_hedge(text: str) -> str:
    body = text.strip().rstrip("。")
    return f"{body}。" if _HEDGE.search(body) else f"{body}{_HEDGE_TEXT}。"


def _strip_outlet(claim: str, outlet_names: tuple[str, ...]) -> str:
    """出典名を含む節を削る (指摘された名前を含むものだけ)。"""
    out = claim
    m = _TO_WHO_REPORTED.search(out)
    if m and _mentions(m.group("who"), outlet_names):
        out = f"{out[: m.start()]}{_HEDGE_TEXT}{out[m.end() :]}"
    m = _PREFIX_ACCORDING.match(out)
    if m and _mentions(m.group("who"), outlet_names):
        out = _ensure_hedge(out[m.end() :])
    m = _SUBJECT_REPORTED.match(out)
    if m and _mentions(m.group("who"), outlet_names):
        out = _ensure_hedge(m.group("body"))
    return _PAREN.sub(lambda p: "" if _mentions(p.group("in"), outlet_names) else p.group(0), out)


def _strip_marker(claim: str, word: str) -> str:
    """「再浮上」等を含む括弧と、最後でない節を削る (最後の節は述部を含むので削らない)。"""
    out = _PAREN.sub(lambda p: "" if word in p.group("in") else p.group(0), claim)
    clauses = re.split(r"(?<=、)", out)
    kept = [c for i, c in enumerate(clauses) if i == len(clauses) - 1 or word not in c]
    return "".join(kept)


def _strip_date(claim: str, month: int, day: int) -> str:
    pat = re.compile(
        rf"(?:\d{{4}}年)?0?{month}月0?{day}日(?:頃|ごろ)?(?:には|に|の|から|まで|付で|時点で)?"
    )
    return pat.sub("", claim)


def _tidy(text: str) -> str:
    out = re.sub(r"[、，]{2,}", "、", text.strip())
    out = re.sub(r"、(?=。)", "", out)
    return out if out.endswith("。") or not out else f"{out}。"


def _acceptable(candidate: str, original_head: str) -> bool:
    body = candidate.rstrip("。").strip()
    if len(body) < MIN_SALVAGED_CHARS or body[0] in _LEADING_PARTICLES:
        return False
    terms = extract_terms(original_head)
    return not terms or any(t in candidate for t in terms)


def _apply_flags(claim: str, sources: GroundingSources) -> tuple[str, list[str]]:
    """型ごとに 1 回ずつ、前の段で削った後の claim の違反に対して削る。(claim, 削った型)。"""
    cur, removed = claim, list[str]()
    if "multi_sentence" in check_fidelity(cur, sources):
        cur, removed = first_sentence(cur), [*removed, "second_sentence"]
    outlets = _names(check_fidelity(cur, sources), _OUTLET_PREFIX)
    if outlets:
        cur, removed = _strip_outlet(cur, outlets), [*removed, "outlet"]
    for word in _names(check_fidelity(cur, sources), _MARKER_PREFIX):
        cur, removed = _strip_marker(cur, word), [*removed, f"{_MARKER_PREFIX}{word}"]
    for stamp in _names(check_fidelity(cur, sources), _DATE_PREFIX):
        m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", stamp)
        if m:
            cur = _strip_date(cur, int(m.group(1)), int(m.group(2)))
            removed = [*removed, "date"]
    return cur, removed


def salvage_claim(claim: str, sources: GroundingSources) -> SalvageResult | None:
    """止まった claim を削って関門に通し直す。通れば削った claim と型、通らなければ None。"""
    if not check_fidelity(claim, sources):
        return None
    cur, removed = _apply_flags(claim.strip(), sources)
    cur = _tidy(cur)
    if cur == _tidy(claim) or not _acceptable(cur, first_sentence(claim)):
        return None
    if check_fidelity(cur, sources) or check_grounding(cur, sources).violations:
        return None
    return SalvageResult(claim=cur, removed=tuple(dict.fromkeys(removed)))

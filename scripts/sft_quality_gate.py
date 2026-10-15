#!/usr/bin/env python3
"""SFT 学習データの品質関門 (2026-10-15、K1)。

背景: 09-27 の監査 (data/mlx/audit_sft_dataset.py) が組み立てに組み込まれず、s22/s23 で
実行されなかった。契約外の欄・別記事の書き直しの取り違え・本文に無い CVE・detect の複製・
課題の印の取りこぼしが学習データに残った。ここでは **学習の前に必ず通る関門** として
例ごとに検査し、1 つでも落ちたら exit 1 (学習を起動させない)。

    uv run --with tokenizers python scripts/sft_quality_gate.py data/mlx/dataset_s23 \
        --manifest data/mlx/dataset_s23_manifest.json --baseline data/mlx/dataset_s22

トークナイザ (`tokenizers`) が使えなければ文字数で近似し、結果に approximate と明記する。
build_dataset_s24.py の最後が `run_gate()` を呼ぶ。GPU・外部 LLM は使わない。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sft_gate_schemas import SCHEMA_CHECKS, _summary_contract  # noqa: E402

from src.pipeline.summary_runaway import _is_runaway_text  # noqa: E402

TASK_MARK = re.compile(r"\[task: (\w+)\]")
#: 知識 QA (印の無い行) の型
KNOWLEDGE_QA = re.compile(r"^(Explain|Describe|What|Which|List|Summarize)\b")
KNOWLEDGE_QA_MAX_CHARS = 400
#: 課題ごとのトークン上限 (学習の --max-seq。n 系は --limit task=N で上書き)
DEFAULT_MAX_SEQ = 12500
#: 暴走の閾値 (summary_runaway と CLAUDE.md の暴走定義)
SUMMARY_MAX_CHARS = 2000
REMEDIATION_MAX_CHARS = 1000
TTP_MAX = 20
#: 長さの基準版との比 (p50/p90/p99)
LENGTH_RATIO_MAX = 1.2
MIN_TITLE_FOR_GUARD = 8
#: detect の評価窓の最古日 (CLAUDE.md 2026-09-25: 評価窓 09-10..09-23 を除外)
DEFAULT_EVAL_OLDEST = "2026-09-10"
PROVENANCE_FIELDS = ("teacher", "effort", "rubric", "input_sha")
_EMPTY_THOUGHT = "<|channel>thought\n<channel|>"
HF_TOKENIZER_GLOB = "models--mlx-community--gemma-4-26b-a4b-it-4bit/snapshots/*"

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
HASH_RE = re.compile(
    r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40}|[0-9a-fA-F]{32})(?![0-9a-fA-F])"
)
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2} 〜")

FREE_TEXT_TASKS = frozenset({"pir_focus", "none"})


# ---------------------------------------------------------------------------
# 入力の読み込み
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    split: str
    idx: int
    task: str  # 全 message (system 含む) から特定した印
    build_task: str  # 組み立てスクリプトが使う user のみの判定 (差があれば取りこぼし)
    marks: tuple[str, ...]
    messages: tuple[dict[str, str], ...]
    meta: dict[str, Any]
    out: str
    prompt: str  # assistant 以外の連結
    user: str
    body: str

    @property
    def label(self) -> str:
        return f"{self.split}:{self.idx}"


def _summary_body(user: str) -> str:
    start = user.find("【記事 本文】")
    if start < 0:
        return user
    end = user.find("\n# 出力スキーマ", start)
    return user[start : end if end > 0 else len(user)]


def _make_row(split: str, idx: int, raw: dict[str, Any]) -> Row:
    messages = tuple(raw["messages"])
    prompt = "\n".join(m["content"] for m in messages if m["role"] != "assistant")
    user = "\n".join(m["content"] for m in messages if m["role"] == "user")
    marks = tuple(dict.fromkeys(TASK_MARK.findall(prompt)))
    build = TASK_MARK.search(user)
    task = marks[0] if marks else "none"
    return Row(
        split=split,
        idx=idx,
        task=task,
        build_task=build.group(1) if build else "none",
        marks=marks,
        messages=messages,
        meta=dict(raw.get("meta") or {}),
        out=messages[-1]["content"],
        prompt=prompt,
        user=user,
        body=_summary_body(user) if task == "summary" else prompt,
    )


def load_rows(dataset_dir: Path) -> list[Row]:
    rows: list[Row] = []
    for split in ("train", "valid"):
        path = dataset_dir / f"{split}.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            rows.extend(
                _make_row(split, i, json.loads(line))
                for i, line in enumerate(x for x in fh if x.strip())
            )
    return rows


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def strings(x: object) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for v in x.values() for s in strings(v)]
    if isinstance(x, list):
        return [s for v in x for s in strings(v)]
    return []


@dataclass
class CheckResult:
    name: str
    violations: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
    uninspectable: dict[str, int] = field(default_factory=dict)
    informational: bool = False

    @property
    def passed(self) -> bool:
        return self.informational or self.violations == 0

    def add_example(self, text: str, limit: int = 8) -> None:
        if len(self.examples) < limit:
            self.examples.append(text)


# ---------------------------------------------------------------------------
# 1. 課題の印
# ---------------------------------------------------------------------------


def check_task_marks(rows: list[Row], manifest: dict[str, Any]) -> CheckResult:
    res = CheckResult("1_task_marks")
    outside_user: Counter[str] = Counter()
    for r in rows:
        if len(r.marks) > 1:
            res.violations += 1
            res.detail["multiple_marks"] = res.detail.get("multiple_marks", 0) + 1
            res.add_example(f"{r.label} 印が複数 {r.marks}")
        if not r.marks and not _is_knowledge_qa(r):
            res.violations += 1
            res.detail["unmarked_not_qa"] = res.detail.get("unmarked_not_qa", 0) + 1
            res.add_example(f"{r.label} 印が無く知識 QA でもない: {r.user[:60]!r}")
        if r.task != r.build_task:
            res.violations += 1
            outside_user[r.task] += 1
    if outside_user:
        res.detail["mark_not_in_user_message"] = dict(outside_user)
    res.detail["rows_per_task"] = dict(Counter(r.task for r in rows))
    res.violations += _manifest_count_mismatches(rows, manifest, res)
    return res


def _is_knowledge_qa(r: Row) -> bool:
    return (
        bool(KNOWLEDGE_QA.match(r.user))
        and len(r.user) <= KNOWLEDGE_QA_MAX_CHARS
        and _try_json(r.out) is None
    )


def _manifest_count_mismatches(rows: list[Row], manifest: dict[str, Any], res: CheckResult) -> int:
    counts = manifest.get("task_counts") or {}
    if not counts:
        res.detail["manifest"] = "task_counts が無い (件数照合不能)"
        return 1
    by = Counter((r.split, r.task) for r in rows)
    mismatches = 0
    carry_tasks: tuple[str, ...] = ()
    for task, c in counts.items():
        if task == "carry_over":
            carry_tasks = tuple(c.get("tasks") or ())
            continue
        for split in ("train", "valid"):
            want, got = c.get(f"new_{split}"), by[(split, task)]
            if want is not None and want != got:
                mismatches += 1
                res.add_example(f"manifest {task} {split}: 期待 {want} / 実際 {got}")
    carry = counts.get("carry_over")
    if carry:
        for split in ("train", "valid"):
            got = sum(by[(split, t)] for t in carry_tasks)
            if carry.get(split) != got:
                mismatches += 1
                res.add_example(
                    f"manifest carry_over {split}: 期待 {carry.get(split)} / 実際 {got}"
                )
    return mismatches


# ---------------------------------------------------------------------------
# 2. 出力の schema
# ---------------------------------------------------------------------------


def _free_text_errors(out: str) -> list[str]:
    text = out.strip()
    if text.startswith(("{", "[", "```")) or _try_json(text) is not None:
        return ["wrapped_in_json"]
    return []


def check_schema(rows: list[Row]) -> CheckResult:
    res = CheckResult("2_output_schema")
    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    nonempty_extra: Counter[str] = Counter()
    for r in rows:
        if r.task == "summary":
            nonempty_extra.update(_nonempty_extra_keys(r))
        if r.task in FREE_TEXT_TASKS:
            errs = _free_text_errors(r.out)
        elif r.task in SCHEMA_CHECKS:
            j = _try_json(r.out)
            errs = ["not_json"] if j is None else SCHEMA_CHECKS[r.task](j)
        else:
            res.uninspectable[r.task] = res.uninspectable.get(r.task, 0) + 1
            continue
        if errs:
            res.violations += 1
            reasons[r.task].update(errs)
            res.add_example(f"{r.label} {r.task}: {errs[:3]}")
    res.detail["failing_rows_reasons"] = {t: dict(c.most_common(12)) for t, c in reasons.items()}
    res.detail["summary_extra_key_with_nonempty_value"] = dict(nonempty_extra)
    return res


def _nonempty_extra_keys(r: Row) -> list[str]:
    j = _try_json(r.out)
    if not isinstance(j, dict):
        return []
    required, _ = _summary_contract()
    return [k for k, v in j.items() if k not in required and v not in (None, "", [], {})]


# ---------------------------------------------------------------------------
# 3. 出どころ / 4. 入力 sha
# ---------------------------------------------------------------------------


def _provenance(r: Row, manifest: dict[str, Any]) -> dict[str, Any]:
    base = (manifest.get("provenance") or {}).get(r.task) or {}
    merged = {**base, **{k: v for k, v in r.meta.items() if v not in (None, "")}}
    return {k: merged.get(k) for k in PROVENANCE_FIELDS if merged.get(k) not in (None, "")}


def check_provenance(rows: list[Row], manifest: dict[str, Any]) -> CheckResult:
    res = CheckResult("3_provenance")
    missing: Counter[str] = Counter()
    versions: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.task == "none":
            continue  # 知識 QA は教師の出どころを持たない型 (別課題で扱う)
        prov = _provenance(r, manifest)
        gone = [f for f in PROVENANCE_FIELDS if f not in prov]
        if gone:
            res.violations += 1
            missing[r.task] += 1
        if "rubric" in prov:
            versions[r.task].add(str(prov["rubric"]))
    multi = {t: sorted(v) for t, v in versions.items() if len(v) > 1}
    res.violations += len(multi)
    res.detail = {
        "rows_missing_fields_by_task": dict(missing),
        "tasks_with_multiple_versions": multi,
    }
    res.add_example(
        "行に meta {teacher, effort, rubric, input_sha} か manifest.provenance[task] が必要"
    )
    return res


def check_input_sha(rows: list[Row]) -> CheckResult:
    res = CheckResult("4_input_sha")
    for r in rows:
        claimed, teacher = r.meta.get("input_sha"), r.meta.get("teacher_input_sha")
        if not claimed or not teacher:
            res.uninspectable[r.task] = res.uninspectable.get(r.task, 0) + 1
            continue
        actual = hashlib.sha256(r.body.encode()).hexdigest()
        if not (claimed == teacher == actual or (claimed == teacher and r.task != "summary")):
            res.violations += 1
            res.add_example(
                f"{r.label} {r.task}: 学習入力 {str(claimed)[:10]} / 教師 {str(teacher)[:10]}"
            )
    return res


# ---------------------------------------------------------------------------
# 5. トークン数
# ---------------------------------------------------------------------------


class TokenCounter:
    """学習 (cloud_train/train_lora.py の encode) と同じ形で数える。使えなければ文字数で近似。"""

    def __init__(self, tokenizer_dir: Path | None) -> None:
        self.approximate = True
        self._tok: Any = None
        self._tpl: Any = None
        if tokenizer_dir is None:
            tokenizer_dir = _find_tokenizer_dir()
        try:
            if tokenizer_dir is None:
                raise FileNotFoundError("tokenizer dir")
            import jinja2
            from tokenizers import Tokenizer  # type: ignore[import-not-found]

            env = jinja2.Environment()
            env.globals["raise_exception"] = _raise
            self._tpl = env.from_string(
                (tokenizer_dir / "chat_template.jinja").read_text(encoding="utf-8")
            )
            self._tok = Tokenizer.from_file(str(tokenizer_dir / "tokenizer.json"))
            self.approximate = False
            self.source = str(tokenizer_dir)
        except Exception as exc:  # noqa: BLE001 - 依存が無い環境では近似に落とす (明示する)
            self.source = f"approximate by characters ({type(exc).__name__}: {str(exc)[:60]})"

    def count(self, messages: tuple[dict[str, str], ...]) -> int | None:
        """総トークン数。形が合わない例は None。"""
        if self._tok is None:
            return sum(len(m["content"]) for m in messages)
        msgs = [dict(m) for m in messages]
        content = msgs[-1]["content"]
        prompt = self._tpl.render(messages=msgs[:-1], add_generation_prompt=True, bos_token="<bos>")
        prompt = prompt.removesuffix(_EMPTY_THOUGHT)
        rendered = self._tpl.render(messages=msgs, bos_token="<bos>")
        if content not in rendered:
            return None
        ending = rendered[rendered.rindex(content) + len(content) :]
        full = self._tok.encode(prompt + content + ending, add_special_tokens=False).ids
        return len(full)


def _raise(message: str) -> None:
    raise ValueError(message)


def _find_tokenizer_dir() -> Path | None:
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub"
    found = sorted(hub.glob(HF_TOKENIZER_GLOB))
    return found[-1] if found and (found[-1] / "tokenizer.json").exists() else None


def count_tokens(rows: list[Row], counter: TokenCounter) -> dict[str, int | None]:
    return {r.label: counter.count(r.messages) for r in rows}


def check_token_limits(
    rows: list[Row], tokens: dict[str, int | None], counter: TokenCounter, limits: dict[str, int]
) -> CheckResult:
    res = CheckResult("5_token_limits")
    res.detail["tokenizer"] = counter.source
    res.detail["approximate"] = counter.approximate
    over: Counter[str] = Counter()
    for r in rows:
        n = tokens[r.label]
        limit = limits.get(r.task, limits.get("*", DEFAULT_MAX_SEQ))
        if n is None or n > limit:
            res.violations += 1
            over[r.task] += 1
            res.add_example(
                f"{r.label} {r.task}: {n} > {limit}" if n else f"{r.label} {r.task}: 形が合わない"
            )
    res.detail["over_limit_by_task"] = dict(over)
    return res


# ---------------------------------------------------------------------------
# 6. 本文に無い識別子 / 7. 反復・暴走
# ---------------------------------------------------------------------------


def _norm_url(u: str) -> str:
    return u.rstrip(".,;:)").rstrip("/").lower()


def _grounding_misses(out_strings: list[str], body: str) -> dict[str, list[str]]:
    low = body.lower()
    body_urls = {_norm_url(u) for u in URL_RE.findall(body)}
    misses: dict[str, list[str]] = {"cve": [], "hash": [], "url": []}
    for s in out_strings:
        misses["cve"] += [c for c in CVE_RE.findall(s) if c.lower() not in low]
        misses["hash"] += [h for h in HASH_RE.findall(s) if h.lower() not in low]
        misses["url"] += [
            u for u in URL_RE.findall(s) if _norm_url(u) not in body_urls and u.lower() not in low
        ]
    return {k: v for k, v in misses.items() if v}


def _norm_name(s: str) -> str:
    import unicodedata

    return re.sub(r"[\s\-_.()]+", "", unicodedata.normalize("NFKC", s).lower())


def check_grounding(rows: list[Row]) -> CheckResult:
    res = CheckResult("6_grounding")
    kinds: Counter[str] = Counter()
    rows_by_task: Counter[str] = Counter()
    names: list[dict[str, str]] = []
    for r in rows:
        if r.task == "none":
            continue
        j = _try_json(r.out)
        out_strings = strings(j) if j is not None else [r.out]
        misses = _grounding_misses(out_strings, r.body)
        if misses:
            res.violations += 1
            kinds.update({k: len(v) for k, v in misses.items()})
            rows_by_task[r.task] += 1
            res.add_example(f"{r.label} {r.task}: { ({k: v[:2] for k, v in misses.items()}) }")
        if r.task == "summary" and isinstance(j, dict):
            names += _name_mismatches(r, j)
    res.detail = {
        "items_not_in_body": dict(kinds),
        "violating_rows_by_task": dict(rows_by_task),
        "name_mismatches_info_only": len(names),
        "name_mismatch_list": names[:100],
    }
    return res


def _name_mismatches(r: Row, j: dict[str, Any]) -> list[dict[str, str]]:
    body = _norm_name(r.body)
    return [
        {"row": r.label, "field": f, "value": str(v)}
        for f in ("victim_orgs", "tools", "malware_families")
        for v in j.get(f) or []
        if _norm_name(str(v)) and _norm_name(str(v)) not in body
    ]


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _summary_runaway(j: dict[str, Any], body: str) -> list[str]:
    reasons = []
    if len(str(j.get("summary") or "")) > SUMMARY_MAX_CHARS:
        reasons.append("summary_over_2000")
    if len(str(j.get("remediation") or "")) > REMEDIATION_MAX_CHARS:
        reasons.append("remediation_over_1000")
    if len(j.get("mitre_techniques") or []) >= TTP_MAX:
        reasons.append("ttp_ge_20")
    collapsed = _collapse(body)
    for ev in j.get("mitre_evidence") or []:
        quote = _collapse(str(ev.get("quote") if isinstance(ev, dict) else ""))
        if not quote or quote not in collapsed:
            reasons.append("mitre_quote_not_verbatim")
    return reasons


def check_runaway(rows: list[Row]) -> CheckResult:
    res = CheckResult("7_repetition_runaway")
    reasons: Counter[str] = Counter()
    for r in rows:
        if r.task == "none":
            continue
        j = _try_json(r.out)
        found = (
            ["repeated_text"]
            if any(_is_runaway_text(s) for s in (strings(j) if j is not None else [r.out]))
            else []
        )
        if r.task == "summary" and isinstance(j, dict):
            found += _summary_runaway(j, r.body)
        if found:
            res.violations += 1
            reasons.update(found)
            res.add_example(f"{r.label} {r.task}: {found}")
    res.detail["reasons"] = dict(reasons)
    return res


# ---------------------------------------------------------------------------
# 8. 長さ / 9. 重複 / 10. guard / 11. 分布
# ---------------------------------------------------------------------------


def _percentile(values: list[int], p: float) -> float:
    s = sorted(values)
    return float(s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))])


def length_profile(rows: list[Row], tokens: dict[str, int | None]) -> dict[str, dict[str, float]]:
    by: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        n = tokens.get(r.label)
        if n:
            by[r.task].append(n)
    return {
        t: {f"p{int(p * 100)}": _percentile(v, p) for p in (0.5, 0.9, 0.99)} for t, v in by.items()
    }


def check_length_vs_baseline(
    profile: dict[str, dict[str, float]], baseline: dict[str, dict[str, float]] | None
) -> CheckResult:
    res = CheckResult("8_length_vs_baseline")
    res.detail["profile"] = profile
    if baseline is None:
        res.violations += 1
        res.add_example("基準版が読めない (--baseline)")
        return res
    res.detail["baseline"] = baseline
    for task, pcts in profile.items():
        for name, value in pcts.items():
            base = baseline.get(task, {}).get(name)
            if base and value > base * LENGTH_RATIO_MAX:
                res.violations += 1
                res.add_example(
                    f"{task} {name}: {value:.0f} > 基準 {base:.0f} x {LENGTH_RATIO_MAX}"
                )
    return res


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def check_duplicates(rows: list[Row], manifest: dict[str, Any]) -> CheckResult:
    res = CheckResult("9_duplicates")
    allowed = manifest.get("intended_exposures") or {}
    exact: dict[str, list[Row]] = defaultdict(list)
    by_input: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        exact[_sha(r.prompt, r.out)].append(r)
        by_input[_sha(r.prompt)].append(r)
    dup: Counter[str] = Counter()
    groups: Counter[str] = Counter()
    rows_in: Counter[str] = Counter()
    for g in exact.values():
        extra = len(g) - int(allowed.get(g[0].task, 1))
        if extra > 0:
            res.violations += extra
            dup[g[0].task] += extra
            groups[g[0].task] += 1
            rows_in[g[0].task] += len(g)
            res.add_example(f"完全一致 {len(g)} 行: {g[0].task} {[x.label for x in g][:4]}")
    conflicts = 0
    overlap = 0
    for g in by_input.values():
        if len({x.out for x in g}) > 1:
            conflicts += 1
            res.add_example(f"同じ入力でラベルが違う: {[x.label for x in g][:3]}")
        if {x.split for x in g} == {"train", "valid"}:
            overlap += 1
    res.violations += conflicts + overlap
    res.detail = {
        "exact_duplicate_extra_rows_by_task": dict(dup),
        "exact_duplicate_groups_by_task": dict(groups),
        "exact_duplicate_rows_involved_by_task": dict(rows_in),
        "same_input_different_label_groups": conflicts,
        "train_valid_overlap_inputs": overlap,
    }
    return res


def _load_guard() -> tuple[set[str], set[str]]:
    os.environ.setdefault("KUEBIKO_ROOT", str(REPO))
    path = REPO / "data/mlx/build_dataset_s24.py"
    spec = importlib.util.spec_from_file_location("build_dataset_s24_for_gate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"guard の定義が読めない: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ids, titles = mod.load_guard(Counter())
    return ids, titles


def check_guard(
    rows: list[Row], eval_oldest: str, guard: tuple[set[str], set[str]] | None = None
) -> CheckResult:
    res = CheckResult("10_guard_and_dates")
    try:
        ids, titles = guard if guard is not None else _load_guard()
    except Exception as exc:  # noqa: BLE001 - guard が読めないことは合格にしない
        res.violations += 1
        res.add_example(f"guard を読めない: {type(exc).__name__}: {exc}")
        return res
    needles = sorted(
        {n for n in ids | titles if len(n) >= MIN_TITLE_FOR_GUARD}, key=len, reverse=True
    )
    hits: Counter[str] = Counter()
    for r in rows:
        if r.task == "none":
            continue
        hit = next((n for n in needles if n in r.prompt), None)
        if hit:
            res.violations += 1
            hits[r.task] += 1
            res.add_example(f"{r.label} {r.task} guard 一致: {hit[:50]!r}")
    newer = _rows_newer_than_eval(rows, eval_oldest, res)
    res.detail = {
        "guard_hits_by_task": dict(hits),
        "guard_needles": len(needles),
        "rows_newer_than_eval_oldest": newer,
        "eval_oldest": eval_oldest,
        "date_uninspectable_tasks": sorted(
            {r.task for r in rows if r.task not in ("detect", "none")}
        ),
    }
    return res


def _rows_newer_than_eval(rows: list[Row], eval_oldest: str, res: CheckResult) -> int:
    newer = 0
    for r in rows:
        if r.task != "detect":
            continue
        m = DATE_RE.search(r.prompt)
        if m and m.group(1) >= eval_oldest:
            newer += 1
            res.violations += 1
            res.add_example(f"{r.label} detect 期間 {m.group(1)} >= 評価の最古日 {eval_oldest}")
    return newer


def distributions(rows: list[Row]) -> CheckResult:
    res = CheckResult("11_distributions", informational=True)
    dist: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        j = _try_json(r.out)
        if not isinstance(j, dict):
            continue
        dist[f"{r.task}:rows"][r.split] += 1
        for key in (
            "importance",
            "matched",
            "same_event",
            "kind",
            "category",
            "scope",
            "impact",
            "actor",
            "target",
        ):
            if key in j:
                dist[f"{r.task}:{key}"][str(j[key])] += 1
        for item in j.get("open") or []:
            dist["detect:domain"][str(item.get("domain"))] += 1
        if r.task == "triage" and "境界" in str(j.get("reason")):
            dist["triage:boundary_reason"]["境界を含む理由"] += 1
    res.detail = {k: dict(v.most_common(15)) for k, v in sorted(dist.items())}
    return res


# ---------------------------------------------------------------------------
# 実行・出力
# ---------------------------------------------------------------------------


def run_gate(
    dataset_dir: Path,
    manifest_path: Path | None = None,
    baseline_dir: Path | None = None,
    limits: dict[str, int] | None = None,
    eval_oldest: str = DEFAULT_EVAL_OLDEST,
    tokenizer_dir: Path | None = None,
    guard: tuple[set[str], set[str]] | None = None,
) -> dict[str, Any]:
    rows = load_rows(dataset_dir)
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path and manifest_path.exists()
        else {}
    )
    counter = TokenCounter(tokenizer_dir)
    tokens = count_tokens(rows, counter)
    baseline: dict[str, dict[str, float]] | None = None
    if baseline_dir and (baseline_dir / "train.jsonl").exists():
        brows = load_rows(baseline_dir)
        baseline = length_profile(brows, count_tokens(brows, counter))
    results = [
        check_task_marks(rows, manifest),
        check_schema(rows),
        check_provenance(rows, manifest),
        check_input_sha(rows),
        check_token_limits(rows, tokens, counter, limits or {"*": DEFAULT_MAX_SEQ}),
        check_grounding(rows),
        check_runaway(rows),
        check_length_vs_baseline(length_profile(rows, tokens), baseline),
        check_duplicates(rows, manifest),
        check_guard(rows, eval_oldest, guard),
        distributions(rows),
    ]
    return {
        "dataset": str(dataset_dir),
        "rows": len(rows),
        "passed": all(r.passed for r in results),
        "checks": [
            {
                "name": r.name,
                "passed": r.passed,
                "violations": r.violations,
                "uninspectable_by_task": r.uninspectable,
                "detail": r.detail,
                "examples": r.examples,
            }
            for r in results
        ],
    }


def format_table(report: dict[str, Any]) -> str:
    lines = [
        f"dataset: {report['dataset']} ({report['rows']} 行) => "
        f"{'PASS' if report['passed'] else 'FAIL'}"
    ]
    for c in report["checks"]:
        mark = "ok  " if c["passed"] else "FAIL"
        unins = (
            f"  検査不能 {sum(c['uninspectable_by_task'].values())} 行"
            if c["uninspectable_by_task"]
            else ""
        )
        lines.append(f"  [{mark}] {c['name']:<28} 違反 {c['violations']:>5}{unins}")
        lines += [f"           - {e}" for e in c["examples"][:3]]
    return "\n".join(lines)


def _parse_limits(items: list[str]) -> dict[str, int]:
    limits = {"*": DEFAULT_MAX_SEQ}
    for item in items:
        task, _, n = item.partition("=")
        limits[task] = int(n)
    return limits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dataset_dir", type=Path)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--baseline", type=Path, default=REPO / "data/mlx/dataset_s22")
    ap.add_argument(
        "--limit", action="append", default=[], help="task=N (既定 *=12500、n 系は ach=18500 等)"
    )
    ap.add_argument("--eval-oldest", default=DEFAULT_EVAL_OLDEST)
    ap.add_argument("--tokenizer-dir", type=Path)
    ap.add_argument(
        "--out", type=Path, help="JSON の出力先 (既定: <dataset_dir>/quality_gate.json)"
    )
    args = ap.parse_args(argv)
    report = run_gate(
        args.dataset_dir,
        args.manifest,
        args.baseline,
        _parse_limits(args.limit),
        args.eval_oldest,
        args.tokenizer_dir,
    )
    out = args.out or args.dataset_dir / "quality_gate.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(format_table(report))
    print(f"json -> {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

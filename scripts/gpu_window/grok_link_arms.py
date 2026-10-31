#!/usr/bin/env python3
"""GROK-LINK-JUDGE の 2 腕 (リンクなし / あり) × 2 回の要約を作る道具 (GPU-TOOLS-GROKLINK)。

仕様は docs/replay_prereg/grok_link_fetch.md (生成の経路の封: コミット fbb9a0e8)。
**LLM を呼ぶのは枠 GPU-A2 だけ**。

3 つの段 (どれも本文を書き出さない):
  --attest   (ホストで。git だけ・DB なし) prereg がコミット済みで HEAD の祖先か、生成の経路が
             封から不変かを検査し、証明書 (経路の sha256 つき) を書く
  --draw     (使い捨てのコンテナ。DB は SELECT のみ) 窓が閉じた後に 40 件を seed 20261107 で
             抜き sample.json (既にあれば拒否)
  (既定)     (使い捨てのコンテナ) 160 要約を作る。--dry-run は計画の表示だけ (LLM・DB に触れない)

    python scripts/gpu_window/grok_link_arms.py --attest
    docker compose run --rm --no-deps -T kuebiko python scripts/gpu_window/grok_link_arms.py --draw
    docker compose run --rm --no-deps -T kuebiko python scripts/gpu_window/grok_link_arms.py
コンテナには .git も docs/ も無いため、prereg の検査はホストの証明書 (--attest) を介する。
証明書には道具 2 本の sha256 を入れ、コンテナ側の写し (古いイメージ) と食い違えば拒否する。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import grok_link_sample as gls  # noqa: E402

PREREG_NAME = "grok_link_fetch"
PREREG_COMMIT = "39de1c8f"  # prereg の最終コミット (§5.1 の訂正。生成の経路は fbb9a0e8 から不変)
GENERATION_BASE = "fbb9a0e8"  # 封をしたコミット。生成の経路はここから変えない (§6.4)
EXPECTED_MODEL = "kuebiko-sft:s24ci65"
EXPECTED_DIGEST = "56d89a8cee6e6a15aa8b83fb1f264691c251f1ce61edb389a7b12c6d59c5f315"
OUT_DIR = Path("data/eval/grok_link_judge_1107")
GOLD44 = Path("data/eval/grok_link_fetch_2026_10_24/sample.json")
RUNS = (1, 2)
ARMS = ("without", "with")
MAX_ATTEMPTS = 2
MAX_MISSING_SUMMARIES = 8  # 160 のうち (5%)
MAX_BOTH_RUNS_MISSING_POSTS = 4  # 40 のうち (10%)
TOOL_FILES = ("grok_link_arms.py", "grok_link_sample.py")
GOLD44_SHA256 = "4424d45647e528630e7178bfe0fb2966768ab62db57e9e0fcc25165bc7c4fd9e"  # prereg §2.2
REPO_ROOT = Path(__file__).resolve().parents[2]
#: §5.1・§6.4 の生成の経路。封の後に 1 byte でも変われば無効 (git diff と sha256 の両方で見る)
GENERATION_PATH = (
    "src/grok/link_fetch.py",
    "src/grok/link_relevance.py",
    "src/pipeline/briefing.py",
    "src/pipeline/summary_grounding.py",
    "src/pipeline/summary_runaway.py",
    "src/pipeline/dispatch.py",
    "src/pipeline/body_limits.py",
    "src/prompts/rubric_store.py",
    "prompts/briefing/summarizer.j2",
    "prompts/_persona.j2",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_shas() -> dict[str, str]:
    here = Path(__file__).resolve().parent
    return {n: hashlib.sha256((here / n).read_bytes()).hexdigest() for n in TOOL_FILES}


def generation_shas(root: Path = REPO_ROOT) -> dict[str, str]:
    """生成の経路の各ファイルの sha256 (無いファイルは空文字 = 食い違いとして検出される)。"""
    out: dict[str, str] = {}
    for rel in GENERATION_PATH:
        f = root / rel
        out[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else ""
    return out


def verify_gold44(path: Path = GOLD44) -> list[Any]:
    """gold44 の sample.json が prereg §2.2 の sha256 と一致することを確かめて読む。"""
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != GOLD44_SHA256:
        raise SystemExit(f"{path} の sha256 が prereg §2.2 の値と違う (除外の集合が変わった)")
    data: list[Any] = json.loads(raw.decode("utf-8"))
    return data


# ---- 純粋部分: 腕・順序・関門の当て直し・無効の判定 ---------------------------------------


def arm_order(status_id: str, run: int) -> tuple[str, str]:
    """投稿の中の腕の順 = sha256(f"{status_id}:{回}:gen") の偶奇で入れ替える (§5.1)。"""
    parity = int(hashlib.sha256(f"{status_id}:{run}:gen".encode()).hexdigest(), 16) % 2
    return ("without", "with") if parity == 0 else ("with", "without")


def plan_units(status_ids: Sequence[str]) -> list[tuple[str, int, str]]:
    """生成の順序 (投稿, 回, 腕)。回 1 の全投稿 → 回 2 の全投稿。"""
    return [(s, run, arm) for run in RUNS for s in status_ids for arm in arm_order(s, run)]


def pages_from_linked_context(raw: str | list[Any]) -> tuple[Any, ...]:
    """保存された linked_context を LinkedPage に戻す (取り直さない。未知の欄は捨てる)。"""
    from src.grok.link_fetch import LinkedPage

    allowed = {f.name for f in fields(LinkedPage)}
    data = json.loads(raw) if isinstance(raw, str) else raw
    return tuple(LinkedPage(**{k: v for k, v in d.items() if k in allowed}) for d in data)


def reconstruct_gate_post(body: str) -> str:
    """保存した body から、本番が関門に渡した ``f"{text}\\n{quoted}"`` を復元する。

    body = text + ["\\n[引用] " + quoted] + ["\\n参考 URL: ..."] (grok_convert の組み立て)。
    """
    lines = body.split("\n")
    if lines and lines[-1].startswith("参考 URL: "):
        lines = lines[:-1]
    rest = "\n".join(lines)
    # 投稿の本文が空だと body は "[引用] ..." で始まる (先頭に改行が無い)
    if rest.startswith("[引用] "):
        return f"\n{rest[len('[引用] ') :]}"
    text, sep, quoted = rest.partition("\n[引用] ")
    return f"{text}\n{quoted if sep else ''}"


def gate_mismatches(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """保存の skip_reason と、今のコードの関門の当て直しが食い違う status_id (§5.1・§6.4)。"""
    from src.grok.link_relevance import apply_relevance_gate

    bad: list[str] = []
    for r in rows:
        stored = pages_from_linked_context(r["linked_context"])
        cleared = tuple(replace(p, skip_reason=None) for p in stored)
        again = apply_relevance_gate(reconstruct_gate_post(str(r["body"])), cleared)
        if [p.skip_reason for p in again] != [p.skip_reason for p in stored]:
            bad.append(str(r["status_id"]))
    return bad


def prompt_bodies(body: str, linked_context: str | list[Any]) -> dict[str, str]:
    """腕ごとの prompt_body。本番の ``llm_body + link_text`` と同じ切り詰めを通す。"""
    from src.grok.link_fetch import build_link_section
    from src.pipeline.body_limits import MAX_STORED_BODY_CHARS
    from src.pipeline.briefing import MAX_LLM_BODY_CHARS

    llm_body = body[:MAX_STORED_BODY_CHARS][:MAX_LLM_BODY_CHARS]
    link_text = build_link_section(pages_from_linked_context(linked_context))
    return {"without": llm_body, "with": llm_body + link_text}


def generation_invalidations(
    results: Sequence[Mapping[str, Any]], status_ids: Sequence[str]
) -> list[str]:
    """§6.4 の生成側の無効 (欠けが 8 を超える・2 回とも欠けた投稿が 4 を超える)。"""
    missing = [r for r in results if not r.get("ok")]
    reasons: list[str] = []
    if len(missing) > MAX_MISSING_SUMMARIES:
        reasons.append(f"missing_summaries={len(missing)}>{MAX_MISSING_SUMMARIES}")
    both = 0
    for s in status_ids:
        runs_missing = {r["run"] for r in missing if r["status_id"] == s}
        if set(RUNS) <= runs_missing:
            both += 1
    if both > MAX_BOTH_RUNS_MISSING_POSTS:
        reasons.append(f"posts_missing_in_both_runs={both}>{MAX_BOTH_RUNS_MISSING_POSTS}")
    return reasons


def model_invalidations(
    resolved: str, before: Mapping[str, str], after: Mapping[str, str]
) -> list[str]:
    """§5.1・§6.4: 解決先の名前と /api/tags の digest (最初・最後)。"""
    reasons: list[str] = []
    if resolved != EXPECTED_MODEL:
        reasons.append(f"resolved_model={resolved}")
    for label, tags in (("start", before), ("end", after)):
        if tags.get(EXPECTED_MODEL) != EXPECTED_DIGEST:
            reasons.append(f"digest_{label}={tags.get(EXPECTED_MODEL)}")
    return reasons


def stability_invalidations(start: Mapping[str, str], end: Mapping[str, str]) -> list[str]:
    """160 要約が 1 つの生成の経路・1 つのテンプレートで作られたか (§6.4)。"""
    return [
        f"{k}_changed"
        for k in ("generation_files", "template_sha256", "tools")
        if start.get(k) != end.get(k)
    ]


# ---- 証明書 (ホスト側。git だけ) ----------------------------------------------------------


def generation_path_changes(repo_root: Path) -> list[str]:
    """``git diff --quiet GENERATION_BASE HEAD -- <生成の経路>`` が差分を示すファイル。"""
    import subprocess

    changed: list[str] = []
    for rel in GENERATION_PATH:
        proc = subprocess.run(  # noqa: S603
            ["git", "diff", "--quiet", GENERATION_BASE, "HEAD", "--", rel],  # noqa: S607
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if proc.returncode != 0:
            changed.append(rel)
    return changed


def attest(repo_root: Path, out: Path) -> dict[str, Any]:
    from scripts.replay._gate import require_prereg

    rec = require_prereg(PREREG_NAME, repo_root, caller=Path(__file__))
    if not rec.git_sha.startswith(PREREG_COMMIT):
        raise SystemExit(f"prereg の最終コミット {rec.git_sha[:8]} が {PREREG_COMMIT} と違う")
    changed = generation_path_changes(repo_root)
    if changed:
        raise SystemExit(f"生成の経路が {GENERATION_BASE} から変わった (§6.4 の無効): {changed}")
    payload = {
        "prereg_path": rec.path,
        "prereg_git_sha": rec.git_sha,
        "prereg_blob_sha256": rec.blob_sha256,
        "head_sha": rec.head_sha,
        "prereg_commit_time": rec.commit_time,
        "attested_at": rec.run_started_at,
        "generation_base": GENERATION_BASE,
        "generation_files": generation_shas(repo_root),
        "tools": tool_shas(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def check_attestation(path: Path) -> dict[str, Any]:
    """コンテナ側: 証明書があり、道具と生成の経路の写しが証明書と同じであること。"""
    if not path.is_file():
        raise SystemExit(f"prereg の証明書が無い: {path} (ホストで --attest を先に)")
    cert: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if cert.get("tools") != tool_shas():
        raise SystemExit("道具の写しが証明書と違う (イメージが古い / 道具が変わった)。再 attest")
    if cert.get("generation_files") != generation_shas():
        raise SystemExit("生成の経路の写しが証明書と違う (イメージが古い / 経路が変わった)。§6.4")
    return cert


# ---- 外部との接点 (Ollama のタグ・テンプレート・DB) ---------------------------------------


def fetch_tags(base_url: str, timeout: float = 15.0) -> dict[str, str]:
    req = urllib.request.Request(base_url.rstrip("/") + "/api/tags")  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.loads(resp.read().decode("utf-8"))
    return {str(m["name"]): str(m.get("digest", "")) for m in data.get("models", [])}


def probe_article(
    status_id: str = "0", published: datetime | None = None, feed_title: str = "probe"
) -> Any:
    """本番の Grok と同じ形の記事 (題は NO_TITLE_SENTINEL に差し替わる、has_title=False)。"""
    from src.tools.article_model import Article

    return Article(
        id=f"grok-x-{status_id}",
        title="",
        url=f"https://x.com/i/status/{status_id}",
        summary_html="",
        published=published or datetime.now(UTC),
        feed_title=feed_title,
        feed_url="https://x.com/",
        has_title=False,
    )


def template_fingerprint(template: Any) -> str:
    """描画前のテンプレートの指紋 = 空の本文で描画した prompt の sha256。"""
    from src.pipeline.briefing import _prompt_article

    return sha256_text(template.render(article=_prompt_article(probe_article()), body=""))


def open_ro() -> Any:
    from scripts.readonly_conn import open_read_only
    from src.storage.db_backend import connect, is_pg_enabled
    from src.storage.run_history import DEFAULT_DB_PATH

    if not is_pg_enabled():
        raise SystemExit("DATABASE_URL が無く SQLite に落ちる。使い捨てのコンテナで実行する")
    return open_read_only(connect, DEFAULT_DB_PATH)


def draw_output_path(out_dir: Path, allow_early: bool) -> Path:
    """試験用 (窓が閉じる前) の抽出は本番の標本の置き場を使わない。"""
    base = out_dir.with_name(out_dir.name + "_early_test") if allow_early else out_dir
    return base / "sample.json"


def draw_step(args: argparse.Namespace) -> int:
    from scripts.replay._closure import (
        R18_VERSION,
        assert_components_ok,
        build_components,
        component_diagnostics,
    )

    gls.assert_draw_allowed(datetime.now(UTC), allow_early=args.allow_early_for_test)
    out = draw_output_path(Path(args.out_dir), bool(args.allow_early_for_test))
    if out.exists():
        raise SystemExit(f"{out} が既にある。標本は 1 回だけ抜く (上書きしない)")
    cert = check_attestation(Path(args.attestation))
    gold = verify_gold44()
    excluded = gls.excluded_status_ids(gold)
    gold_articles = [str(r["article_id"]) for r in gold]
    conn = open_ro()
    try:
        cands = gls.candidates_from_rows(conn.execute(gls.population_sql(excluded)).fetchall())
        counts = gls.stage_counts(
            [dict(r) for r in conn.execute(gls.STAGE_SQL).fetchall()], excluded
        )
        if counts["candidates_before_r18"] != len(cands):
            raise SystemExit(
                f"段の件数 {counts['candidates_before_r18']} と SQL {len(cands)} が違う"
            )
        inputs = gls.load_r18_inputs(conn, [c.article_id for c in cands] + gold_articles)
    finally:
        conn.close()
    comp = build_components([c.article_id for c in cands] + gold_articles, **inputs)
    assert_components_ok(comp, gls.MAX_COMPONENT_SHARE)
    kept, removed = gls.r18_filter(cands, gold_articles, comp)
    counts["removed_by_r18"] = len(removed)
    counts["candidates_after_r18"] = len(kept)
    picked = gls.draw(kept)
    n_components, largest, share = component_diagnostics(comp)
    payload = {
        "seed": gls.SEED_DRAW,
        "gold44_sha256": GOLD44_SHA256,
        "r18": {
            "version": R18_VERSION,
            "max_component_share": gls.MAX_COMPONENT_SHARE,
            "n_components": n_components,
            "largest_component": largest,
            "component_share": round(share, 6),
            "n_items": len(comp),
        },
        "allow_early_for_test": bool(args.allow_early_for_test),
        "prereg_git_sha": cert["prereg_git_sha"],
        "counts": counts,
        "posts": [
            {"status_id": c.status_id, "article_id": c.article_id, "recovered": c.recovered}
            for c in picked
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    digest = sha256_text(out.read_text("utf-8"))[:12]
    print(f"[grok-link] {out} {len(picked)} 件 sha256={digest}")
    return 0


# ---- 生成 ---------------------------------------------------------------------------------


def load_sample(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "sample.json"
    sample: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if sample.get("allow_early_for_test"):
        raise SystemExit("sample.json は --allow-early-for-test のもの (本番の標本ではない)")
    if len(sample["posts"]) != gls.SAMPLE_SIZE:
        raise SystemExit(f"sample.json が {gls.SAMPLE_SIZE} 件でない")
    return sample


def fetch_post_rows(conn: Any, posts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """標本の投稿の畳んだ行 (最も早い保存の行)。本文はメモリ上だけで使う。"""
    ids = [str(p["status_id"]) for p in posts]
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT DISTINCT ON (substring(url from '/status/([0-9]+)')) "  # noqa: S608
        "substring(url from '/status/([0-9]+)') AS status_id, article_id, url, body, "
        "linked_context, feed_title "
        "FROM articles WHERE body_source = 'grok' AND linked_context IS NOT NULL "
        f"AND substring(url from '/status/([0-9]+)') IN ({ph}) "
        "ORDER BY substring(url from '/status/([0-9]+)'), created_at, id",
        tuple(ids),
    ).fetchall()
    out = [dict(r) for r in rows]
    expect = {str(p["status_id"]): str(p["article_id"]) for p in posts}
    if {r["status_id"]: r["article_id"] for r in out} != expect:
        raise SystemExit("標本の article_id と DB の畳んだ行が一致しない")
    return sorted(out, key=lambda r: int(r["status_id"]))


async def summarize_once(
    llm: Any, template: Any, row: Mapping[str, Any], arm: str
) -> tuple[Any, list[str]]:
    from src.pipeline.briefing import (
        _ensure_grounded_summary,
        _grounding_reasons,
        _prompt_article,
        _summarize_without_runaway,
    )

    body = prompt_bodies(str(row["body"]), row["linked_context"])[arm]
    sid = str(row["status_id"])
    article = probe_article(sid, gls.posted_at(sid), str(row.get("feed_title") or ""))
    article = article.model_copy(update={"title": str(row["body"])[:120]})
    prompt = template.render(article=_prompt_article(article), body=body)
    summary = await _summarize_without_runaway(llm, prompt, think=False, url=article.url)
    summary = await _ensure_grounded_summary(
        summary, llm, prompt, article=article, body=body, think=False, body_source="grok"
    )
    return summary, _grounding_reasons(summary, article, body)


async def generate(
    llm: Any, template: Any, rows: Sequence[Mapping[str, Any]], model: str, digest: str, sink: Any
) -> list[dict[str, Any]]:
    by_id = {str(r["status_id"]): r for r in rows}
    results: list[dict[str, Any]] = []
    for sid, run, arm in plan_units(list(by_id)):
        rec: dict[str, Any] = {"status_id": sid, "arm": arm, "run": run, "ok": False, "attempts": 0}
        started = time.monotonic()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            rec["attempts"] = attempt
            try:
                s, reasons = await summarize_once(llm, template, by_id[sid], arm)
            except Exception as e:  # noqa: BLE001 - 1 件の失敗で枠を落とさない
                rec["error"] = type(e).__name__
                continue
            rec.update(
                ok=True,
                title_ja=s.title_ja,
                summary=s.summary,
                analyst_note=s.analyst_note,
                remediation=s.remediation,
                grounding_reasons=reasons,
            )
            rec.pop("error", None)
            break
        rec.update(elapsed_s=round(time.monotonic() - started, 2), model=model, digest=digest)
        sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sink.flush()
        results.append(rec)
    return results


def rubric_version(conn: Any) -> int | None:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM app_config_versions WHERE config_key = ?",
        ("summarizer_rubric",),
    ).fetchone()
    return int(row["v"]) if row and row["v"] is not None else None


def tiers_version(conn: Any) -> int | None:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM app_config_versions WHERE config_key = ?", ("model_tiers",)
    ).fetchone()
    return int(row["v"]) if row and row["v"] is not None else None


def run_step(args: argparse.Namespace) -> int:
    from src.config_loader import load_app_config
    from src.grok.link_relevance import link_relevance_gate_enabled
    from src.pipeline.dispatch import _load_template
    from src.tools.model_tiers import Step, build_llm_for, resolve_step_model

    os.environ["GROK_SUMMARY_GROUNDING"] = "shadow"
    # 本番の llm_usage に書かない (LLM クライアントの構築より前)
    os.environ["LLM_USAGE_LOCAL"] = "0"
    cert = check_attestation(Path(args.attestation))
    out_dir = Path(args.out_dir)
    sample = load_sample(out_dir)
    conn = open_ro()
    try:
        rows = fetch_post_rows(conn, sample["posts"])
        bad = gate_mismatches(rows)
        if bad:
            raise SystemExit(f"関門の当て直しが保存と食い違う (無効 §6.4): {len(bad)} 件")
        versions = {"summarizer_rubric": rubric_version(conn), "model_tiers": tiers_version(conn)}
    finally:
        conn.close()
    config = load_app_config()
    template = _load_template()
    resolved = resolve_step_model(Step.ARTICLE_SUMMARY)
    before = fetch_tags(config.ollama_base_url)
    early = model_invalidations(resolved, before, before)
    if early:
        raise SystemExit(f"生成の前にモデルが一致しない (無効 §6.4): {early}")
    llm = build_llm_for(Step.ARTICLE_SUMMARY, config)
    start = {
        "generation_files": json.dumps(generation_shas()),
        "template_sha256": template_fingerprint(template),
        "tools": json.dumps(tool_shas()),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat()
    with (out_dir / "summaries.jsonl").open("w", encoding="utf-8") as sink:
        results = asyncio.run(
            generate(llm, template, rows, resolved, before.get(resolved, ""), sink)
        )
    after = fetch_tags(config.ollama_base_url)
    end = {
        "generation_files": json.dumps(generation_shas()),
        "template_sha256": template_fingerprint(_load_template()),
        "tools": json.dumps(tool_shas()),
    }
    invalid = (
        generation_invalidations(results, [str(r["status_id"]) for r in rows])
        + model_invalidations(resolved, before, after)
        + stability_invalidations(start, end)
    )
    manifest = {
        "prereg_path": cert["prereg_path"],
        "prereg_git_sha": cert["prereg_git_sha"],
        "prereg_blob_sha256": cert["prereg_blob_sha256"],
        "generation_base": cert["generation_base"],
        "generation_files_sha256": generation_shas(),
        "tools_sha256": tool_shas(),
        "sample_sha256": hashlib.sha256((out_dir / "sample.json").read_bytes()).hexdigest(),
        "template_sha256_start": start["template_sha256"],
        "template_sha256_end": end["template_sha256"],
        "versions": versions,
        "resolved_model": resolved,
        "digest_start": before.get(EXPECTED_MODEL, ""),
        "digest_end": after.get(EXPECTED_MODEL, ""),
        "flags": {
            "GROK_SUMMARY_GROUNDING": os.environ["GROK_SUMMARY_GROUNDING"],
            "GROK_LINK_RELEVANCE_GATE": "on" if link_relevance_gate_enabled() else "off",
        },
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "n_summaries": len(results),
        "n_ok": sum(1 for r in results if r["ok"]),
        "invalid_reasons": invalid,
    }
    from scripts.replay._manifest import assert_no_body

    assert_no_body(manifest)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), "utf-8")
    print(f"[grok-link] {manifest['n_ok']}/{manifest['n_summaries']} 要約, invalid={invalid}")
    return 1 if invalid else 0


def dry_run(args: argparse.Namespace) -> int:
    sample_path = Path(args.out_dir) / "sample.json"
    ids = (
        [str(p["status_id"]) for p in json.loads(sample_path.read_text("utf-8"))["posts"]]
        if sample_path.is_file()
        else [f"<id{i}>" for i in range(gls.SAMPLE_SIZE)]
    )
    units = plan_units(ids)
    print("[grok-link] dry-run (LLM・DB に触れない)")
    print(f"  prereg={PREREG_NAME} commit={PREREG_COMMIT} 乱数種(抽出)={gls.SEED_DRAW}")
    print(
        f"  標本 {len(ids)} 投稿 x 腕 {len(ARMS)} x 回 {len(RUNS)} = {len(units)} 要約"
        f" (1 要約 最大 {MAX_ATTEMPTS} 試行)"
    )
    print(
        f"  窓 {gls.WINDOW_START.isoformat()} .. {gls.WINDOW_END.isoformat()}"
        f" / 抽出は {gls.DRAW_NOT_BEFORE.isoformat()} 以降"
    )
    print(
        f"  モデル {EXPECTED_MODEL} digest={EXPECTED_DIGEST[:12]} (最初・最後に /api/tags で確認)"
    )
    print(
        "  GROK_SUMMARY_GROUNDING=shadow・LLM_USAGE_LOCAL=0 固定"
        f" / 出力 {args.out_dir}/summaries.jsonl, manifest.json (本文なし)"
    )
    print(
        f"  標本ファイル: {'あり' if sample_path.is_file() else 'まだ無い (--draw は窓が閉じた後)'}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--attestation", default=str(OUT_DIR / "prereg_attestation.json"))
    ap.add_argument(
        "--attest", action="store_true", help="ホストで prereg の証明書を書く (git のみ)"
    )
    ap.add_argument("--draw", action="store_true", help="40 件を抜いて sample.json を書く")
    ap.add_argument(
        "--allow-early-for-test", action="store_true", help="窓が閉じる前の抽出 (試験用)"
    )
    ap.add_argument("--dry-run", action="store_true", help="計画の表示だけ")
    args = ap.parse_args()
    if args.dry_run:
        return dry_run(args)
    if args.attest:
        attest(Path.cwd(), Path(args.attestation))
        print(f"[grok-link] 証明書 -> {args.attestation}")
        return 0
    if args.draw:
        return draw_step(args)
    return run_step(args)


if __name__ == "__main__":
    raise SystemExit(main())

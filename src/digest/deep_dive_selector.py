"""F1 weekly deep dive 選定 (Phase 5T-T2、案 B rubric scoring)。

Stage 0+1 通過済の候補を LLM に渡して 4 軸 (pir/roi/timeliness/novelty) で
0-5 score を取得、composite で 0-5 件を選定する。

合計 composite 値が ``composite_threshold`` 未満なら 0 件配信を許容
(「今週は深掘りなし」を素直に表現)。

LLM prompt は実出力を見て反復改善する前提 (5T-T design decision)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import jinja2

from src.digest.db_filter import DigestCandidate
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient, LLMResponse

_log = get_logger(__name__)

PROMPTS_DIR = Path("prompts")
RUBRIC_TEMPLATE = "deep_dive_rubric.j2"
# 出力は候補数×~40tok。chunk あたり RUBRIC_CHUNK_SIZE 件なので ~5000tok で num_predict
# 12000 に十分収まる。
RUBRIC_MAX_TOKENS = 12_000
RUBRIC_TEMPERATURE = 0.25
# 2026-07-20 再設計: 上流 (db_filter Stage 2') が決定論 composite で RUBRIC_POOL_MAX (60) 件に
# 有界化するため通常は 1 チャンクで完結する。バッチ分割は「上限超の候補を渡された場合でも
# per-call timeout を起こさない」安全機構として残置 (rubric score は絶対 0-5 anchor なので
# バッチ跨ぎでも composite 比較は妥当)。
RUBRIC_CHUNK_SIZE = 120

# Composite weight (5T-T design: pir 0.4 / roi 0.3 / timeliness 0.2 / novelty 0.1)
DEFAULT_WEIGHTS: dict[str, float] = {
    "pir": 0.40,
    "roi": 0.30,
    "timeliness": 0.20,
    "novelty": 0.10,
}

# composite 閾値: これ未満は選定しない (「今週は深掘りなし」配信を許容)。件数の主ゲート。
DEFAULT_COMPOSITE_THRESHOLD = 2.5

# 選定件数の上限ガード (2026-07-07)。件数は閾値通過数=内容駆動で決まり、これは可読性/
# runaway 防止の安全弁 (通常は効かない)。digest 出力予算は件数に応じ拡張し厚みを保つ。
DEFAULT_MAX_SELECT = 12

# summary 長制限 (token 抑制)
SUMMARY_TRIM_CHARS = 600


@dataclass(frozen=True)
class ScoredArticle:
    """LLM rubric 判定後の article + score。"""

    candidate: DigestCandidate
    pir: float
    roi: float
    timeliness: float
    novelty: float
    composite: float
    rationale: str


def _render_prompt(
    *,
    items: list[DigestCandidate],
    recent_briefs: list[str],
    past_selected_keys: list[str],
    pir_context: list[dict[str, str]] | None = None,
) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(RUBRIC_TEMPLATE)
    rendered_items = [
        {
            "id": c.article_id,
            "title": c.title,
            "feed": c.feed_title,
            "importance": c.importance or "",
            "category": c.category or "",
            "dedup_key": c.dedup_key,
            "summary": (c.summary or "")[:SUMMARY_TRIM_CHARS],
        }
        for c in items
    ]
    return template.render(
        items=rendered_items,
        total=len(rendered_items),
        recent_briefs=recent_briefs,
        past_selected_keys=past_selected_keys,
        pir_context=pir_context or [],
    )


def _parse_llm_output(text: str) -> list[dict[str, object]]:
    """LLM 応答から JSON entries を salvage 方式で抽出 (Phase 5T-T2.1)。

    LLM 出力の脆さに耐える二段階パーサ:
        1. 全体を JSON として読めるか試行
        2. 失敗時は正規表現で個別 entry をスキャンし、各 entry を独立 parse

    salvage 方式の利点:
        - max_tokens 到達で末尾 truncate されても、parse 済 entries は救出
        - LLM の typo (例: "CRationale", "way: 2}") があっても他 entry は採用
        - prompt 改善前でも実用最低限の動作を保証

    失敗時は空 list を返す (raise しない、selector 側で 0 件として処理)。
    """
    if not text:
        return []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # Pass 1: 全体 JSON parse を試行 (正常時の高速 path)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            arr = obj.get("scored_articles", [])
            if isinstance(arr, list) and arr:
                return [e for e in arr if isinstance(e, dict)]
        except json.JSONDecodeError:
            pass

    # Pass 2: salvage モード - entry を 1 つずつ正規表現で抽出
    return _salvage_entries(cleaned)


# entry 1 件分のパターン: { "id": "...", "scores": {...}, "rationale": "..." }
# rationale を含まない不完全 entry も許容、id+scores が取れれば採用。
_ENTRY_OUTER_RE = re.compile(
    r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"scores"\s*:\s*(\{[^}]*\})'
    r'(?:\s*,\s*"\w*[Rr]ationale"\s*:\s*"([^"]*)")?',
)
# 各 score の key:value (typo 耐性で "way"/"CRationale" 等は無視)
_SCORE_KV_RE = re.compile(r'"(pir|roi|timeliness|novelty)"\s*:\s*(-?\d+(?:\.\d+)?)')


def _salvage_entries(text: str) -> list[dict[str, object]]:
    """truncate / typo に耐える正規表現ベース salvage 抽出。"""
    entries: list[dict[str, object]] = []
    for m in _ENTRY_OUTER_RE.finditer(text):
        article_id = m.group(1)
        scores_block = m.group(2)
        rationale = m.group(3) or ""
        scores: dict[str, float] = {}
        for sm in _SCORE_KV_RE.finditer(scores_block):
            key = sm.group(1)
            try:
                scores[key] = float(sm.group(2))
            except ValueError:
                continue
        # 4 軸すべて揃わなくても採用 (欠損は 0 として扱う、selector 側で clip)
        if not scores:
            continue
        entries.append(
            {
                "id": article_id,
                "scores": scores,
                "rationale": rationale,
            },
        )
    return entries


def _compute_composite(
    scores: dict[str, float],
    weights: dict[str, float] | None = None,
) -> float:
    w = weights or DEFAULT_WEIGHTS
    return sum(scores.get(k, 0.0) * weight for k, weight in w.items())


def _clip_score(v: float) -> float:
    return max(0.0, min(5.0, v))


def _to_scored(
    parsed: list[dict[str, object]],
    candidates_by_id: dict[str, DigestCandidate],
    weights: dict[str, float],
) -> list[ScoredArticle]:
    """LLM 出力を ScoredArticle 列に変換し composite 降順で返す。"""
    out: list[ScoredArticle] = []
    for entry in parsed:
        article_id = entry.get("id")
        if not isinstance(article_id, str):
            continue
        candidate = candidates_by_id.get(article_id)
        if candidate is None:
            continue
        raw = entry.get("scores") or {}
        if not isinstance(raw, dict):
            continue
        try:
            scores = {
                "pir": _clip_score(float(raw.get("pir", 0))),
                "roi": _clip_score(float(raw.get("roi", 0))),
                "timeliness": _clip_score(float(raw.get("timeliness", 0))),
                "novelty": _clip_score(float(raw.get("novelty", 0))),
            }
        except (TypeError, ValueError):
            continue
        composite = _compute_composite(scores, weights)
        out.append(
            ScoredArticle(
                candidate=candidate,
                pir=scores["pir"],
                roi=scores["roi"],
                timeliness=scores["timeliness"],
                novelty=scores["novelty"],
                composite=composite,
                rationale=str(entry.get("rationale") or ""),
            ),
        )
    out.sort(key=lambda s: s.composite, reverse=True)
    return out


async def select_deep_dive_articles(
    *,
    llm: LLMClient,
    candidates: list[DigestCandidate],
    recent_briefs: list[str] | None = None,
    past_selected_keys: list[str] | None = None,
    weights: dict[str, float] | None = None,
    composite_threshold: float = DEFAULT_COMPOSITE_THRESHOLD,
    max_select: int = DEFAULT_MAX_SELECT,
) -> list[ScoredArticle]:
    """候補から LLM rubric scoring で深掘り対象を選定 (composite 降順)。

    Args:
        llm: LLM クライアント (Ollama)
        candidates: Stage 0+1 通過済 article list
        recent_briefs: 今週 brief/alert で速報したタイトル (context 注入)
        past_selected_keys: 過去 4 週 F1 選定済 dedup_key (context 注入)
        weights: composite 重み (None ならデフォルト)
        composite_threshold: これ未満は除外 (0 件配信を許容)
        max_select: 最大選定件数

    Returns:
        composite 降順の ScoredArticle (空 list なら「今週は深掘りなし」)。
    """
    if not candidates:
        return []
    actual_weights = weights or DEFAULT_WEIGHTS
    # 段5: pir 軸を実 PIR (pir.yaml) で評価させる。従来は rubric anchor がハードコードで
    # pir.yaml 未参照だった → synthesis と同じ build_synthesis_pir_context を再利用し注入。
    # PIR システム障害時は [] で legacy 挙動 (rubric の anchor のみで判定)。
    try:
        from src.pir.integration import build_synthesis_pir_context, get_pir_config

        pir_context = build_synthesis_pir_context(get_pir_config().priorities)
    except Exception:  # noqa: BLE001 — PIR システム障害で選定を止めない
        pir_context = []

    # 上流有界化 (RUBRIC_POOL_MAX=60) 済みのため通常 1 チャンク。超過時のみ分割 (安全機構)。
    chunks = [
        candidates[i : i + RUBRIC_CHUNK_SIZE] for i in range(0, len(candidates), RUBRIC_CHUNK_SIZE)
    ]
    parsed: list[dict[str, object]] = []
    for idx, chunk in enumerate(chunks):
        prompt = _render_prompt(
            items=chunk,
            recent_briefs=recent_briefs or [],
            past_selected_keys=past_selected_keys or [],
            pir_context=pir_context,
        )
        _log.info(
            "deep_dive_llm_request",
            chunk=idx + 1,
            chunks=len(chunks),
            candidate_count=len(chunk),
            prompt_chars=len(prompt),
        )
        response: LLMResponse = await llm.generate(
            prompt=prompt,
            temperature=RUBRIC_TEMPERATURE,
            max_tokens=RUBRIC_MAX_TOKENS,
            think=False,  # gemma_4_thinking_breaks_digests: digest 系は think=False 必須
        )
        parsed.extend(_parse_llm_output(response.text or ""))
    _log.info(
        "deep_dive_llm_response",
        chunks=len(chunks),
        total_candidates=len(candidates),
        parsed_count=len(parsed),
    )
    candidates_by_id = {c.article_id: c for c in candidates}
    scored = _to_scored(parsed, candidates_by_id, actual_weights)

    above = [s for s in scored if s.composite >= composite_threshold]
    selected = above[:max_select]
    _log.info(
        "deep_dive_selection",
        scored_count=len(scored),
        above_threshold=len(above),
        selected=len(selected),
        composite_threshold=composite_threshold,
        max_select=max_select,
    )
    return selected

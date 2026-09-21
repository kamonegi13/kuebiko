"""F1 weekly deep dive 選定 (Phase 5T-T2、案 B rubric scoring)。

Stage 0+1 通過済の候補を LLM に渡して 4 軸 (pir/roi/timeliness/novelty) で
0-5 score を取得、composite で 0-5 件を選定する。

合計 composite 値が ``composite_threshold`` 未満なら 0 件配信を許容
(「今週は深掘りなし」を素直に表現)。

LLM prompt は実出力を見て反復改善する前提 (5T-T design decision)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jinja2
from pydantic import BaseModel, Field

from src.digest.db_filter import DigestCandidate
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

PROMPTS_DIR = Path("prompts")
RUBRIC_TEMPLATE = "digest/deep_dive_rubric.j2"
# ⚠ **旧コメントの「候補数×~40tok」は実測の 1/5 だった** (2026-09-21 に本番が失敗して判明)。
#   実測は **~200 tok/件** で、60 件を 1 回で採点させると 12,000 で途中切れする。
#   2026-09-21 の本番は 60 件中 **5 件しか採点されず**、下流の本文が枯れて空投稿になった。
#   「毎週ちょうど 12 件」だったのは上限が拘束していたのではなく、途中切れが 12 件前後で
#   起きていたためと見る方が観測に合う。
RUBRIC_MAX_TOKENS = 12_000
RUBRIC_TEMPERATURE = 0.25
# 1 チャンクの件数。**出力予算から逆算する** (200 tok/件 × 25 = 5,000 tok で 12,000 の
# 半分以下)。2026-09-21 までは 120 で、60 件のプールが 1 回に押し込まれて途中切れしていた。
# rubric score は絶対 0-5 anchor なので、チャンクを跨いでも composite の比較は妥当。
RUBRIC_CHUNK_SIZE = 25

# Composite weight (5T-T design: pir 0.4 / roi 0.3 / timeliness 0.2 / novelty 0.1)
DEFAULT_WEIGHTS: dict[str, float] = {
    "pir": 0.40,
    "roi": 0.30,
    "timeliness": 0.20,
    "novelty": 0.10,
}

# composite 閾値: これ未満は選定しない (「今週は深掘りなし」配信を許容)。件数の主ゲート。
DEFAULT_COMPOSITE_THRESHOLD = 2.5

# 選定件数の上限 (2026-09-20 に 12 → 20)。
# ⚠ 「閾値通過数=内容駆動で決まり、これは通常効かない安全弁」という旧コメントは**誤り**
#   だった。実測では直近 10 週すべてが**ちょうど 12 件** = 毎回この上限が拘束していた。
#   20 へ引き上げたのは、本文をセクション単位の横断散文へ変えて 1 件あたりの紙幅から
#   解放されたため (旧構成は 1 記事 300 字の列挙で、件数がそのまま長さになっていた)。
DEFAULT_MAX_SELECT = 20

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
    # 層分けの一般化 (2026-08-20、3 本目): 編集層の SSoT は DB (config_store,
    # key=deep_dive_rubric)。合成に失敗したら legacy .j2 に落ちる (WARNING を
    # 残す = 無音にしない)。rollback: DEEP_DIVE_RUBRIC_COMPOSER=0。
    template = None
    from src.prompts.prompt_store import build_prompt_template
    from src.prompts.registry import get_spec

    spec = get_spec("deep_dive_rubric")
    if spec is not None:
        template = build_prompt_template(spec, PROMPTS_DIR / RUBRIC_TEMPLATE)
    if template is None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
            autoescape=False,
            keep_trailing_newline=True,
        )
        template = env.get_template(RUBRIC_TEMPLATE)
    # ⭐ **番号参照**。長い article_id (rss:https://... 100 字超) を写させると、
    #   モデルが綴りを崩して照合に失敗する。実測で 284 件の採点のうち 66 件が
    #   id 不一致、採点率 51% だった (2026-09-21)。事象ニュースで一度解決した
    #   のと同型 (08-22 の引用関門で確立した番号参照)。
    rendered_items = [
        {
            "no": i + 1,
            "id": c.article_id,
            "title": c.title,
            "feed": c.feed_title,
            "importance": c.importance or "",
            "category": c.category or "",
            "dedup_key": c.dedup_key,
            "summary": (c.summary or "")[:SUMMARY_TRIM_CHARS],
        }
        for i, c in enumerate(items)
    ]
    return template.render(
        items=rendered_items,
        total=len(rendered_items),
        recent_briefs=recent_briefs,
        past_selected_keys=past_selected_keys,
        pir_context=pir_context or [],
    )


class _WireScores(BaseModel):
    model_config = {"extra": "ignore"}
    pir: float = 0.0
    roi: float = 0.0
    timeliness: float = 0.0
    novelty: float = 0.0


class _WireScored(BaseModel):
    model_config = {"extra": "ignore"}
    #: 候補一覧の番号 (1 始まり)。⭐ **長い article_id を写させない** — 実測で
    #: id 不一致が 66 件、採点率 51% まで落ちた (2026-09-21)。
    no: int = 0
    scores: _WireScores = Field(default_factory=_WireScores)
    rationale: str = ""


class _WireRubricOutput(BaseModel):
    """rubric 採点の構造化出力 (2026-09-21)。

    ⚠ **自由文だと暴走する**。3 チャンクのうち 1 つが 25 件で 12,000 トークンに達し
    (他は 1,200-2,900)、同じ entry を吐き続けて採点が候補数の 2 倍になった。
    ⭐ Ollama は maxItems を文法へコンパイルするので、続けたくても閉じる
    (detect・本文と同じ処置。既知の Gemma 4 不具合 ollama#15502)。
    """

    model_config = {"extra": "ignore"}
    scored_articles: list[_WireScored] = Field(
        default_factory=list,
        # チャンク 1 つ分 + 余裕。ここを候補数ちょうどにすると、1 件でも多く返そうと
        # した瞬間に文法違反で全損しうる。
        json_schema_extra={"maxItems": RUBRIC_CHUNK_SIZE * 2},
    )


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
    """LLM 出力を ScoredArticle 列に変換し composite 降順で返す。

    ⚠ **同じ記事を 2 度採点してくることがある** (2026-09-21、60 候補に対し採点 138 件)。
    暴走したチャンクが同じ entry を吐き続けるため。重複を残すと**同じ記事が recap に
    2 回載る** (dry-run の出典で実際に起きた)。最初の 1 件だけを採る。
    """
    out: list[ScoredArticle] = []
    seen: set[str] = set()
    dupes = 0
    for entry in parsed:
        article_id = entry.get("id")
        if not isinstance(article_id, str):
            continue
        candidate = candidates_by_id.get(article_id)
        if candidate is None:
            continue
        if article_id in seen:
            dupes += 1
            continue
        seen.add(article_id)
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
    if dupes:
        _log.warning("deep_dive_duplicate_scores", dropped=dupes, kept=len(out))
    out.sort(key=lambda s: s.composite, reverse=True)
    return out


async def score_deep_dive_candidates(
    *,
    llm: LLMClient,
    candidates: list[DigestCandidate],
    recent_briefs: list[str] | None = None,
    past_selected_keys: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> list[ScoredArticle]:
    """候補**全件**を LLM rubric で採点する (閾値も上限もかけない)。

    ⭐ 採点と選抜を分ける seam (2026-09-20)。本番は選抜まで行うが、**蒸留の教師収穫**では
    落選分のスコアも要る (`f1_selections` は選ばれた分しか残さないため、負例の目標値が
    無かった)。選抜側はここを呼ぶだけにして、収穫スクリプトと同じ経路を通す。

    Returns:
        composite 降順の ScoredArticle (全候補)。
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
        # think=False: digest 系は thinking で本文が空になる (gemma_4_thinking_breaks_digests)
        result = await llm.generate_structured(
            prompt=prompt,
            schema=_WireRubricOutput,
            temperature=RUBRIC_TEMPERATURE,
            max_tokens=RUBRIC_MAX_TOKENS,
            think=False,
        )
        # 番号 → 実 id に戻す (チャンク内の 1 始まり)。範囲外は捨てる。
        parsed.extend(
            {
                "id": chunk[e.no - 1].article_id if 1 <= e.no <= len(chunk) else "",
                "scores": {
                    "pir": e.scores.pir,
                    "roi": e.scores.roi,
                    "timeliness": e.scores.timeliness,
                    "novelty": e.scores.novelty,
                },
                "rationale": e.rationale,
            }
            for e in result.scored_articles
        )
    _log.info(
        "deep_dive_llm_response",
        chunks=len(chunks),
        total_candidates=len(candidates),
        parsed_count=len(parsed),
    )
    candidates_by_id = {c.article_id: c for c in candidates}
    scored = _to_scored(parsed, candidates_by_id, actual_weights)
    # ⭐ **採点されなかった候補を黙らせない**。2026-09-21 に 60 件中 5 件しか採点されず、
    #   それが本番の空投稿の真因だったが、当時のログは parsed_count を出すだけで警告が
    #   無かった (no-silent-caps がここに掛かっていなかった)。
    if len(scored) < len(candidates):
        _log.warning(
            "deep_dive_scoring_incomplete",
            candidates=len(candidates),
            scored=len(scored),
            parsed=len(parsed),
            chunks=len(chunks),
        )
    return scored


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

    採点そのものは ``score_deep_dive_candidates`` に委譲し、ここは閾値と上限だけを持つ。

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
    scored = await score_deep_dive_candidates(
        llm=llm,
        candidates=candidates,
        recent_briefs=recent_briefs,
        past_selected_keys=past_selected_keys,
        weights=weights,
    )
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

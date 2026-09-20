"""digest 用 LLM 生成 wrapper (Phase 5T-J)。

src/digest/db_filter.py で取得した article リストを LLM に渡して、
動的セクション分けされた Markdown digest を生成する。

LLM プロンプト (prompts/digest/weekly_recap.j2 等) は
セクション固定でなく LLM 動的判定 (今週の重点を反映)。
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from src.config_loader import load_app_config
from src.digest.db_filter import DigestCandidate
from src.digest.recap_render import (
    RecapOutput,
    mismatched_citations,
    render_markdown,
    thin_sections,
    uncited_articles,
)
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

PROMPTS_DIR = Path("prompts")
# Phase 5T-J 検証: F1 (30 件入力) で 4563 chars 出力時に 2000 tokens 上限
# でセクション末尾が文中切断された。4000 tokens (~9000 chars 相当) に拡張。
DIGEST_MAX_TOKENS = 4_000
DIGEST_TEMPERATURE = 0.25
# 件数駆動 (2026-07-07): recap 本文は 1 回の LLM 呼出で選定全件を個別深掘りするため、
# 出力予算を件数に応じ拡張し各記事の厚みを保つ (固定 4000 だと件数増で 1 件あたりが痩せる)。
# floor=DIGEST_MAX_TOKENS、per-item ~800tok、ceiling で青天井を防ぐ (生成時間/可読性)。
_DIGEST_TOKENS_PER_ITEM = 800
_DIGEST_MAX_TOKENS_CEILING = 12_000


def _digest_max_tokens(item_count: int) -> int:
    """選定件数に応じた digest 出力予算 (各記事の深掘りの厚みを保つ)。"""
    scaled = min(_DIGEST_MAX_TOKENS_CEILING, item_count * _DIGEST_TOKENS_PER_ITEM)
    return max(DIGEST_MAX_TOKENS, scaled)


def _resolve_link_url(candidate: DigestCandidate, guild_id: str) -> str:
    """Phase 5T-K: digest 項目の link 用 URL を決定。

    優先順:
        1. guild_id + discord_channel_id + discord_message_id が揃えば Discord 直接 URL
        2. そうでなければ元記事 URL (fallback)
    """
    if guild_id and candidate.discord_channel_id and candidate.discord_message_id:
        return (
            f"https://discord.com/channels/{guild_id}/"
            f"{candidate.discord_channel_id}/{candidate.discord_message_id}"
        )
    return candidate.url


def _build_jinja_env() -> jinja2.Environment:
    """digest プロンプト用 Jinja2 環境。"""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=False,  # markdown 出力のためエスケープ無効
        keep_trailing_newline=True,
    )


def _render_prompt(
    template_name: str,
    *,
    candidates: list[DigestCandidate],
    period_label: str,
) -> str:
    """テンプレート + 候補リストから LLM プロンプトを組み立てる。

    Phase 5T-K: 各記事の URL を Discord post URL に変換 (guild_id 設定時)。
    """
    # 層分けの一般化 (2026-08-20、3 本目): 編集層の SSoT は DB (config_store,
    # key=weekly_recap_rubric)。合成に失敗したら legacy .j2 に落ちる (WARNING を
    # 残す = 無音にしない)。rollback: WEEKLY_RECAP_COMPOSER=0。
    template = None
    from src.prompts.prompt_store import build_prompt_template
    from src.prompts.registry import get_spec

    spec = get_spec("weekly_recap")
    # ⚠ template_name は引数 — 将来 _DIGEST_SPECS に別テンプレートが増えたとき、無条件に
    # weekly_recap の合成を返すと**要求と違うプロンプトが黙って使われる**。legacy_path との
    # 一致を関門にする (不一致は legacy 経路へ)。
    if spec is not None and (PROMPTS_DIR / template_name) == spec.legacy_path:
        template = build_prompt_template(spec, PROMPTS_DIR / template_name)
    if template is None:
        env = _build_jinja_env()
        template = env.get_template(template_name)
    items = [
        {
            # ⭐ article_id を渡す。本文がセクション横断散文になったので、どの記事を
            #   扱ったかを LLM に**明示して返させる**しかない (旧構成は 1 記事 1 ブロック
            #   だったので対応が自明だった)。網羅の検証もこの id で行う。
            "article_id": c.article_id,
            "title": c.title,
            "feed": c.feed_title,
            "importance": c.importance or "",
            "category": c.category or "",
            # Phase 5T-P: summary を渡して digest に厚みを持たせる
            # (旧レコードで NULL の場合は空文字、プロンプト側で fallback)
            "summary": (c.summary or "")[:600],  # 過大 token 抑制
        }
        for c in candidates
    ]
    return template.render(items=items, period_label=period_label, total=len(items))


async def generate_digest(
    *,
    llm: LLMClient,
    candidates: list[DigestCandidate],
    template_name: str,
    period_label: str,
    think: bool | None = None,
) -> str:
    """LLM に digest 生成を依頼して Markdown 本文を返す。

    2026-09-20 に**記事の列挙からセクション単位の横断散文へ**再設計した
    (経緯と根拠は src/digest/recap_render.py)。LLM は主題分け・見出し・解説散文だけを
    構造化出力で返し、**体裁と出典行はコードが組む**。

    ⭐ 網羅は指示でなく構造で守る。選定した記事がどの節にも引用されなければ、
    書き直しヒントを添えて 1 度だけ戻す (旧構成は 12 件中 3-5 件しか載らない回が
    直近 5 回中 3 回あり、指示では止まっていなかった)。

    Args:
        llm: LLM クライアント
        candidates: digest 集約対象 article のリスト
        template_name: Jinja2 テンプレート名 (例: "digest/weekly_recap.j2")
        period_label: digest の対象期間ラベル
        think: LLM thinking モード ON/OFF/既定
    """
    if not candidates:
        raise ValueError("candidates が空です (呼び出し側で skip 判定すること)")

    cfg = load_app_config()
    guild_id = cfg.discord_guild_id or ""
    sources = {c.article_id: (c.feed_title, _resolve_link_url(c, guild_id)) for c in candidates}
    selected_ids = [c.article_id for c in candidates]
    prompt = _render_prompt(template_name, candidates=candidates, period_label=period_label)
    max_tokens = _digest_max_tokens(len(candidates))
    _log.info(
        "digest_llm_request",
        template=template_name,
        candidate_count=len(candidates),
        prompt_chars=len(prompt),
        max_tokens=max_tokens,
    )

    out = await _generate_sections(llm, prompt, max_tokens=max_tokens, think=think)
    missing = uncited_articles(out, selected_ids=selected_ids)
    thin = thin_sections(out)
    if missing or thin:
        _log.warning(
            "digest_recap_rewrite",
            uncited=len(missing),
            thin_sections=len(thin),
            sections=len(out.sections),
        )
        # ⚠ **前回の出力を見せてから直させる**。1 度これを忘れて「次の主題は本文が
        #   短すぎます: ○○」とだけ渡し、モデルはその主題を知らないまま作り直して
        #   見出しも本文も空の節を返した (2026-09-20)。
        retry = await _generate_sections(
            llm,
            prompt + _previous_output(out) + _rewrite_hint(missing, thin, sources),
            max_tokens=max_tokens,
            think=think,
        )
        # ⭐ **良くなったときだけ採る**。書き直しは悪化しうる (実測で網羅 3 件漏れ →
        #   7 件漏れに退行した)。指示を足せば良くなるとは限らない。
        out = _better(out, retry, selected_ids=selected_ids)
        missing = uncited_articles(out, selected_ids=selected_ids)

    if missing:
        # 書き直しても漏れたら**黙らせない**。見逃し防止が最優先の機能なので、
        # 「載らなかった」ことが後から分かる形で残す。
        _log.warning(
            "digest_recap_uncited_after_rewrite", uncited=len(missing), selected=len(selected_ids)
        )
    # ⭐ **出典が別記事を指すのは信頼を損なう**。dry-run 実測で 16 件中 3 件が、その節で
    #   一言も触れていない記事を指していた。書き出す前に落とす (08-22 の引用実在関門と
    #   同じ思想)。落とした事実は必ずログに残す。
    titles = {c.article_id: c.title for c in candidates}
    bad = mismatched_citations(out, titles=titles)
    if bad:
        _log.warning(
            "digest_recap_citation_mismatch",
            dropped=sum(len(v) for v in bad.values()),
            sections=len(bad),
        )
    digest_text = render_markdown(out, period_label=period_label, sources=sources, drop=bad)
    _log.info(
        "digest_llm_response",
        template=template_name,
        output_chars=len(digest_text),
        sections=len(out.sections),
        cited=len(selected_ids) - len(missing),
        selected=len(selected_ids),
    )
    return digest_text


async def _generate_sections(
    llm: LLMClient, prompt: str, *, max_tokens: int, think: bool | None
) -> RecapOutput:
    # think=False: digest はテキスト直行で十分 (Gemma 4 の thinking で本文が空になる)
    result = await llm.generate_structured(
        prompt=prompt,
        schema=RecapOutput,
        temperature=DIGEST_TEMPERATURE,
        max_tokens=max_tokens,
        think=think,
    )
    return result


def _previous_output(out: RecapOutput) -> str:
    """前回の出力を要約して渡す (見出しと扱った記事だけ。本文は長いので字数のみ)。"""
    lines = ["", "# あなたの前回の出力 (これを直してください)"]
    for sec in out.sections:
        lines.append(
            f"- {sec.emoji} {sec.heading or '(見出しなし)'} "
            f"— 本文 {len(sec.body.strip())} 字 / 記事 {len(sec.article_ids)} 件"
        )
    return "\n".join(lines)


def _better(first: RecapOutput, retry: RecapOutput, *, selected_ids: list[str]) -> RecapOutput:
    """網羅が改善した方を返す (純粋関数)。同点なら描画できる節が多い方。"""

    def score(o: RecapOutput) -> tuple[int, int]:
        usable = sum(1 for s in o.sections if s.heading.strip() and s.body.strip())
        return (-len(uncited_articles(o, selected_ids=selected_ids)), usable)

    return retry if score(retry) > score(first) else first


def _rewrite_hint(missing: list[str], thin: list[str], sources: dict[str, tuple[str, str]]) -> str:
    """漏れと薄い節を名指しで返す書き直しヒント。

    ⚠ 「全部含めよ」と繰り返しても効かない (それが元の指示だった)。**どれが漏れたか**を
    具体的に挙げる (事象ニュースの書き直しヒントと同じ形)。
    """
    lines = ["", "# 書き直し指示"]
    if missing:
        lines.append(
            f"次の {len(missing)} 件がどの主題にも入っていません。必ずどこかで扱ってください:"
        )
        lines.extend(f"- {m} ({sources[m][0]})" for m in missing if m in sources)
        lines.append(
            "前回の主題構成を保ったまま、漏れた記事を適切な主題へ加えて全体を出し直してください。"
        )
    if thin:
        lines.append(
            "次の主題は本文が短すぎます。記事を横断した解説に書き直してください: "
            + " / ".join(thin)
        )
    return "\n".join(lines)

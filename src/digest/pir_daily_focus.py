"""PIR Daily Focus pipeline (E1 後継、旧 research-digest の置き換え)。

各 enabled PIR の直近 24h match を 1 view に集約。Spotlight (週次 narrative)
の daily 簡易版として、朝 06:30 JST に brief ch へ 1 post。

- Spotlight: 1 PIR の戦術-作戦レベル深掘り (週次、LLM narrative)
- Daily Focus: 全 enabled PIR の 24h 状況一覧 (毎日、LLM 1-2 文 / PIR)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2

from src.config_loader import AppConfig, DiscordChannel, PipelineConfig
from src.logging_config import get_logger
from src.pir.evaluator import PirMatch, evaluate_pir_matches

# PIR 定義は DB が正本 (yaml は初回の種)。2026-09-29 まで yaml を読んでいて、DB で削除した
# PIR と古い説明文で朝ブリーフの PIR 別の要点を書いていた
from src.pir.integration import load_current_pir_config
from src.pir.models import Pir
from src.spotlight.identifier_check import find_unsupported_identifiers
from src.tools.decision_probe import probe_choice
from src.tools.discord_publisher import (
    BriefingMessage,
    DiscordPublisher,
    Source,
)
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import Step

_log = get_logger(__name__)

# 投稿先 (固定: brief ch、朝の通読用)
DAILY_FOCUS_TARGET_CHANNEL: DiscordChannel = "brief"

# 各 PIR section に含める article 件数
_MAX_ARTICLES_PER_PIR = 3

# LLM が "本日の要点" 1-2 文生成 (per-PIR で 1 call)
_PROMPT_TEMPLATE = "digest/pir_daily_focus.j2"
_LLM_MAX_TOKENS = 256
_LLM_TEMPERATURE = 0.3

# 決定的な識別子関門 (C3 測定で確認した「別記事の数値・事例を混ぜる」対策、2026-10-11)。
# 生成文に入力記事 (タイトル・要約) に実在しない CVE / IP / domain / hash / version /
# cvss / actor_id があれば 1 回だけ再生成し、それでも駄目なら要点を出さず空文字を返す
# (既存の LLM 失敗時と同じ扱い — headline のみになる)。``=0`` で関門無効 (旧挙動)。
PIR_FOCUS_FIDELITY_GATE_FLAG_ENV = "PIR_FOCUS_FIDELITY_GATE"
_FIDELITY_GATE_MAX_ATTEMPTS = 2  # 初回生成 + 再生成 1 回


def fidelity_gate_enabled() -> bool:
    """関門の有効判定 (既定 ON)。``PIR_FOCUS_FIDELITY_GATE=0`` で無効化 (検査なし)。"""
    raw = os.environ.get(PIR_FOCUS_FIDELITY_GATE_FLAG_ENV, "1").strip()
    return raw not in ("0", "false", "False")


# 文単位の裏付け判定 (S6、2026-10-11)。識別子関門 (上記) をすり抜けた生成文を「。」区切りで
# 文に分け、文ごとに入力記事 (``matches``) との max_k P(yes) (Jev 型 logprob、
# ``src.tools.decision_probe.probe_choice``) を読む。しきい値未満の文があれば要点全体を
# 1 回だけ作り直し、それでも残れば **その文だけ** を落とす (全文落ちたら既存の失敗時の
# 扱いで空文字)。``=1``/``=true`` で有効化 (既定 off — 試作 (S5) の回収率がまだ実用の
# 安全網として弱いため、本番投入はオーケストレータの判断に委ねる、data/eval/s6_report.md)。
PIR_FOCUS_CLAIM_GATE_FLAG_ENV = "PIR_FOCUS_CLAIM_GATE"
_CLAIM_GATE_MAX_ATTEMPTS = 2  # 初回生成 + 作り直し 1 回 (識別子関門と同じ型)

# しきい値の根拠 (data/eval/s6_report.md 詳細):
# data/eval/s5_claims_gold.jsonl (Opus 5.5・medium 裁定、300 主張) と
# data/eval/s5_probe.jsonl (kuebiko-sft:i58 の記事ごと P(yes)、文のスコア=max_k) を
# date:pir_id の key 単位で較正 40 key (124 主張) / 測定 40 key (131 主張) に分割
# (seed=10115、data/mlx/measure_S5_report.py:_split_keys と同一の分割)。較正側で
# 「正しい文 (supported) の誤検出 ≤ 5%」を満たす最大のしきい値 = 0.22
# (0.22 で誤検出 4/104=3.8%、0.23 まで上げると 6/104=5.8% で基準を超えるため止める)。
# 測定側 (未使用の key) での再現: 裏付けなしの検出率 15/34=44.1%、
# 正しい文の誤検出率 2/97=2.1%。
PIR_FOCUS_CLAIM_THRESHOLD = 0.22


def claim_gate_enabled() -> bool:
    """関門の有効判定 (既定 off)。

    ``PIR_FOCUS_CLAIM_GATE`` に "0"/"false"/空 以外を明示したときだけ有効化する。
    """
    raw = os.environ.get(PIR_FOCUS_CLAIM_GATE_FLAG_ENV, "0").strip()
    return raw not in ("", "0", "false", "False")


def split_claim_sentences(text: str) -> list[str]:
    """「。」区切りで文に分ける (S5 試作と同じ単純な分割、LLM では分けない)。空要素は捨てる。"""
    parts = [p.strip() for p in text.replace("\r", "").replace("\n", "").split("。")]
    return [p for p in parts if p]


def _claim_article_block(idx: int, match: PirMatch) -> str:
    """1 記事分の表示行 (番号付き)。S5 試作 (data/mlx/measure_S5.py:article_line、
    truncate=None — 要約は切らない) と同じ整形。"""
    lines = [f"[記事{idx}] [{match.importance}] {match.title} ({match.feed_title})"]
    if match.summary:
        lines.append(f"  要約: {match.summary}")
    return "\n".join(lines)


def _claim_question(sentence: str, article: str) -> str:
    """S5 試作 (data/mlx/s5_probe.py:_question) と同じ問い (較正と合わせるため文面を変えない)。"""
    return (
        f"{article}\n\n"
        "## 判定対象の文\n"
        f"{sentence}\n\n"
        "質問: 「判定対象の文」に書かれている事実は、上の記事に書かれているか。"
        "一つでも書かれていない事実・断定の格上げ・但し書きの脱落があれば No と答える。"
    )


async def _claim_max_score(
    sentence: str, matches: list[PirMatch], *, config: AppConfig
) -> float | None:
    """文 × 全入力記事の max_k P(yes)。全記事で ``probe_choice`` が None (通信失敗・外部 LLM
    割当時等) ならこの文も None (呼び出し側は fail-open — 判定不能な文は落とさない)。
    """
    scores: list[float] = []
    for idx, match in enumerate(matches, start=1):
        article = _claim_article_block(idx, match)
        probabilities = await probe_choice(
            Step.PIR_FOCUS_CLAIM_JUDGE,
            _claim_question(sentence, article),
            ("Yes", "No"),
            config=config,
        )
        if probabilities is None:
            continue
        p_yes = probabilities.get("Yes")
        if p_yes is not None:
            scores.append(p_yes)
    return max(scores) if scores else None


async def _score_claim_sentences(
    text: str, matches: list[PirMatch], *, config: AppConfig
) -> tuple[list[str], list[str]]:
    """text を文に分けて判定する。戻り値は (通った文, 落ちた文) の順。

    スコアが読めない文 (``_claim_max_score`` が None) は通った扱い (fail-open)。
    """
    passed: list[str] = []
    failed: list[str] = []
    for sentence in split_claim_sentences(text):
        score = await _claim_max_score(sentence, matches, config=config)
        if score is not None and score < PIR_FOCUS_CLAIM_THRESHOLD:
            failed.append(sentence)
        else:
            passed.append(sentence)
    return passed, failed


async def apply_claim_gate(
    text: str,
    *,
    llm: LLMClient,
    pir: Pir,
    matches: list[PirMatch],
    config: AppConfig | None,
) -> str:
    """文単位の裏付け判定 (既定 off)。``config`` が無い (呼び出し側が未対応) ときも無効。

    ``_CLAIM_GATE_MAX_ATTEMPTS`` (初回 + 作り直し 1 回) の最終試行でもしきい値未満の文が
    残れば、**その文だけ** を落とす (全文落ちたら既存の失敗時の扱いで空文字)。
    """
    if not claim_gate_enabled() or config is None or not text:
        return text

    candidate = text
    passed: list[str] = []
    failed: list[str] = []
    for attempt in range(1, _CLAIM_GATE_MAX_ATTEMPTS + 1):
        passed, failed = await _score_claim_sentences(candidate, matches, config=config)
        if not failed:
            return candidate
        if attempt == 1:
            _log.warning(
                "daily_focus_claim_gate_fired",
                pir_id=pir.id,
                failed_count=len(failed),
            )
        if attempt == _CLAIM_GATE_MAX_ATTEMPTS:
            break  # 最終試行: これ以上は作り直さず、下で失格文を落とす
        # 要点全体を作り直す (識別子関門もこの再生成に乗る — 既存の _generate_llm_summary を
        # そのまま呼ぶ、合成ロジックを複製しない)
        candidate = await _generate_llm_summary(llm, pir, matches)
        if not candidate:
            # 既存の失敗時の扱い (headline のみ)
            return ""

    if not passed:
        _log.warning("daily_focus_claim_gate_all_dropped", pir_id=pir.id)
        return ""
    _log.warning(
        "daily_focus_claim_gate_dropped_sentences",
        pir_id=pir.id,
        dropped_count=len(failed),
    )
    return "。".join(passed) + "。"


@dataclass(frozen=True)
class PirFocusSection:
    """1 PIR 分の daily focus section。"""

    pir: Pir
    matches: list[PirMatch]  # importance 順 + recency でソート済、上限 _MAX_ARTICLES_PER_PIR 件
    total_match_count: int  # フィルタ前の全 match 件数
    llm_summary: str  # LLM 生成 1-2 文 (空文字なら未生成)


@dataclass(frozen=True)
class DailyFocusRunResult:
    """pir-daily-focus run の結果。"""

    sections: list[PirFocusSection] = field(default_factory=list)
    posted: bool = False
    skipped_no_matches: bool = False
    errors: list[str] = field(default_factory=list)


_IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _sort_matches(matches: list[PirMatch]) -> list[PirMatch]:
    """importance 高い順 → created_at 新しい順。"""
    return sorted(
        matches,
        key=lambda m: (
            _IMPORTANCE_ORDER.get((m.importance or "").lower(), 99),
            -_parse_ts(m.created_at),
        ),
    )


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _filter_meaningful_matches(matches: list[PirMatch]) -> list[PirMatch]:
    """importance が medium/high のもののみ採用 (low は noise として除外)。"""
    return [m for m in matches if (m.importance or "").lower() in ("high", "medium")]


def _today_label(tz_name: str = "Asia/Tokyo") -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _build_prompt(pir: Pir, matches: list[PirMatch]) -> str:
    # 層分けの一般化 (2026-08-20、3 本目): 編集層の SSoT は DB (config_store,
    # key=pir_daily_focus_rubric)。合成に失敗したら legacy .j2 に落ちる (WARNING を
    # 残す = 無音にしない)。rollback: PIR_DAILY_FOCUS_COMPOSER=0。
    template = None
    from src.prompts.prompt_store import build_prompt_template
    from src.prompts.registry import get_spec

    spec = get_spec("pir_daily_focus")
    if spec is not None:
        template = build_prompt_template(spec, Path("prompts") / _PROMPT_TEMPLATE)
    if template is None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(Path("prompts"))),
            autoescape=False,
            keep_trailing_newline=True,
        )
        template = env.get_template(_PROMPT_TEMPLATE)
    # 要約は切らずに全文渡す (2026-10-11 P4b)。先頭段落だけに切っていた旧実装
    # (``_first_paragraphs``) は、字数切り (``m.summary[:240]``) の改善として入れたが、
    # 2 段落目以降にしか書かれていない事実まで入力から消してしまい、Opus 裁定の比較で
    # 旧実装の正しい記述を「入力に無い」と誤判定させた (C3b は無効、data/eval/improve_progress.md)。
    # 1 PIR あたり記事は ``_MAX_ARTICLES_PER_PIR`` (3) 件までで総字数は抑えられているため、
    # プロンプト長の調整は記事数 (N) で行う方針とし、字数での機械的な切り捨てはしない。
    return template.render(pir=pir, matches=matches)


def unwrap_json_text(text: str) -> str:
    """出力が ``{"summary": "…"}`` のような 1 つの文字列だけを包んだ JSON なら中身を返す。

    ⚠ 2026-10-02: 09-29 に担当を s21 (JSON で答える課題を学習) にしてから、要点が JSON に
    包まれたまま朝ブリーフに出ていた (毎日全件)。中身の文は正しいので、包みだけを外す
    (文は 1 字も変えない)。包みでなければそのまま返す。
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    if isinstance(obj, dict) and len(obj) == 1:
        (value,) = obj.values()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return text


async def _generate_llm_summary(
    llm: LLMClient,
    pir: Pir,
    matches: list[PirMatch],
) -> str:
    """LLM に 1-2 文要点を生成させる。

    失敗時、または識別子関門 (``fidelity_gate_enabled``) を再生成込みで満たせなかった
    場合は空文字 (呼び出し側は既存の LLM 失敗時と同じ扱いで headline のみにする)。
    """
    prompt = _build_prompt(pir, matches)
    gate_on = fidelity_gate_enabled()
    max_attempts = _FIDELITY_GATE_MAX_ATTEMPTS if gate_on else 1

    for attempt in range(1, max_attempts + 1):
        try:
            response = await llm.generate(
                prompt=prompt,
                temperature=_LLM_TEMPERATURE,
                max_tokens=_LLM_MAX_TOKENS,
                think=False,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("daily_focus_llm_failed", pir_id=pir.id, error=str(e))
            return ""

        text = unwrap_json_text(response.text.strip())
        if not gate_on:
            return text

        unsupported = find_unsupported_identifiers(text, prompt)
        if not unsupported:
            return text

        _log.warning(
            "daily_focus_fidelity_gate_fired",
            pir_id=pir.id,
            attempt=attempt,
            unsupported_count=len(unsupported),
            unsupported_kinds=sorted({ident.kind for ident in unsupported}),
        )

    # 再生成しても出典に実在しない識別子が残った → 要点を出さない (空文字)
    return ""


def _format_match_line(m: PirMatch) -> str:
    imp = (m.importance or "low").lower()
    icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(imp, "·")
    title = (m.title or m.article_id).strip()
    feed = m.feed_title or "source"
    # S1: 出典の信頼度マーカー (朝の通読で SNS/一次を一目で。tier は feed メタから決定的)。
    from src.cti.source_basis import SOURCE_TIER_MARKER, classify_source_tier

    tier = classify_source_tier(m.feed_title or "", m.feed_url or "")
    marker = SOURCE_TIER_MARKER.get(tier, "")
    return f"  {icon} [{imp}]{marker} [{title}]({m.url}) — {feed}"


def _format_section(section: PirFocusSection) -> str:
    """1 PIR 分の Markdown section。"""
    head = f"### {section.pir.title}  ({section.total_match_count} match"
    if section.total_match_count > len(section.matches):
        head += f", top {len(section.matches)}"
    head += ")"
    lines = [head]
    for m in section.matches:
        lines.append(_format_match_line(m))
    if section.llm_summary:
        lines.append(f"  💡 {section.llm_summary}")
    return "\n".join(lines)


def _format_full_digest(sections: list[PirFocusSection], period_label: str) -> str:
    """全 PIR section を結合して 1 つの digest 本文に。"""
    header = (
        f"📍 **PIR Daily Focus — {period_label}** (last 24h)\n"
        f"各 PIR 領域で直近 24h に match した article (importance ≥ medium) を集約。\n"
    )
    body = "\n\n".join(_format_section(s) for s in sections)
    return f"{header}\n{body}"


# Discord 要点射影 (2026-07-12): full digest (記事 3 行 + 💡) は朝刊の 77% を占め毎朝
# 4-5 連投の主犯だった。Discord には 1 PIR = 1 行のみ、記事リンクは Web 全文で読む。
_COMPACT_GIST_MAX = 160  # 1 行要点の上限 (llm_summary は 1-2 文 ≈ 100-150 字想定)


def format_compact_digest(sections: list[PirFocusSection], period_label: str) -> str:
    """Discord 用の PIR 要点射影: 1 PIR = 1 行 (決定論、LLM 追加呼出なし)。"""
    if not sections:
        return ""
    lines = [f"📍 **PIR Daily Focus — {period_label}** (last 24h, {len(sections)} 領域)"]
    for s in sections:
        imps = {(m.importance or "low").lower() for m in s.matches}
        icon = "🔴" if "high" in imps else ("🟡" if "medium" in imps else "⚪")
        gist = (s.llm_summary or (s.matches[0].title if s.matches else "") or "").strip()
        if len(gist) > _COMPACT_GIST_MAX:
            gist = gist[: _COMPACT_GIST_MAX - 1] + "…"
        line = f"{icon} **{s.pir.title}** ({s.total_match_count} match)"
        if gist:
            line += f" — {gist}"
        lines.append(line)
    return "\n".join(lines)


async def collect_pir_focus_sections(
    *,
    llm: LLMClient,
    lookback_hours: int = 24,
    max_pirs_with_llm: int = 25,
    config: AppConfig | None = None,
) -> list[PirFocusSection]:
    """全 enabled PIR を loop して match を集計、各 PIR に LLM 1-2 文要約を付加。

    Args:
        llm: Ollama client (per-PIR で 1 call)
        lookback_hours: PIR match の振り返り期間 (default 24h)
        max_pirs_with_llm: LLM 要約を付ける PIR 上限 (cost 安全弁)
        config: 文単位の裏付け判定 (``PIR_FOCUS_CLAIM_GATE``) が使う。省略時は無効

    Returns:
        match が >=1 件の PIR のみを含む list (空 PIR は除外)
    """
    cfg = load_current_pir_config()
    sections: list[PirFocusSection] = []

    enabled = [p for p in cfg.priorities if p.enabled]
    _log.info(
        "daily_focus_pirs_total",
        enabled_count=len(enabled),
        lookback_hours=lookback_hours,
    )

    for pir in enabled:
        all_matches = evaluate_pir_matches(
            pir,
            lookback_hours=lookback_hours,
            limit=2000,
        )
        meaningful = _filter_meaningful_matches(all_matches)
        if not meaningful:
            continue
        sorted_matches = _sort_matches(meaningful)
        top = sorted_matches[:_MAX_ARTICLES_PER_PIR]

        # LLM 要約 (上限超えなら skip)
        llm_summary = ""
        if len(sections) < max_pirs_with_llm:
            llm_summary = await _generate_llm_summary(llm, pir, top)
            llm_summary = await apply_claim_gate(
                llm_summary, llm=llm, pir=pir, matches=top, config=config
            )

        sections.append(
            PirFocusSection(
                pir=pir,
                matches=top,
                total_match_count=len(meaningful),
                llm_summary=llm_summary,
            ),
        )

    _log.info(
        "daily_focus_sections_built",
        section_count=len(sections),
        with_llm=sum(1 for s in sections if s.llm_summary),
    )
    return sections


async def run_pir_daily_focus(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    llm: LLMClient,
    publishers: dict[DiscordChannel, DiscordPublisher],
    dry_run: bool = False,
) -> DailyFocusRunResult:
    """pir-daily-focus pipeline 本体。"""
    lookback = pipeline.source.digest_lookback_hours or 24
    period_label = _today_label()

    sections = await collect_pir_focus_sections(
        llm=llm,
        lookback_hours=lookback,
        config=config,
    )

    if not sections:
        _log.info("daily_focus_skip_empty", pipeline=pipeline.name)
        return DailyFocusRunResult(
            sections=[],
            posted=False,
            skipped_no_matches=True,
            errors=[],
        )

    body = _format_full_digest(sections, period_label)

    # Discord embed 用 sources (上位 5 件、参照リンク)
    sources: list[Source] = []
    for s in sections:
        for m in s.matches:
            sources.append(
                Source(title=m.feed_title or "source", url=m.url, language="auto"),
            )
            if len(sources) >= 5:
                break
        if len(sources) >= 5:
            break

    message = BriefingMessage(
        title=f"PIR Daily Focus ({period_label})",
        bluf=f"{len(sections)} PIR で match あり",
        importance="medium",
        category="pir_daily_focus",
        summary=body,
        sources=sources,
        metadata={
            "target_channel": DAILY_FOCUS_TARGET_CHANNEL,
            "digest_kind": "pir_daily_focus",
            "pir_section_count": len(sections),
            "routing_reason": "digest_pipeline:pir_daily_focus",
            "routing_rule_id": "DIGEST",
        },
    )

    if dry_run:
        _log.info(
            "daily_focus_dry_run",
            pipeline=pipeline.name,
            section_count=len(sections),
            body_chars=len(body),
        )
        return DailyFocusRunResult(
            sections=sections,
            posted=False,
            skipped_no_matches=False,
            errors=[],
        )

    publisher = publishers.get(DAILY_FOCUS_TARGET_CHANNEL)
    if publisher is None:
        _log.error(
            "daily_focus_publisher_missing",
            pipeline=pipeline.name,
            channel=DAILY_FOCUS_TARGET_CHANNEL,
        )
        return DailyFocusRunResult(
            sections=sections,
            posted=False,
            skipped_no_matches=False,
            errors=[f"publisher_missing:{DAILY_FOCUS_TARGET_CHANNEL}"],
        )

    try:
        await publisher.post(message)
    except Exception as e:  # noqa: BLE001
        _log.error("daily_focus_post_failed", pipeline=pipeline.name, error=str(e))
        return DailyFocusRunResult(
            sections=sections,
            posted=False,
            skipped_no_matches=False,
            errors=[f"discord_post: {type(e).__name__}: {e}"],
        )

    _log.info(
        "daily_focus_posted",
        pipeline=pipeline.name,
        section_count=len(sections),
        body_chars=len(body),
    )
    return DailyFocusRunResult(
        sections=sections,
        posted=True,
        skipped_no_matches=False,
        errors=[],
    )


__all__: list[str] = [
    "DAILY_FOCUS_TARGET_CHANNEL",
    "PIR_FOCUS_CLAIM_GATE_FLAG_ENV",
    "PIR_FOCUS_CLAIM_THRESHOLD",
    "PIR_FOCUS_FIDELITY_GATE_FLAG_ENV",
    "DailyFocusRunResult",
    "PirFocusSection",
    "apply_claim_gate",
    "claim_gate_enabled",
    "collect_pir_focus_sections",
    "fidelity_gate_enabled",
    "run_pir_daily_focus",
    "split_claim_sentences",
]

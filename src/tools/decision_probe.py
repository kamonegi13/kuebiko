"""汎用の logprob 判定部品 (Jev / System-1 型、S4、2026-10-11)。

``(prompt, 選択肢)`` → 各選択肢の確率 を、文章を生成せず **1 回の前向き計算**
(``num_predict=1``, ``temperature=0``) で読む。試作 (data/mlx/s1_common.py、
data/eval/s1_report.md) の本番移植。最初の用途は薄い本文の 2 段目判定
(``src/pipeline/briefing.py`` の ``thin_body_probe``)。

設計:
- モデル名は呼び出し側が選ばず、``model_tiers`` の ``Step`` から解決する
  (``STEP_REGISTRY`` → ティア → 実モデル、CLAUDE.md §4 の denylist 検証を通る経路を強制する)。
- 外部 LLM (``anthropic:``/``claudecode:``/ユーザ定義接続先) が割り当てられている場合は
  判定を行わない (``None``)。logprob を読む口を持たないプロバイダのため。
- モデルの系統ごとにプロンプトの組み方が異なる。JSON 課題で学習した SFT モデル
  (``_JSON_BIASED_MODELS``) は素の 1 語応答だと最初のトークンが ``{"`` に寄る
  (s1_report.md §3 実測) ため、raw モードで ``{"answer": "`` まで前置してから続きを読む。
  それ以外のモデルは Ollama 自身のチャットテンプレートに任せる (``/api/chat``)。
- 選択肢の表記揺れ (Yes/yes/Yes. 等) は末尾の句読点 1 文字までを剥がして比較する。
  複数トークンに分かれる選択肢 (「いいえ」) は最初のトークン (選択肢の先頭一致) で照合する。
- 全選択肢が top_logprobs に見つからない場合、呼出自体が失敗した場合、外部 LLM 割当の
  場合はいずれも ``None`` を返す — 呼び出し側は従来の決定的な経路にそのまま戻ること。
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import ollama

from src.logging_config import get_logger
from src.tools.llm_client import LLMForbiddenModelError, validate_model_name
from src.tools.local_usage import record_probe_usage
from src.tools.model_tiers import STEP_REGISTRY, Step, is_external_model, resolve_step_model
from src.tools.ollama_timing import extract_timing
from src.tools.task_prefix import PREFIX_TRAINED_MODELS, is_prefix_trained

if TYPE_CHECKING:
    from src.config_loader import AppConfig

_log = get_logger(__name__)

#: 1 回の前向き計算で読む候補数 (表記揺れを拾うのに十分な余裕を持たせる。s1_common.py 踏襲)。
DEFAULT_TOP_LOGPROBS = 20

#: JSON 課題で学習した SFT モデル (素の 1 語応答が ``{"`` に寄る、s1_report.md §3 実測)。
#: このモデルを判定に使う場合だけ raw モード + JSON 前置きで読む。
#: 新しいモデルを足すときは、素の 1 語応答で実際に偏りがあるか試走して確かめること
#: (raw は Ollama 自身のテンプレート適用を経由せず、バージョン更新で挙動が変わるリスクが
#: あるため、必要なモデルだけに限定する — 全モデルの既定にはしない)。
#: JSON で答える課題を学習した自前 SFT (= 課題接頭辞を学習したモデル)。SSoT は task_prefix —
#: 名前で複製しない (10-11: i58 と同一 digest の s23i が本番名で、i58 だけを列挙すると本番で
#: 前置きが効かず常に None になっていた)。
_JSON_BIASED_MODELS: frozenset[str] = PREFIX_TRAINED_MODELS

#: raw モードでの JSON 前置き (アシスタント側の応答をここまで埋めて続きを読む)。
_JSON_ANSWER_PREFIX = '{"answer": "'

#: 表記揺れ (Yes. / Yes, 等) の末尾句読点を剥がすための 1 文字セット。
_TOKEN_TRIM_SUFFIXES = (".", ",", "!", "?", "、", "。")


def _is_json_biased(model: str) -> bool:
    """このモデルに raw モード + JSON 前置きを使うか。

    接頭辞つきで学習したモデル (``PREFIX_TRAINED_MODELS``) に加え、評価中の一時タグ
    (環境変数 ``SFT_TASK_PREFIX_MODELS``) も含める (``is_prefix_trained`` と同じ判定)。
    一時タグが chat 経路で測られて較正が本番とずれるのを防ぐ (s24_plan §4.2-1)。
    """
    return model.strip() in _JSON_BIASED_MODELS or is_prefix_trained(model)


def _normalize_token(token: str) -> str:
    """候補トークンを比較用に正規化 (前後空白除去 + 末尾句読点 1 文字除去 + 小文字化)。"""
    text = token.strip()
    if text and text[-1] in _TOKEN_TRIM_SUFFIXES:
        text = text[:-1]
    return text.lower()


def _question_with_choices(question: str, choices: tuple[str, ...]) -> str:
    """選択肢を列挙した質問文 (末尾の答え欄は呼出側 (raw/chat) で組む)。"""
    choices_str = " または ".join(choices)
    return f"{question}\n\n1語だけで答えてください ({choices_str})。"


def _json_biased_prompt(question: str, choices: tuple[str, ...]) -> str:
    """JSON 課題で学習した SFT モデル向け raw プロンプト (Gemma chat テンプレート手組み)。

    Ollama 自身のテンプレート適用を経由せず、chat ターンを手組みし、さらにアシスタント
    側の返答を ``_JSON_ANSWER_PREFIX`` まで前置して続きを読む (素の 1 語応答だと最初の
    トークンが ``{"`` に寄ってしまうのを避ける対策、s1_common.py 踏襲)。
    """
    body = _question_with_choices(question, choices)
    return f"<start_of_turn>user\n{body}<end_of_turn>\n<start_of_turn>model\n{_JSON_ANSWER_PREFIX}"


def _default_messages(question: str, choices: tuple[str, ...]) -> list[dict[str, str]]:
    """素のモデル向け (Ollama 自身のチャットテンプレートに任せる)。"""
    content = f"{_question_with_choices(question, choices)}\n答え:"
    return [{"role": "user", "content": content}]


def _choice_for_token(token: str, choices: tuple[str, ...]) -> str | None:
    """候補トークンが指す選択肢。選択肢の**先頭**に一致すれば、その選択肢の最初のトークンとみなす。

    選択肢は複数トークンに分かれうる (Gemma で「いいえ」=「いい」+「え」)。完全一致だけで照合すると
    分かれた選択肢が常に見つからず floor 扱いになり、明白な「いいえ」が P(はい)≈0.88 に化けていた
    (2026-10-11、摘発発表・新事実の判定がほぼ全件「はい」)。複数の選択肢の先頭に当たるトークンは
    どちらの証拠にもしない (``None``)。
    """
    norm = _normalize_token(token)
    if not norm:
        return None
    matched = [c for c in choices if _normalize_token(c).startswith(norm)]
    return matched[0] if len(matched) == 1 else None


def _count_choices_found(candidates: list[tuple[str, float]], choices: tuple[str, ...]) -> int:
    """top_logprobs の候補に実際に在った選択肢の数 (floor で補う前、P0-16 の記録用)。"""
    found = {c for token, _ in candidates if (c := _choice_for_token(token, choices)) is not None}
    return len(found)


def _extract_choice_probabilities(
    candidates: list[tuple[str, float]], choices: tuple[str, ...]
) -> dict[str, float] | None:
    """top_logprobs の候補一覧から各選択肢の確率 (softmax) を読む。

    どの選択肢も候補に無ければ ``None`` (呼び出し側は従来の決定的な経路へ)。一部だけ見つかった
    ときは、見つからない選択肢の logprob を候補一覧の最小値 (= top_logprobs に入らなかった上限)
    とみなす。2026-10-11: 全選択肢を必須にしていたため、確信の強い判定ほど反対の選択肢が
    top から落ちて None になり、台帳の割当で 2/3 が欠落していた (反対側の相対確率は 1e-7〜1e-10)。
    """
    best_logprob: dict[str, float] = {}
    for token, logprob in candidates:
        choice = _choice_for_token(token, choices)
        if choice is None:
            continue
        if choice not in best_logprob or logprob > best_logprob[choice]:
            best_logprob[choice] = logprob
    if not best_logprob:
        return None
    floor = min(lp for _, lp in candidates)
    for choice in choices:
        best_logprob.setdefault(choice, floor)

    max_logprob = max(best_logprob.values())
    exp_values = {choice: math.exp(lp - max_logprob) for choice, lp in best_logprob.items()}
    denom = sum(exp_values.values())
    return {choice: value / denom for choice, value in exp_values.items()}


async def probe_choice(
    step: Step,
    question: str,
    choices: tuple[str, ...],
    *,
    config: AppConfig,
    db_path: Path | None = None,
    top_logprobs: int = DEFAULT_TOP_LOGPROBS,
    client: ollama.AsyncClient | None = None,
) -> dict[str, float] | None:
    """``(question, choices)`` → 選択肢ごとの確率。生成せず 1 回の前向き計算で読む。

    モデルは ``step`` から解決する (model_tiers、CLAUDE.md §4 の denylist 検証込み)。
    外部 LLM 割当時・denylist 抵触時・応答に選択肢が無い時・通信失敗時はすべて ``None``
    (呼び出し側は従来の決定的な経路にそのまま戻ること — この関数は補強であって必須経路ではない)。
    """
    if not choices:
        raise ValueError("choices は 1 件以上必要です")

    model = resolve_step_model(step, db_path=db_path)
    if is_external_model(model):
        # logprob を読む口を持たない (Anthropic/Claude Code/ユーザ定義接続先)。
        return None
    try:
        validate_model_name(model)
    except LLMForbiddenModelError:
        # 構築前の最終防御 (CLAUDE.md §4)。到達しない想定だが fail-safe に None で返す。
        _log.error("decision_probe_forbidden_model", step=step.value, model=model)
        return None

    spec = STEP_REGISTRY[step]
    owns_client = client is None
    active_client = (
        client
        if client is not None
        else ollama.AsyncClient(host=config.ollama_base_url, timeout=spec.timeout_seconds)
    )
    response: ollama.GenerateResponse | ollama.ChatResponse
    started = time.monotonic()

    def _emit_error(exc: Exception) -> None:
        record_probe_usage(
            step=step.value, model=model, started_monotonic=started, error_class=type(exc).__name__
        )

    try:
        if _is_json_biased(model):
            response = await active_client.generate(
                model=model,
                prompt=_json_biased_prompt(question, choices),
                raw=True,
                logprobs=True,
                top_logprobs=top_logprobs,
                options={"num_predict": 1, "temperature": 0},
            )
        else:
            response = await active_client.chat(
                model=model,
                messages=_default_messages(question, choices),
                think=False,
                logprobs=True,
                top_logprobs=top_logprobs,
                options={"num_predict": 1, "temperature": 0},
            )
    except ollama.ResponseError as e:
        _emit_error(e)
        _log.warning(
            "decision_probe_response_error", step=step.value, model=model, error=str(e)[:200]
        )
        return None
    except (TimeoutError, httpx.TimeoutException) as e:
        _emit_error(e)
        _log.warning("decision_probe_timeout", step=step.value, model=model, error=str(e)[:200])
        return None
    except (httpx.ConnectError, ConnectionError) as e:
        _emit_error(e)
        # ollama 公式 SDK は httpx.ConnectError を組み込み ConnectionError に変換して
        # 再 raise するため両方拾う (src/tools/llm_client.py:_chat と同じ流儀)。
        _log.warning(
            "decision_probe_connection_error", step=step.value, model=model, error=str(e)[:200]
        )
        return None
    except httpx.RequestError as e:
        _emit_error(e)
        _log.warning(
            "decision_probe_request_error", step=step.value, model=model, error=str(e)[:200]
        )
        return None
    finally:
        if owns_client:
            # ollama SDK の AsyncClient.close は戻り値型注釈を持たない (upstream stub の欠落)。
            await active_client.close()  # type: ignore[no-untyped-call]

    logprobs = response.logprobs
    in_tokens = int(getattr(response, "prompt_eval_count", None) or 0)
    out_tokens = int(getattr(response, "eval_count", None) or 0)
    timing = extract_timing(response)
    if not logprobs:
        # サーバ/モデルが logprobs を返さない (未対応バージョン等)。安全側で None。
        record_probe_usage(
            step=step.value,
            model=model,
            started_monotonic=started,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            choices_present=False,
            floor_used=False,
            load_ms=timing.load_ms,
            prompt_eval_ms=timing.prompt_eval_ms,
            eval_ms=timing.eval_ms,
        )
        return None
    entry = logprobs[0]
    candidates: list[tuple[str, float]] = [(t.token, t.logprob) for t in (entry.top_logprobs or ())]
    if not any(token == entry.token for token, _ in candidates):
        # 温度 0 なので通常は argmax = top_logprobs の先頭に含まれるが、念のため補う。
        candidates.append((entry.token, entry.logprob))

    found = _count_choices_found(candidates, choices)
    record_probe_usage(
        step=step.value,
        model=model,
        started_monotonic=started,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        choices_present=found == len(choices),
        floor_used=0 < found < len(choices),
        load_ms=timing.load_ms,
        prompt_eval_ms=timing.prompt_eval_ms,
        eval_ms=timing.eval_ms,
    )
    return _extract_choice_probabilities(candidates, choices)

"""retrieval few-shot プール — 較正格子 P5 (docs/self_evolving_tuning_design.md §4 C8)。

高確信の遅延正解ラベル (E1 active) を教材プールとし、判定対象記事の近傍 k=2 件を
judgment プロンプトの例示層へ **code 所有で** 動的注入する (DB block は触らない)。

- 検索: embedding 近傍 (dedup 基盤の EmbeddingClient を流用。プール側は構築時に自前で
  埋め込みプロセスキャッシュ — article_embeddings は url_hash キーで結合が遠いため)。
  embedder 不在/失敗時は 同カテゴリ優先 + 新しい順 に fail-open。
- 版数紐づけ: プールキャッシュは judgment_rubric の現行版をキーに持ち、版が動いたら
  自動で組み直す (旧基準の下で選ばれた例を漫然と使い続けない)。
- 自己混入ガード: 類似度 0.97 以上の例は除外 (パネル/goldset が対象記事自身を教材に
  引くと答えの漏洩になる)。
- 有効化は ``JUDGMENT_FEWSHOT=1`` (既定 off — §10.1 シャドー先行。goldset 3 者比較で
  効果を実測してから cutover する)。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from src.logging_config import get_logger

_log = get_logger(__name__)

ENV_FLAG = "JUDGMENT_FEWSHOT"

_K = 2
_SELF_SIM = 0.97
_POOL_TTL_SEC = 1800
_POOL_LOOKBACK_DAYS = 120  # body 保持 90 日 + 余裕 (実質は body の残存が上限)
_MAX_POOL = 200
_EXCERPT_CHARS = 200
_TITLE_CHARS = 80


def is_fewshot_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() == "1"


@dataclass(frozen=True)
class Example:
    """教材 1 件 (E1 遅延正解付き事例)。"""

    article_id: str
    title: str
    excerpt: str
    candidates: str
    answer: str
    category: str
    vector: Any  # np.ndarray (正規化済み) | None


@dataclass
class _PoolCache:
    rubric_version: int | None
    built_at: float
    examples: list[Example]


_cache: _PoolCache | None = None


def invalidate_fewshot_pool() -> None:
    """テスト/明示リフレッシュ用。"""
    global _cache  # noqa: PLW0603
    _cache = None


def current_judgment_rubric_version() -> int | None:
    """judgment_rubric の現行版 (プールの版バインドとパネル裁定の層別次元 §13-17 が共用)。"""
    try:
        from src.storage.config_store import list_history

        history = list_history("judgment_rubric", limit=1)
        return history[0].version if history else None
    except Exception:  # noqa: BLE001 — 版が読めなくてもプールは使う (None キー)
        return None


# 後方互換 (テストの monkeypatch 対象)
_current_rubric_version = current_judgment_rubric_version


async def _build_pool(repo: Any, embedder: Any | None, now: datetime) -> list[Example]:
    from src.cti.actor_normalizer import load_actor_aliases
    from src.pipeline.persistence import _relevant_actors

    since_iso = (now - timedelta(days=_POOL_LOOKBACK_DAYS)).isoformat()
    # §13-6: 教材は **パネルが unanimous_correct で確認済み** のラベルに限定する
    # (設計 C8「高確信ラベル」の実装。未確認ラベルを教材にしない)
    rows = repo.fetch_subject_panel_cases(since_iso, confirmed_only=True)[:_MAX_POOL]
    aliases = load_actor_aliases()
    examples: list[Example] = []
    for r in rows:
        title = str(r["title"])[:_TITLE_CHARS]
        excerpt = " ".join(str(r["body"]).split())[:_EXCERPT_CHARS]
        cands = _relevant_actors(aliases.find_all(f"{r['title']}\n{r['body']}"), r["category"])
        # §13-6: 正解が候補ゲート外の例は「候補外の答え」を教える構造ゲート破りの教材に
        # なるため除外する (候補到達可能例のみ)
        if not any(c.id == str(r["truth"]) for c in cands):
            continue
        vector = None
        if embedder is not None:
            try:
                resp = await embedder.embed(f"{title}\n{excerpt}", kind="document")
                vec = np.asarray(resp.vector, dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                vector = vec / norm if norm > 0 else None
            except Exception as e:  # noqa: BLE001 — 埋め込み失敗の例は fallback 順位で使う
                _log.warning("fewshot_embed_failed", error=str(e)[:80])
        examples.append(
            Example(
                article_id=str(r["article_id"]),
                title=title,
                excerpt=excerpt,
                candidates=", ".join(c.id for c in cands) or "(候補なし)",
                answer=str(r["truth"]),
                category=str(r["category"]),
                vector=vector,
            )
        )
    return examples


async def _get_pool(repo: Any, embedder: Any | None, now: datetime) -> list[Example]:
    global _cache  # noqa: PLW0603
    version = _current_rubric_version()
    if (
        _cache is not None
        and _cache.rubric_version == version
        and (time.monotonic() - _cache.built_at) < _POOL_TTL_SEC
    ):
        return _cache.examples
    examples = await _build_pool(repo, embedder, now)
    _cache = _PoolCache(rubric_version=version, built_at=time.monotonic(), examples=examples)
    _log.info(
        "fewshot_pool_built",
        size=len(examples),
        embedded=sum(1 for e in examples if e.vector is not None),
        rubric_version=version,
    )
    return examples


def _rank(
    examples: list[Example],
    query_vec: Any | None,
    category: str,
) -> list[Example]:
    """近傍順に並べる。query_vec 無しは 同カテゴリ優先 + プール格納順 (新しい順相当)。"""
    if query_vec is None:
        return sorted(examples, key=lambda e: 0 if e.category == category else 1)
    scored: list[tuple[float, Example]] = []
    for e in examples:
        if e.vector is None:
            continue
        sim = float(np.dot(query_vec, e.vector))
        if sim >= _SELF_SIM:
            continue  # 自己/複製の混入 = 答えの漏洩
        scored.append((sim, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in scored]


async def get_fewshot_section(
    *,
    title: str,
    body: str,
    category: str | None,
    repo: Any | None = None,
    embedder: Any | None = None,
    now: datetime | None = None,
    k: int = _K,
) -> str:
    """判定対象記事に対する例示セクション (プロンプト末尾に足す文字列) を返す。

    例が無い/全経路失敗なら空文字 (fail-open — 注入なしで従来動作)。
    """
    try:
        from src.config_loader import load_app_config
        from src.pipeline.filters import _try_build_embedder
        from src.storage.run_history import RunHistoryRepository

        _repo = repo if repo is not None else RunHistoryRepository()
        _embedder = embedder if embedder is not None else _try_build_embedder(load_app_config())
        base = now or datetime.now(UTC)
        pool = await _get_pool(_repo, _embedder, base)
        if not pool:
            return ""

        query_vec = None
        if _embedder is not None:
            try:
                q_text = f"{title}\n{' '.join(body.split())[:_EXCERPT_CHARS]}"
                resp = await _embedder.embed(q_text, kind="query")
                vec = np.asarray(resp.vector, dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                query_vec = vec / norm if norm > 0 else None
            except Exception as e:  # noqa: BLE001 — 検索失敗は fallback 順位
                _log.warning("fewshot_query_embed_failed", error=str(e)[:80])

        picked = _rank(pool, query_vec, (category or "").strip())[:k]
        if not picked:
            return ""
        lines = ["【過去の確定事例 (基準合わせの参考。本記事とは別の事件 — 内容を混同しない)】"]
        for i, ex in enumerate(picked, 1):
            answer = f'"{ex.answer}"' if ex.answer else '"" (主題なし)'
            lines.append(
                f"例{i}: 「{ex.title}」 抜粋: {ex.excerpt} / 候補: {ex.candidates}"
                f" → 確定した subject_actor_id: {answer}"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — few-shot の失敗で判定を止めない (fail-open)
        _log.warning("fewshot_section_failed", error=str(e)[:80])
        return ""

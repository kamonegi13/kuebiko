"""毎時の群化に被害組織名の別名判定を差し込む (EVENTNEWS_ORG_IDENTITY、2026-10-12)。

``eventnews_hourly_job`` から呼ぶ唯一の入口 ``resolve_org_identity``。候補と既存メンバーの
victim_org キーを、別名と判定された組だけ代表キーへ置き換えて返す。観測と判定が失敗しても
群化は止めない (何も畳まずに元の一覧を返す)。仕様と根拠は ``src/eventnews/org_identity.py``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy.typing as npt

from src.config_loader import AppConfig
from src.eventnews import org_identity as oi
from src.eventnews.grouping import org_norm_enabled
from src.eventnews.models import ItemState, MemberArticle
from src.logging_config import get_logger
from src.storage.repo_org_identity import FORM_VERSION
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

#: 名前の取得で ``article_entities`` に付ける長さの下限 (結合信号の抽出と同じ。短い値は
#: そもそも entity に入らないので、別名の対象にもならない)。
_MIN_NAME_CHARS = 4
_SQL_VICTIM_NAMES = (
    "SELECT article_id, TRIM(value) FROM article_entities"
    " WHERE entity_type = 'victim_org' AND LENGTH(TRIM(value)) >= {min_chars}"
    " AND article_id IN ({placeholders})"
)

Existing = Sequence[tuple[ItemState, Sequence[MemberArticle]]]


class _RepoVerdictStore:
    """``VerdictStore`` の DB 実装 (org_identity_pairs)。"""

    def __init__(self, repo: RunHistoryRepository) -> None:
        self._repo = repo

    def get_many(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
        return self._repo.get_org_identity_logits(keys, form=FORM_VERSION)

    def put_many(self, rows: dict[tuple[str, str], float]) -> None:
        self._repo.put_org_identity_logits(rows, form=FORM_VERSION)


def _victim_names(repo: RunHistoryRepository, article_ids: Sequence[str]) -> dict[str, list[str]]:
    """記事ごとの被害組織名 (表示用の生の値。重複なし・出現順)。"""
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    sql = _SQL_VICTIM_NAMES.format(min_chars=_MIN_NAME_CHARS, placeholders=placeholders)
    with repo._connect() as conn:  # noqa: SLF001 — 他の毎時ジョブ関数と同型
        rows = conn.execute(sql, list(article_ids)).fetchall()
    names: dict[str, list[str]] = {}
    for row in rows:
        bucket = names.setdefault(str(row[0]), [])
        if str(row[1]) not in bucket:
            bucket.append(str(row[1]))
    return names


async def resolve_org_identity(
    repo: RunHistoryRepository,
    config: AppConfig,
    candidates: Sequence[MemberArticle],
    existing: Existing,
    vectors: Mapping[str, npt.ArrayLike],
) -> tuple[list[MemberArticle], list[tuple[ItemState, list[MemberArticle]]]]:
    """別名と判定された被害組織名を畳んだ (候補, 既存) を返す。off/shadow/失敗時は入力どおり。"""
    unchanged = (
        list(candidates),
        [(state, list(members)) for state, members in existing],
    )
    mode = oi.org_identity_mode()
    if mode == "off":
        return unchanged
    if not org_norm_enabled():
        _log.warning("org_identity_needs_org_norm")  # 照合キーの作り方がずれるので何もしない
        return unchanged
    try:
        members = [m for _, ms in existing for m in ms]
        pool_ids = list({a.article_id for a in (*candidates, *members)})
        tasks = oi.build_tasks(candidates, members, vectors, _victim_names(repo, pool_ids))

        async def judge(task: oi.NamePairTask) -> float | None:
            return await oi.judge_name_pair(
                task.name_a, task.ctx_a, task.name_b, task.ctx_b, config=config
            )

        same, stats = await oi.resolve_same_pairs(tasks, _RepoVerdictStore(repo), judge)
        alias = oi.build_alias_map(same)
    except Exception as e:  # noqa: BLE001 — 別名判定が作れなくても群化は続ける
        _log.warning("org_identity_failed", error=str(e)[:200])
        return unchanged
    _log.info(
        "org_identity",
        mode=mode,
        tasks=len(tasks),
        judged=stats.judged,
        cache_hits=stats.cache_hits,
        deferred=stats.deferred,
        failed=stats.failed,
        alias_keys=len(alias.mapping),
    )
    if mode == "shadow" or not alias:
        return unchanged
    return oi.apply_alias(candidates, existing, alias)

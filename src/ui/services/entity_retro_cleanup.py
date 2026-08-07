"""actor entity の retro-cleanup (幽霊アクター監査 2026-08-06 の恒久対処)。

辞書 curation (alias の除去・改名・付け替え) 後も article_entities は add-only の
ため過去の誤マッチ行が残り、「言及された組織・関係者」に表示本文のどこにも
居ないアクターが出続ける (hunters / CHROMIUM / GRU / MSS で実害)。本 service は
指定 actor (または全 actor) の (article, actor) 対を **現行 registry の production
matcher** で全スナップショット (同一 article_id の全 run 行の title/summary/body)
に照合し、どこにも照合できない行を削除する。

呼び出し元:
- UI 辞書編集 (pages.py actors_update): 名称が **減った** 保存の直後に該当 actor のみ
  (名称が増えた場合の reattribute_actor と対称の関係)
- MITRE 提案承認 (mitre_alias_conflict): alias を失った側の actor
- scripts/cleanup_stale_actor_entities.py: 全 actor 一括 (dry-run 既定の運用バッチ)

安全弁 (監査バッチと同一):
- ransomware leak 系 feed の行は対象外 — actor は構造化ソースの slug 由来
  (ransomware_ingest) で、本文に名前が無いのは仕様のため
- 全スナップショットで本文が空 (90 日 purge 済み等) の行は検証不能として残す
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry, load_actor_aliases
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

# 構造化ソース (slug 照合) 由来の actor を持つ feed。本文照合の対象外とする。
_STRUCTURED_FEED_MARKERS = ("ransomware.live",)

_CHUNK = 200


@dataclass(frozen=True)
class RetroCleanupStats:
    """retro-cleanup の結果 (dry-run 時 deleted=0、候補は delete_candidates)。"""

    checked: int = 0
    deleted: int = 0
    kept_matched: int = 0
    kept_structured: int = 0
    kept_unverifiable: int = 0
    delete_candidates: tuple[tuple[str, str, str], ...] = ()  # (article_id, value, reason)
    deleted_by_actor: dict[str, int] = field(default_factory=dict)


def cleanup_stale_actor_entities(
    repo: RunHistoryRepository,
    *,
    actor_ids: Sequence[str] | None = None,
    registry: ActorAliasRegistry | None = None,
    apply: bool = True,
) -> RetroCleanupStats:
    """現行辞書で照合できない actor entity 行を削除する。

    ``actor_ids`` を渡すと該当 actor の行だけを対象にする (辞書編集フックの
    通常経路)。``apply=False`` で dry-run (削除候補の列挙のみ)。
    """
    reg = registry if registry is not None else load_actor_aliases()

    with repo._connect() as conn:  # noqa: SLF001 — service は repo 接続を直接使う
        if actor_ids:
            ph = ",".join("?" for _ in actor_ids)
            rows = conn.execute(
                "SELECT article_id, value FROM article_entities"
                f" WHERE entity_type='actor' AND value IN ({ph})",
                tuple(actor_ids),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT article_id, value FROM article_entities WHERE entity_type='actor'"
            ).fetchall()

    by_article: dict[str, list[str]] = {}
    for r in rows:
        by_article.setdefault(str(r["article_id"]), []).append(str(r["value"]))

    art_ids = list(by_article)
    resolve_cache: dict[str, ActorAlias | None] = {}
    to_delete: list[tuple[str, str, str]] = []
    kept_matched = 0
    kept_structured = 0
    kept_unverifiable = 0
    checked = 0

    for i in range(0, len(art_ids), _CHUNK):
        chunk = art_ids[i : i + _CHUNK]
        ph = ",".join("?" for _ in chunk)
        with repo._connect() as conn:  # noqa: SLF001
            arts_rows = conn.execute(
                "SELECT article_id, title, summary, body, feed_title"
                f" FROM articles WHERE article_id IN ({ph})",
                tuple(chunk),
            ).fetchall()
        snapshots_by_aid: dict[str, list[dict[str, str]]] = {}
        for r in arts_rows:
            snapshots_by_aid.setdefault(str(r["article_id"]), []).append(
                {
                    "title": str(r["title"] or ""),
                    "summary": str(r["summary"] or ""),
                    "body": str(r["body"] or ""),
                    "feed": str(r["feed_title"] or ""),
                }
            )
        for aid, snapshots in snapshots_by_aid.items():
            is_structured = any(
                m in s["feed"].lower() for s in snapshots for m in _STRUCTURED_FEED_MARKERS
            )
            has_any_body = any(s["body"] for s in snapshots)
            for val in by_article.get(aid, []):
                checked += 1
                if is_structured:
                    kept_structured += 1
                    continue
                if val not in resolve_cache:
                    resolve_cache[val] = reg.by_id(reg.resolve_actor_id(val))
                entry = resolve_cache[val]
                if entry is None:
                    to_delete.append((aid, val, "id_unresolved"))
                    continue
                matched = any(
                    reg.matched_names_for(entry, s[fld])
                    for s in snapshots
                    for fld in ("body", "summary", "title")
                )
                if matched:
                    kept_matched += 1
                elif not has_any_body:
                    kept_unverifiable += 1
                else:
                    to_delete.append((aid, val, "no_match_in_any_snapshot"))

    deleted = 0
    if apply and to_delete:
        with repo._connect() as conn:  # noqa: SLF001
            for aid, val, _reason in to_delete:
                cur = conn.execute(
                    "DELETE FROM article_entities"
                    " WHERE article_id=? AND entity_type='actor' AND value=?",
                    (aid, val),
                )
                deleted += int(cur.rowcount or 0)
        _log.info(
            "actor_entity_retro_cleanup",
            actor_ids=list(actor_ids) if actor_ids else "all",
            checked=checked,
            deleted=deleted,
        )

    return RetroCleanupStats(
        checked=checked,
        deleted=deleted,
        kept_matched=kept_matched,
        kept_structured=kept_structured,
        kept_unverifiable=kept_unverifiable,
        delete_candidates=tuple(to_delete),
        deleted_by_actor=dict(Counter(v for _, v, _ in to_delete)),
    )

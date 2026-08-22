"""帰属の粒度低下を検出し、別名提案へ還流する (2026-08-22)。

**信号**: 記事タイトルが辞書未収録のアクター名を含むのに、主題帰属は本文の**別の**
既知アクターへ LLM 経由で付いている。決定論のタイトル層 (帰属の 64% を担う) が
名前を知らないため発火せず、LLM が近縁の既知アクターへ寄せた形。

- 正しい場合 = 別名の取りこぼし (例: 報道の呼称 ↔ ベンダ designation)
- 誤りの場合 = **粒度低下** (別アクターを既知アクターに丸める) — CTI では無帰属より悪い

どちらかは**人にしか判定できない** (辞書ゲート + 人承認は不変、identity 8 原則)。
本モジュールは判定せず、既存の別名提案キュー (news_alias) へ「同一性を確認せよ」と
起票するだけ。承認されれば alias が追加され直近 90 日が再帰属し、以後は決定論の
タイトル層が発火する。

**実測 (2026-08-22、90 日窓)**: 16 種 / 19 記事。全 16 種が実アクター・キャンペーン名で
ノイズ混入ゼロだった (cavern manticore→iran_mois / mirage kitten→unc1549 /
handala→void_manticore / copycop→storm_1516 等)。

**棄却した設計**: 同じ証拠から「辞書化すると N 件が決定論帰属に転じる」見込み利得を
測り、提案の序列に使う案は**実測で棄却した** — 上位が storm / genesis / payload /
aurora / 習近平 等のノイズで埋まり、既存の一般語・地政学判定器は 1 件も落とさなかった。
「帰属が実際に付いた」ことを要求する本モジュールの条件こそが精度の源であり、
未帰属を数える方向には判別力が無い。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.generic_alias_words import is_generic_alias
from src.cti.news_alias_harvest import PROPOSAL_TYPE_NEWS_ALIAS
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_DEFAULT_WINDOW_DAYS = 90
_SAMPLE_LIMIT = 3
# 1 文字・2 文字の断片は語境界照合でも誤爆しやすいため対象外
_MIN_KEY_LEN = 4


@dataclass(frozen=True)
class DowngradeSuspect:
    """タイトルの未知名 ↔ 本文帰属先の既知アクター の対 (同一性は未判定)。"""

    key: str  # タイトルに出た辞書未収録の名前 (正規化済みの小文字)
    display: str  # 記事タイトル中の実際の綴り (別名として辞書へ入れる表記)
    attributed_to: tuple[str, ...]  # 本文経由で実際に帰属した既知アクター id
    articles: int
    sample_titles: tuple[str, ...]


def _title_match(title: str, key: str) -> str | None:
    """語境界つきの大小無視照合。一致したら**タイトル中の実際の綴り**を返す。

    部分文字列照合だと "pink" が "pinkerton" 等を拾う。英数字の連なりの内側では
    一致させない (日本語は語境界を持たないため前後の英数字のみを見る)。

    綴りを返すのは、別名を辞書へ入れるときに表示表記 (FrostyNeighbor) を保つため
    — 暗定 entity の value は正規化で小文字化されており、そのまま alias にすると
    既存表記 (UNC-1151 / Wicked Panda) と不揃いになり UI・STIX 出力に出る。
    """
    if not title or not key:
        return None
    pattern = rf"(?<![0-9A-Za-z]){re.escape(key)}(?![0-9A-Za-z])"
    m = re.search(pattern, title, flags=re.IGNORECASE)
    return m.group(0) if m else None


def detect_attribution_downgrades(
    repo: RunHistoryRepository,
    *,
    now: datetime | None = None,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> list[DowngradeSuspect]:
    """粒度低下の疑いを記事数の降順で返す。判定はしない (人の確認材料)。"""
    base = now or datetime.now(UTC)
    since = base - timedelta(days=window_days)
    rows = repo.list_provisional_with_article_context(since=since)

    articles: dict[str, set[str]] = {}
    targets: dict[str, list[str]] = {}
    samples: dict[str, list[str]] = {}
    display: dict[str, str] = {}

    for row in rows:
        key = str(row["value"])
        if len(key) < _MIN_KEY_LEN:
            continue
        title = str(row["title"] or "")
        matched = _title_match(title, key)
        if matched is None:
            continue
        # タイトル層が既知アクターで発火した分 (source='title') は正常。
        # source='llm' = タイトルの未知名を無視して本文の別アクターへ寄せた形のみを見る。
        if str(row["subject_actor_source"] or "").strip() != "llm":
            continue
        ids = str(row["subject_actor_ids"] or "").strip().strip("[]").replace('"', "")
        resolved = [a.strip() for a in ids.split(",") if a.strip()]
        if not resolved:
            continue
        articles.setdefault(key, set()).add(str(row["article_id"]))
        display.setdefault(key, matched)
        for actor_id in resolved:
            if actor_id not in targets.setdefault(key, []):
                targets[key].append(actor_id)
        bucket = samples.setdefault(key, [])
        if len(bucket) < _SAMPLE_LIMIT and title not in bucket:
            bucket.append(title)

    suspects = [
        DowngradeSuspect(
            key=key,
            display=display.get(key, key),
            attributed_to=tuple(sorted(targets.get(key, []))),
            articles=len(ids_set),
            sample_titles=tuple(samples.get(key, [])),
        )
        for key, ids_set in articles.items()
    ]
    suspects.sort(key=lambda s: (-s.articles, s.key))
    _log.info(
        "attribution_downgrades_detected",
        suspects=len(suspects),
        articles=sum(s.articles for s in suspects),
    )
    return suspects


def propose_downgrade_aliases(
    repo: RunHistoryRepository,
    *,
    run_id: int | None = None,
    now: datetime | None = None,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> dict[str, int]:
    """疑いを別名提案キューへ起票する。``{proposed, skipped, suspects}`` を返す。

    dedup_key は news_alias 収穫と**同一名前空間** — 同じ (actor, alias) の問いを
    2 経路が二重起票しない (先に上げた方の rationale が残る)。

    起票しないもの: 既知名 (already alias) / 一般語 (SSoT) / 帰属先が複数に割れる例
    (どのアクターの別名を問うべきか決まらないため人の自由記述に委ねる)。
    """
    registry = load_actor_aliases()
    suspects = detect_attribution_downgrades(repo, now=now, window_days=window_days)
    proposed = skipped = 0
    for s in suspects:
        if len(s.attributed_to) != 1:
            skipped += 1  # 帰属先が割れる = 別名の問いとして立てられない
            continue
        actor_id = s.attributed_to[0]
        alias = s.display
        if registry.knows_name(alias) or registry.resolve_source_slug(alias) is not None:
            skipped += 1  # 既に辞書が知っている (暗定側の取りこぼし)
            continue
        if is_generic_alias(alias):
            skipped += 1  # 一般語は別名にしない (2026-07-26 の一般語衝突事故)
            continue
        dedup_key = f"news_alias:{actor_id}:{s.key}"  # 名前空間は正規化キーで固定
        if repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS, dedup_key=dedup_key
        ):
            skipped += 1
            continue
        actor = registry.by_id(actor_id)
        canonical = actor.canonical if actor else actor_id
        payload = {
            "actor_id": actor_id,
            "actor_canonical": canonical,
            "alias": alias,
            "_evidence": {
                "signal": "attribution_downgrade",
                "article_count": s.articles,
                "sample_titles": list(s.sample_titles),
            },
        }
        rationale = (
            f"タイトルが辞書未収録の「{alias}」を名指す記事 {s.articles} 件で、"
            f"主題帰属は本文経由で {canonical} に付いています。"
            f"⚠**同一性は未判定** — {canonical} の別名なら承認 (以後タイトル層が"
            "決定論で帰属)、別アクターなら却下して新規アクターとして起票してください。"
            "丸め込みのまま放置すると粒度低下 (別アクターの同一視) になります。"
        )
        # 1 語名は一般語衝突の温床 (2026-07-26: 一般語 11 体の一括承認で言及層の 59% が
        # 誤検出化)。SSoT 未収録の 1 語名にも承認前確認を促す (新興候補起票と同じ規律)。
        if " " not in alias.strip():
            rationale += "。1 語名 — 別名にすると本文の一般語を拾わないか承認前に確認"
        repo.insert_actor_update_proposal(
            run_id=run_id,
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS,
            mitre_group="",
            dedup_key=dedup_key,
            actor_id=actor_id,
            payload=json.dumps(payload, ensure_ascii=False),
            rationale=rationale,
        )
        proposed += 1
    stats = {"proposed": proposed, "skipped": skipped, "suspects": len(suspects)}
    _log.info("downgrade_alias_proposals", **stats)
    return stats

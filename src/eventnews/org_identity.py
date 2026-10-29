"""被害組織名の別名判定 (2026-10-12)。群化で「被害者が食い違う別事象」と誤って塞ぐのを防ぐ。

``org_match_key`` が畳むのは法人格・総称の付け外しだけ。日本語 ⇔ 英語・略称 ⇔ 正式名・旧社名・
繁体字・親子会社といった**別名は開いた集合**で辞書が追いつかないため、判定役 (ローカル LLM) の
P(はい) を 1 回の前向き計算で読む (Jev 型、``decision_probe``)。2 つの問い (同じ事案の同じ被害者か /
同じ組織か) の両方が基準を超えたときだけ同一とする。判定は名前の組ごとに 1 回だけ行い、
DB に残す (毎時ジョブは別 subprocess なので、プロセス内のキャッシュは効かない)。

使い方 (毎時の群化): 候補記事 × 既存メンバー記事のうち、本文の cos が高く、両方が被害者を
名指ししていて照合キーが重ならない組から名前の組を作る → 未判定の組だけ判定 → 同一と判定された
組を ``AliasMap`` にまとめ、記事の entity の victim_org キーを代表キーへ置き換える。
置き換えれば ``blocked_by_different_victims`` も共有 entity も同じ結果になる (seam は 1 つ)。

rollback: ``EVENTNEWS_ORG_IDENTITY`` を "0" (既定) に戻す。"shadow" は判定と記録だけして
群化には使わない。判定が出せない (外部 LLM 割当・通信失敗) ときは何も畳まない
(= 従来どおり。既存の挙動に無害な側へ倒す)。
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
import numpy.typing as npt

from src.cti.org_normalizer import org_match_key
from src.logging_config import get_logger
from src.tools.decision_probe import probe_choice
from src.tools.model_tiers import Step, resolve_step_model
from src.tools.task_prefix import is_prefix_trained

if TYPE_CHECKING:
    from pathlib import Path

    import ollama

    from src.config_loader import AppConfig
    from src.eventnews.models import ItemState, MemberArticle

_log = get_logger(__name__)

ORG_IDENTITY_ENV = "EVENTNEWS_ORG_IDENTITY"
Mode = Literal["off", "shadow", "on"]

#: 本文の cos がこれ以上の記事の組だけ名前を調べる (30 日の実測: 1 時間あたり 5〜6 組)。
MIN_BODY_COS = 0.60
#: 記事の組 1 つあたりに判定する名前の組の上限 (類似度の高い順)。
MAX_NAME_PAIRS_PER_ARTICLE_PAIR = 8
#: 1 回の run で新たに判定する上限 (超えた分は次の run へ持ち越す。1 判定 ~0.25 秒)。
MAX_JUDGMENTS_PER_RUN = 80
#: 2 つの問い (下の ``same_event_question`` / ``same_org_question``) の logit (= log(P(Yes)/P(No))、
#: ±15 で飽和) が**両方**この値以上のときだけ同一とみなす。しきい値は調整用 97 組で選んだ
#: (event は 12、org は -4)。片方だけだと、event は書式の似た別組織の漏えい報道を、org は文脈の近い
#: 別組織を同一視した (replay 30 日で各 5〜9 組)。両方の AND は確認用 98 組・replay の目視で
#: 誤同一視 0 (回収 88%、親子・運営会社の一部は畳まない)。
EVENT_MIN_LOGIT = 12.0
ORG_MIN_LOGIT = -4.0
#: 保存・比較するのは「しきい値までの余裕の小さい方」。0 以上で同一。
SAME_MARGIN = 0.0
_LOGIT_CLIP = 15.0
_P_EPS = 1e-12
#: 文脈として問いに載せる 1 文の上限。
_CONTEXT_CHARS = 130
#: 名前だけの短い文 (見出し的な 1 行) は文脈にならないので飛ばす。名前の長さにこの余裕を足した
#: 長さに満たない文は、より情報のある次の文を探す。
_STUB_EXTRA_CHARS = 8
#: 名前の組 1 つあたりに判定する文脈 (= 記事の組) の上限。判定は文脈で大きく振れる
#: (2026-10-15 実測: 同じ組でも文脈次第で margin が +3 ⇔ -27)。最初に当たった文脈 1 つで
#: 永続させると、似た被害の記事が来た組が誤って同一に固定される。過半数の文脈で通ったときだけ同一。
MAX_CONTEXTS_PER_NAME_PAIR = 3
_CHOICES = ("Yes", "No")
_SENTENCE_SPLIT = re.compile(r"(?<=[。.!?！？\n])\s*")


def org_identity_mode() -> Mode:
    """``EVENTNEWS_ORG_IDENTITY``: "0"/未設定=off、"shadow"、"1"=on。それ以外は off。"""
    raw = os.environ.get(ORG_IDENTITY_ENV, "0").strip().lower()
    if raw == "shadow":
        return "shadow"
    return "on" if raw == "1" else "off"


@dataclass(frozen=True)
class AliasMap:
    """照合キー → 代表キー。代表は連結成分の最小キー (実行間で決定的)。"""

    mapping: Mapping[str, str]

    def canonical(self, key: str) -> str:
        return self.mapping.get(key, key)

    def __bool__(self) -> bool:
        return bool(self.mapping)


def build_alias_map(same_pairs: Iterable[tuple[str, str]]) -> AliasMap:
    """同一と判定された照合キーの組を連結して AliasMap にする (推移的)。"""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in same_pairs:
        if a == b:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            parent[hi] = lo
    return AliasMap({key: find(key) for key in list(parent)})


def remap_entities(
    entities: frozenset[tuple[str, str]], alias: AliasMap
) -> frozenset[tuple[str, str]]:
    """victim_org のキーだけを代表キーへ置き換える。写像が空なら同じ object を返す。"""
    if not alias:
        return entities
    return frozenset(
        (etype, alias.canonical(value) if etype == "victim_org" else value)
        for etype, value in entities
    )


def pair_key(name_a: str, name_b: str) -> tuple[str, str]:
    """名前の組の保存キー (照合キーの昇順タプル。順序に依らない)。"""
    ka, kb = org_match_key(name_a), org_match_key(name_b)
    return (ka, kb) if ka <= kb else (kb, ka)


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} or ({text} if text else set())


def _name_similarity(key_a: str, key_b: str) -> float:
    """安い類似度 (候補の絞り込み順のためだけ)。文字 2-gram の Jaccard、包含は 1 を足す。"""
    ba, bb = _bigrams(key_a), _bigrams(key_b)
    jaccard = len(ba & bb) / len(ba | bb) if ba and bb else 0.0
    short, long_ = sorted((key_a, key_b), key=len)
    return jaccard + (1.0 if len(short) >= 2 and short in long_ else 0.0)


def candidate_name_pairs(
    names_a: Sequence[str], names_b: Sequence[str], *, limit: int
) -> list[tuple[str, str]]:
    """判定にかける名前の組 (照合キーが既に一致する組は除く)。類似の高い順に ``limit`` 件。"""
    scored: list[tuple[float, int, str, str]] = []
    for a in names_a:
        for b in names_b:
            ka, kb = org_match_key(a), org_match_key(b)
            if ka == kb:
                continue
            scored.append((-_name_similarity(ka, kb), len(scored), a, b))
    scored.sort()
    return [(a, b) for _, _, a, b in scored[:limit]]


def context_sentence(title: str, body: str, summary: str, name: str) -> str:
    """記事の中で名前が出る最初の 1 文。見つからなければ見出し。

    名前だけの短い文 (社名の見出し行など) は文脈にならないので、あれば次の文を優先する。
    """
    needle = unicodedata.normalize("NFKC", name).casefold()
    text = f"{title}。{body or summary}"
    stub: str | None = None
    for sentence in _SENTENCE_SPLIT.split(text):
        if not needle or needle not in unicodedata.normalize("NFKC", sentence).casefold():
            continue
        stripped = sentence.strip()
        if len(stripped) >= len(needle) + _STUB_EXTRA_CHARS:
            return stripped[:_CONTEXT_CHARS]
        stub = stub or stripped
    return (stub or title)[:_CONTEXT_CHARS]


_NAMES_AND_CONTEXTS = (
    "名前1: {name_a}\n名前1が出る文: {ctx_a}\n名前2: {name_b}\n名前2が出る文: {ctx_b}\n"
)


#: 文脈が似ていても名前が別の組織なら「いいえ」にさせる注意書き。2026-10-15: 本番の同一判定 28 組の
#: うち 3 組 (別ホテル・競合する 2 つの小売グループ・別の EC 事業者) は、同じ手口・同時期の別の
#: 被害を述べた文脈に引きずられて「はい」になっていた。
_CAUTION = (
    "注意: 2つの文脈が似た種類の被害(同じ手口・同時期の別の漏えい)を述べていても、名前が別の"
    "組織を指すなら「いいえ」です。同じ事案の別々の被害者同士も「いいえ」です。名前そのものが"
    "同じ組織を指すと言える場合だけ「はい」にしてください。\n"
)


def same_event_question(name_a: str, ctx_a: str, name_b: str, ctx_b: str) -> str:
    """問い 1: 同じ事案の同じ被害者か (親会社・子会社・運営会社・サービス名まで含める)。"""
    return (
        "2つの記事が同じサイバー事案を報じているかを調べています。次の2つの名前が、その事案の"
        "同じ被害者(同一組織、またはその親会社・子会社・運営会社・サービス名)を指すか判定して"
        "ください。\n"
        + _CAUTION
        + _NAMES_AND_CONTEXTS.format(name_a=name_a, ctx_a=ctx_a, name_b=name_b, ctx_b=ctx_b)
        + "同じ事案の同じ被害者を指していますか?"
    )


def same_org_question(name_a: str, ctx_a: str, name_b: str, ctx_b: str) -> str:
    """問い 2: 同じ組織か (表記違い・略称・翻訳・旧社名は同じ、似た名前の別法人は違う)。"""
    return (
        "次の2つの組織名が、同じ組織を指しているか判定してください。表記違い・略称・翻訳・"
        "旧社名は同じ組織、別の組織(似た名前の別法人・同業他社・同時期に似た被害に遭った別法人を"
        "含む)は違います。\n"
        + _CAUTION
        + _NAMES_AND_CONTEXTS.format(name_a=name_a, ctx_a=ctx_a, name_b=name_b, ctx_b=ctx_b)
        + "名前1と名前2は同じ組織ですか?"
    )


def to_logit(p_yes: float) -> float:
    """P(はい) → logit。±15 で飽和 (学習・評価と同じ)。"""
    p = min(max(p_yes, _P_EPS), 1 - _P_EPS)
    return max(-_LOGIT_CLIP, min(_LOGIT_CLIP, math.log(p / (1 - p))))


async def _probe_logit(
    question: str,
    *,
    config: AppConfig,
    db_path: Path | None,
    client: ollama.AsyncClient | None,
) -> float | None:
    probabilities = await probe_choice(
        Step.ORG_IDENTITY_JUDGE,
        question,
        _CHOICES,
        config=config,
        db_path=db_path,
        client=client,
    )
    return None if probabilities is None else to_logit(probabilities["Yes"])


async def judge_name_pair(
    name_a: str,
    ctx_a: str,
    name_b: str,
    ctx_b: str,
    *,
    config: AppConfig,
    db_path: Path | None = None,
    client: ollama.AsyncClient | None = None,
) -> float | None:
    """名前の組が同じ組織を指す余裕 (``SAME_MARGIN`` 以上で同一)。判定できなければ None
    (呼び手は何も畳まない)。2 つの問いを 1 回ずつ読み、小さい方の余裕を返す。

    ⚠ 判定役が SFT 済みモデル (s23i 等) のときは**判定しない**。自由形式の yes/no に常に Yes と
    答える偏りがあり (10-11 実測)、全組が「同一」になって被害者の違う別事象を群化が束ねてしまう。
    fast ティアが SFT のとき、素の 26B は step 上書き ``step:org_identity_judge`` で明示する。
    """
    model = resolve_step_model(Step.ORG_IDENTITY_JUDGE, db_path=db_path)
    if is_prefix_trained(model):
        _log.warning("org_identity_judge_is_sft", model=model)
        return None
    event = await _probe_logit(
        same_event_question(name_a, ctx_a, name_b, ctx_b),
        config=config,
        db_path=db_path,
        client=client,
    )
    if event is None:
        return None
    org = await _probe_logit(
        same_org_question(name_a, ctx_a, name_b, ctx_b),
        config=config,
        db_path=db_path,
        client=client,
    )
    if org is None:
        return None
    return min(event - EVENT_MIN_LOGIT, org - ORG_MIN_LOGIT)


@dataclass(frozen=True)
class NamePairTask:
    name_a: str
    ctx_a: str
    name_b: str
    ctx_b: str

    @property
    def key(self) -> tuple[str, str]:
        return pair_key(self.name_a, self.name_b)


class VerdictStore(Protocol):
    """判定済みの名前の組の永続キャッシュ (key → 余裕。``SAME_MARGIN`` 以上で同一)。"""

    def get_many(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], float]: ...

    def put_many(self, rows: dict[tuple[str, str], float]) -> None: ...


@dataclass(frozen=True)
class ResolveStats:
    judged: int = 0
    cache_hits: int = 0
    deferred: int = 0
    failed: int = 0
    same: int = 0


Judge = Callable[[NamePairTask], Awaitable[float | None]]


def _distinct_contexts(tasks: Sequence[NamePairTask], limit: int) -> list[NamePairTask]:
    """同じ名前の組の文脈を、向きと重複を無視して最大 ``limit`` 件 (出現順)。"""
    seen: set[frozenset[tuple[str, str]]] = set()
    picked: list[NamePairTask] = []
    for task in tasks:
        signature = frozenset({(task.name_a, task.ctx_a), (task.name_b, task.ctx_b)})
        if signature in seen:
            continue
        seen.add(signature)
        picked.append(task)
        if len(picked) >= limit:
            break
    return picked


def lower_median(margins: Sequence[float]) -> float:
    """下側中央値。件数が偶数なら小さい方 — 過半数 (厳密) が通ったときだけ 0 以上になる。"""
    ordered = sorted(margins)
    return ordered[(len(ordered) - 1) // 2]


async def _judge_contexts(contexts: Sequence[NamePairTask], judge: Judge) -> list[float]:
    """文脈を順に判定する。結果が決まったら残りは判定しない (多数が通る/通らないが確定)。"""
    total = len(contexts)
    margins: list[float] = []
    positives = negatives = 0
    for task in contexts:
        margin = await judge(task)
        if margin is None:
            continue
        margins.append(margin)
        if margin >= SAME_MARGIN:
            positives += 1
        else:
            negatives += 1
        if positives * 2 > total or negatives * 2 >= total:
            break
    return margins


async def resolve_same_pairs(
    tasks: Sequence[NamePairTask],
    store: VerdictStore,
    judge: Judge,
    *,
    max_judgments: int = MAX_JUDGMENTS_PER_RUN,
) -> tuple[list[tuple[str, str]], ResolveStats]:
    """名前の組を判定し、同一と判定された照合キーの組を返す。判定は組ごとに 1 回 (DB に残す)。

    1 つの組に複数の文脈 (記事の組) があるときは最大 ``MAX_CONTEXTS_PER_NAME_PAIR`` 件を判定し、
    保存する余裕は下側中央値 (過半数の文脈で通ったときだけ 0 以上)。
    ``max_judgments`` は名前の組の数。
    """
    by_key: dict[tuple[str, str], list[NamePairTask]] = {}
    for task in tasks:
        by_key.setdefault(task.key, []).append(task)
    verdicts = store.get_many(list(by_key))
    fresh: dict[tuple[str, str], float] = {}
    judged = failed = deferred = 0
    for key, key_tasks in by_key.items():
        if key in verdicts:
            continue
        if judged >= max_judgments:
            deferred += 1
            continue
        margins = await _judge_contexts(
            _distinct_contexts(key_tasks, MAX_CONTEXTS_PER_NAME_PAIR), judge
        )
        judged += 1
        if not margins:
            failed += 1
            continue
        fresh[key] = lower_median(margins)
    if fresh:
        store.put_many(fresh)
    verdicts = {**verdicts, **fresh}
    same = [key for key, margin in verdicts.items() if margin >= SAME_MARGIN]
    stats = ResolveStats(
        judged=judged,
        cache_hits=len(by_key) - judged - deferred,
        deferred=deferred,
        failed=failed,
        same=len(same),
    )
    return same, stats


def _unit(vec: npt.ArrayLike) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


def build_tasks(
    candidates: Sequence[MemberArticle],
    members: Sequence[MemberArticle],
    vectors: Mapping[str, npt.ArrayLike],
    names_by_article: Mapping[str, Sequence[str]],
    *,
    min_cos: float = MIN_BODY_COS,
    limit_per_pair: int = MAX_NAME_PAIRS_PER_ARTICLE_PAIR,
) -> list[NamePairTask]:
    """判定にかける名前の組。候補 × (既存メンバー + 候補) のうち、本文が近く、両方が被害者を
    名指しし、照合キーが 1 つも重ならない記事の組 (= ``blocked_by_different_victims`` が塞ぐ組)。"""
    pool = {a.article_id: a for a in (*members, *candidates)}
    with_names = [aid for aid in pool if names_by_article.get(aid) and aid in vectors]
    if not with_names:
        return []
    matrix = np.stack([_unit(vectors[aid]) for aid in with_names])
    index = {aid: i for i, aid in enumerate(with_names)}
    keys = {aid: {org_match_key(n) for n in names_by_article[aid]} for aid in with_names}
    tasks: list[NamePairTask] = []
    seen: set[frozenset[str]] = set()
    for cand in candidates:
        ci = index.get(cand.article_id)
        if ci is None:
            continue
        sims = matrix @ matrix[ci]
        for j in np.flatnonzero(sims >= min_cos):
            other_id = with_names[int(j)]
            pair = frozenset((cand.article_id, other_id))
            if other_id == cand.article_id or pair in seen:
                continue
            seen.add(pair)
            if keys[cand.article_id] & keys[other_id]:
                continue
            other = pool[other_id]
            for name_a, name_b in candidate_name_pairs(
                names_by_article[cand.article_id], names_by_article[other_id], limit=limit_per_pair
            ):
                tasks.append(
                    NamePairTask(
                        name_a=name_a,
                        ctx_a=context_sentence(cand.title, cand.body, cand.summary, name_a),
                        name_b=name_b,
                        ctx_b=context_sentence(other.title, other.body, other.summary, name_b),
                    )
                )
    return tasks


def apply_alias(
    candidates: Sequence[MemberArticle],
    existing: Sequence[tuple[ItemState, Sequence[MemberArticle]]],
    alias: AliasMap,
) -> tuple[list[MemberArticle], list[tuple[ItemState, list[MemberArticle]]]]:
    """候補と既存メンバーの victim_org キーを代表キーへ置き換えた新しい一覧を返す (入力は不変)。"""

    def remap(a: MemberArticle) -> MemberArticle:
        return replace(a, entities=remap_entities(a.entities, alias))

    return (
        [remap(c) for c in candidates],
        [(state, [remap(m) for m in members]) for state, members in existing],
    )

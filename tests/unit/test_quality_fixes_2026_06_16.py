"""データ品質監査 (2026-06-16) の根本修正 R-A / R-B / R-E の unit test。

- R-A: アクター帰属の context gating (organization は cyber 記事でのみ)
- R-B: ioc_url の citation/advisory host を benign 除外
- R-E: sector 正規化の alias 拡充 + fuzzy fallback
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from src.cti.actor_normalizer import ActorAlias
from src.cti.ioc_extractor import filter_benign
from src.cti.taxonomy_normalizer import load_normalizer
from src.main import _filter_semantic_duplicates, _relevant_actors, _resolve_channel
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage
from src.tools.embedding_client import EmbeddingClient, EmbeddingResponse


# ───────── R-A: アクター帰属 context gating ─────────
def _group(aid: str) -> ActorAlias:
    return ActorAlias(id=aid, canonical=aid, kind="group")


def _org(aid: str) -> ActorAlias:
    return ActorAlias(id=aid, canonical=aid, kind="organization")


def test_group_actors_kept_regardless_of_category() -> None:
    actors = [_group("apt28"), _group("lazarus")]
    # 非 cyber カテゴリでも group (実 APT) は残る
    assert {a.id for a in _relevant_actors(actors, "geopolitical")} == {"apt28", "lazarus"}


def test_organization_dropped_in_non_cyber_article() -> None:
    actors = [_org("russia_gru"), _org("iran_irgc"), _group("apt28")]
    # geopolitical 記事: organization (言及) は除外、group のみ残る
    out = _relevant_actors(actors, "geopolitical")
    assert {a.id for a in out} == {"apt28"}


def test_organization_kept_in_cyber_article() -> None:
    actors = [_org("russia_gru"), _group("sandworm")]
    # cyber カテゴリ (apt/malware/incident 等) では organization 帰属も認める
    for cat in ("apt", "malware", "incident", "breach", "vulnerability"):
        out = _relevant_actors(actors, cat)
        assert {a.id for a in out} == {"russia_gru", "sandworm"}, cat


def test_relevant_actors_empty_category() -> None:
    actors = [_org("russia_gru"), _group("apt28")]
    assert {a.id for a in _relevant_actors(actors, None)} == {"apt28"}
    assert {a.id for a in _relevant_actors(actors, "")} == {"apt28"}


# ───────── R-B: ioc_url citation/advisory 除外 ─────────
def test_filter_benign_drops_cve_database_urls() -> None:
    urls = [
        "https://www.cve.org/CVERecord?id=CVE-2026-1234",
        "https://nvd.nist.gov/vuln/detail/CVE-2026-1234",
        "https://www.mozilla.org/en-US/security/advisories/mfsa2026-41/",
        "https://source.android.com/docs/security/bulletin/2026/2026-05-01",
    ]
    assert filter_benign(urls) == []  # すべて出典 → 除外


def test_filter_benign_keeps_real_c2() -> None:
    iocs = [
        "https://evil-c2-domain.xyz/gate.php",
        "https://www.cve.org/CVERecord?id=CVE-2026-1",  # 出典 → 除外
        "malicious-payload.top",
    ]
    out = filter_benign(iocs)
    assert "https://evil-c2-domain.xyz/gate.php" in out
    assert "malicious-payload.top" in out
    assert all("cve.org" not in x for x in out)


# ───────── R-E: sector 正規化 alias + fuzzy ─────────
def test_sector_new_aliases_exact() -> None:
    n = load_normalizer()
    assert n.normalize_sector("製造業")[0] == "manufacturing"
    assert n.normalize_sector("critical manufacturing")[0] == "manufacturing"
    assert n.normalize_sector("multi-sector")[0] == "multi_sector"
    assert n.normalize_sector("政府・公共サービス")[0] == "government"
    assert n.normalize_sector("network infrastructure")[0] == "telecom"


def test_sector_fuzzy_fallback() -> None:
    n = load_normalizer()
    # 完全一致しないが既知 alias を部分文字列に含む → fuzzy で吸収
    assert n.normalize_sector("製造業界全体")[0] == "manufacturing"  # 「製造」を含む
    assert n.normalize_sector("中央政府機関")[0] == "government"  # 「政府」を含む


def test_sector_unknown_still_uncategorized() -> None:
    n = load_normalizer()
    # alias を一切含まない真の未知値は uncategorized のまま
    assert n.normalize_sector("税制改正の動向")[0] == "uncategorized"
    assert n.normalize_sector("XYZ未知カテゴリ")[0] == "uncategorized"


def test_sector_fuzzy_no_short_ascii_false_match() -> None:
    n = load_normalizer()
    # 短い ASCII alias (gas/oil/bank/tech 等) で誤マッチしない (min 5 文字制約)
    # "Las Vegas" は "gas" を含むが energy にしない
    assert n.normalize_sector("Las Vegas tourism board")[0] != "energy"


def test_sector_fuzzy_word_boundary_ascii() -> None:
    n = load_normalizer()
    # ASCII は単語境界マッチ: "hospitality" は "hospital" を含むが healthcare にしない
    assert n.normalize_sector("hospitality")[0] != "healthcare"
    # 一方で語として現れれば解決する
    assert n.normalize_sector("hospital network")[0] == "healthcare"


# ───────── Grok alert 衛生ゲート (importance=low は alert 昇格不可) ─────────
_IMP_MAP: dict[Literal["high", "medium", "low"], str] = {
    "high": "alert",
    "medium": "watch",
    "low": "watch",
}


def _bm(target: str, importance: Literal["high", "medium", "low"]) -> BriefingMessage:
    return BriefingMessage(
        title="t",
        summary="s",
        importance=importance,
        category="other",
        metadata={"target_channel": target},
    )


def test_resolve_channel_demotes_low_importance_alert() -> None:
    # Grok theme が alert を提案しても enriched importance=low なら降格 (誤タグ防御)
    assert _resolve_channel(_bm("alert", "low"), _IMP_MAP) == "watch"


def test_resolve_channel_keeps_genuine_high_alert() -> None:
    # 本物の high signal は alert に残す
    assert _resolve_channel(_bm("alert", "high"), _IMP_MAP) == "alert"
    assert _resolve_channel(_bm("alert", "medium"), _IMP_MAP) == "alert"


def test_resolve_channel_only_gates_alert_not_other_channels() -> None:
    # alert 以外は low でも theme の意図を保持 (J1→japan_watch を壊さない)
    assert _resolve_channel(_bm("japan_watch", "low"), _IMP_MAP) == "japan_watch"
    assert _resolve_channel(_bm("watch", "low"), _IMP_MAP) == "watch"


# ───────── R-D: intra-batch 意味的 dedup ─────────
class _FakeEmbedder(EmbeddingClient):
    """title の marker で固定ベクトルを返す fake (ALPHA 同士は cosine 1.0)。"""

    @property
    def dim(self) -> int | None:
        return 4

    @property
    def model(self) -> str:
        return "test-embed"

    async def embed(self, text: str, *, kind: str = "document") -> EmbeddingResponse:
        if "ALPHA" in text:
            vec = (1.0, 0.0, 0.0, 0.0)
        elif "BETA" in text:
            vec = (0.0, 1.0, 0.0, 0.0)
        else:
            vec = (0.0, 0.0, 1.0, 0.0)
        return EmbeddingResponse(vector=vec, model="test-embed", dim=4)


def _art(aid: str, title: str, url: str) -> Article:
    return Article(
        id=aid,
        title=title,
        url=url,
        summary_html="<p>body</p>",
        author="A",
        published=datetime(2026, 6, 16, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


async def test_intra_batch_semantic_dedup(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # 永続ストアは空 (find_similar_embedding=None) → intra-batch 経路のみを検証。
    repo = RunHistoryRepository(db_path=tmp_path / "intrabatch.db")
    arts = [
        _art("a1", "ALPHA 事案 第一報", "https://x.example/1"),
        _art("a2", "ALPHA 事案 別ソース", "https://y.example/2"),  # 同事案・別 URL・同バッチ
        _art("b1", "BETA 無関係な事案", "https://z.example/3"),
    ]
    survivors, skipped, _persist, skipped_ids = await _filter_semantic_duplicates(
        arts,
        repo,
        _FakeEmbedder(),
        threshold_hard=0.92,
        threshold_cluster=0.82,
        window_hours_hard=168,
        window_hours_cluster=48,
    )
    ids = {a.id for a in survivors}
    assert "a1" in ids  # 最初の ALPHA は生存
    assert "a2" not in ids  # 同バッチ内の同事案 → intra_batch dedup で除外
    assert "b1" in ids  # 別事案は生存
    assert "a2" in skipped_ids
    assert skipped == 1


async def test_skipped_article_keeps_its_embedding(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """重複で落とした記事も判断根拠 (embedding) を返す (2026-08-19)。

    従来は survivor だけを ``embeddings_to_persist`` に入れていたため、14 日 239 件の
    skip 判定すべてで根拠が消えていた。**落とす判断こそ後から検証材料が要る** —
    「別の層なら捕まえられたか」「閾値変更で新たに落ちた記事は妥当か」を測れない。
    """
    repo = RunHistoryRepository(db_path=tmp_path / "keep_evidence.db")
    arts = [
        _art("a1", "ALPHA 事案 第一報", "https://x.example/1"),
        _art("a2", "ALPHA 事案 別ソース", "https://y.example/2"),  # intra-batch で落ちる
    ]

    survivors, skipped, persist, skipped_ids = await _filter_semantic_duplicates(
        arts,
        repo,
        _FakeEmbedder(),
        threshold_hard=0.92,
        threshold_cluster=0.79,
        window_hours_hard=168,
        window_hours_cluster=48,
    )

    assert skipped_ids == ["a2"]
    assert {a.id for a in survivors} == {"a1"}
    assert "a2" in persist, "落とした記事の embedding が返っていない (根拠が消える)"
    assert "a1" in persist

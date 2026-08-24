"""事象ニュース層の import 境界関門 (allowlist / default-deny)。

設計 SSoT: docs/event_news_design.md §2。生成物のパイプライン還流
(triage / routing / synthesis 証拠 / 検索 / digest / spotlight / forecast) を
denylist の数え漏れごと防ぐため、src/ 内で src/eventnews を import してよい場所を
allowlist で固定する。新しい消費者 (UI API 等) を作るときはここに 1 行足すこと —
それが「公開面に露出する変更である」ことを明示する儀式になる。
"""

from __future__ import annotations

import re
from pathlib import Path

# src/ 内で eventnews を import してよいモジュール (repo-relative)。
# 許可された消費者。**default-deny** — ここに無いモジュールが import すると落ちる。
# 増やすときは「生成物がどこへ流れるか」を意識して 1 行足すこと (それがこの関門の目的)。
#
# - repo_eventnews: storage の Mixin (ItemState 型と VERSION_CAP を読む infra。消費者ではない)
# - eventnews_hourly_job: 毎時ジョブ本体 (2026-08-24)。**生成するだけで、生成物を
#   他層へ渡さない** — 読み手向けの出口 (UI/Discord) は未配線で、追加時は別途 1 行要る
_ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/storage/repo_eventnews.py",
        "src/ui/services/eventnews_hourly_job.py",
        # 2026-08-24: 読み手向けの出口 (Tier0 = 匿名で閲覧可)。**生成物が公開面へ
        # 出る唯一の経路**なので、ここを増やすときは公開範囲の判断とセットで行う。
        "src/ui/api/eventnews.py",
    }
)

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+src\.eventnews\b", re.MULTILINE)


class TestEventNewsImportBoundary:
    def test_only_allowlisted_modules_import_eventnews(self) -> None:
        src_root = Path(__file__).resolve().parents[2] / "src"
        offenders: list[str] = []
        for path in src_root.rglob("*.py"):
            rel = path.relative_to(src_root.parent).as_posix()
            if rel.startswith("src/eventnews/"):
                continue
            if _IMPORT_RE.search(path.read_text(encoding="utf-8")) and rel not in _ALLOWLIST:
                offenders.append(rel)
        assert offenders == [], (
            f"eventnews を import する未許可モジュール: {offenders} — "
            "消費者を増やすなら test_eventnews_boundary._ALLOWLIST に明示追加すること"
        )

    def test_eventnews_does_not_import_judgment_layers(self) -> None:
        """逆向きも固定: eventnews が triage/routing/synthesis を読まない (§2 一方向)。"""
        pkg = Path(__file__).resolve().parents[2] / "src" / "eventnews"
        banned = re.compile(
            r"^\s*(?:from|import)\s+src\.(?:tools\.article_triage|synthesis|pir\.|forecast)",
            re.MULTILINE,
        )
        offenders = [
            p.name for p in pkg.rglob("*.py") if banned.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"eventnews から判断層への依存を検出: {offenders}"


# --- メタデータ集約の回帰 ---------------------------------------------------
#
# 2026-08-24: `facet_counts.setdefault(k, {})[v] = facet_counts[k].get(v, 0) + 1`
# と 1 行で書いたため、**右辺が setdefault より先に評価されて** KeyError になり、
# 詳細 API が 500 を返した。単体テストでは書式の正しさしか見ておらず、実物を
# 叩くまで気付けなかった。ここで集約そのものを固定する。


class _FakeRepo:
    def __init__(self, entities: dict[str, dict[str, int]], articles: dict[str, object]) -> None:
        self._entities = entities
        self._articles = articles

    def count_entities_for_articles(self, ids: list[str]) -> dict[str, dict[str, int]]:
        return self._entities

    def get_articles_by_ids(self, ids: list[str]) -> dict[str, object]:
        return self._articles


class _FakeArticle:
    def __init__(self, **kw: object) -> None:
        self.subject_actor_ids = kw.get("subject_actor_ids")
        self.victim_sector_canonical = kw.get("victim_sector_canonical")
        self.victim_country_iso = kw.get("victim_country_iso")
        self.socio_political_intent = kw.get("socio_political_intent")
        self.category = kw.get("category")


def test_metadata_aggregates_facets_across_members() -> None:
    """複数記事に跨る facet を件数付きで集計する。"""
    # Arrange
    from typing import cast

    from src.storage.run_history import RunHistoryRepository
    from src.ui.api.eventnews import _metadata_payload

    repo = _FakeRepo(
        {"cve": {"CVE-2026-1": 2, "CVE-2026-2": 1}},
        {
            "a1": _FakeArticle(victim_sector_canonical="government", category="apt"),
            "a2": _FakeArticle(victim_sector_canonical="government", category="vuln"),
        },
    )

    # Act
    out = _metadata_payload(cast(RunHistoryRepository, repo), ["a1", "a2"])

    # Assert
    sector = next(f for f in out["facets"] if f["key"] == "victim_sector")
    assert sector["values"] == [{"value": "government", "articles": 2}]
    cve = next(g for g in out["entities"] if g["type"] == "cve")
    assert cve["values"][0] == {"value": "CVE-2026-1", "articles": 2}


def test_metadata_is_empty_without_members() -> None:
    from typing import cast

    from src.storage.run_history import RunHistoryRepository
    from src.ui.api.eventnews import _metadata_payload

    out = _metadata_payload(cast(RunHistoryRepository, _FakeRepo({}, {})), [])
    assert out == {"entities": [], "subject_actors": [], "facets": []}

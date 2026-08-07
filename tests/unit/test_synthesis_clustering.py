"""src.synthesis.grounded.clustering のテスト (各 claim の裏取りをプールから決定論拡張)。"""

from __future__ import annotations

from src.synthesis.grounded.clustering import expand_claim_sources


def _pool(*ids_titles: tuple[str, str]) -> list[dict[str, object]]:
    return [{"article_id": aid, "title": title} for aid, title in ids_titles]


class TestExpandClaimSources:
    def test_shared_cve_entity_clusters(self) -> None:
        # 同一 CVE を共有する記事は同一事案として裏取りに加わる
        pool = _pool(("a1", "Ivanti flaw exploited"), ("a2", "More on Ivanti"), ("a3", "無関係"))
        ents = {"a1": {"cve:CVE-2026-1"}, "a2": {"cve:CVE-2026-1"}, "a3": {"cve:CVE-2099-9"}}
        out = expand_claim_sources(
            claim_text="Ivanti の脆弱性が悪用",
            seed_ids=("a1",),
            pool=pool,
            entities_by_id=ents,
            max_sources=8,
        )
        assert out[0] == "a1"  # seed 優先
        assert "a2" in out  # 同一 CVE で採用
        assert "a3" not in out  # 別 CVE・タイトル無関係は除外

    def test_title_token_overlap_for_entity_poor_claim(self) -> None:
        # entity の乏しい地政学 claim はタイトル重なりでクラスタ
        pool = _pool(
            ("g1", "ロシアと中国が共同爆撃機パトロールを実施"),
            ("g2", "共同爆撃機パトロールに日本が警戒"),
            ("g3", "Chrome のアップデート"),
        )
        out = expand_claim_sources(
            claim_text="ロシアと中国が共同爆撃機パトロール",
            seed_ids=("g1",),
            pool=pool,
            entities_by_id={},
            max_sources=8,
        )
        assert "g2" in out  # 「共同」「爆撃機」「パトロール」重なり
        assert "g3" not in out

    def test_generic_tokens_do_not_overcluster(self) -> None:
        # "攻撃"/"サイバー" 等の汎用語だけでは引き込まない (stop 語)
        pool = _pool(("x1", "A社へのサイバー攻撃"), ("x2", "B社へのサイバー攻撃"))
        out = expand_claim_sources(
            claim_text="A社へのサイバー攻撃",
            seed_ids=("x1",),
            pool=pool,
            entities_by_id={},
            max_sources=8,
        )
        assert out == ["x1"]  # 汎用語のみ重複 → x2 は採用しない

    def test_cap_limits_total(self) -> None:
        pool = _pool(*[(f"a{i}", f"Ivanti flaw part {i}") for i in range(20)])
        ents = {f"a{i}": {"cve:CVE-2026-1"} for i in range(20)}
        out = expand_claim_sources(
            claim_text="Ivanti flaw",
            seed_ids=("a0",),
            pool=pool,
            entities_by_id=ents,
            max_sources=8,
        )
        assert len(out) == 8
        assert out[0] == "a0"  # seed は必ず含む

    def test_seed_only_when_no_matches(self) -> None:
        pool = _pool(("a1", "topic one"), ("a2", "完全に別の話題"))
        out = expand_claim_sources(
            claim_text="topic one",
            seed_ids=("a1",),
            pool=pool,
            entities_by_id={},
            max_sources=8,
        )
        assert out == ["a1"]

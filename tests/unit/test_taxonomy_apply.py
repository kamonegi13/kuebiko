"""taxonomy 提案の承認時適用 (src/taxonomy/apply.py) のテスト。

2026-07-31 運用レビュー完全調査の本質修正: 「採用」が status flip のみで
提案内容を適用しない欠陥 (UAT-11795 が承認済みのまま辞書未登録) の再発防止。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from src.storage.records import TaxonomyProposalRecord
from src.taxonomy.apply import (
    TaxonomyApplyError,
    add_sector_alias_text,
    add_sector_canonical_text,
    apply_taxonomy_proposal,
)

SECTORS_TEXT = """\
# header コメントは保持されること
canonical:
  financial:
    display: 金融
    aliases:
      - 金融
      - finance

  technology:
    display: IT
    aliases:
      - technology
      - IT

  other:
    display: その他
    aliases: []
"""


def _lookup(text: str) -> dict[str, str]:
    data = yaml.safe_load(text)
    out: dict[str, str] = {}
    for cid, body in data["canonical"].items():
        out[cid.lower()] = cid
        for a in (body or {}).get("aliases") or []:
            out[str(a).lower()] = cid
    return out


class TestAddSectorAlias:
    def test_inserts_and_resolves(self) -> None:
        new = add_sector_alias_text(SECTORS_TEXT, alias="fintech", canonical="financial")
        assert _lookup(new)["fintech"] == "financial"
        # コメント・既存 alias は保持
        assert "# header コメントは保持されること" in new
        assert _lookup(new)["finance"] == "financial"

    def test_idempotent_when_already_resolves(self) -> None:
        assert add_sector_alias_text(SECTORS_TEXT, alias="finance", canonical="financial") == (
            SECTORS_TEXT
        )

    def test_conflict_with_other_canonical(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="既に canonical"):
            add_sector_alias_text(SECTORS_TEXT, alias="IT", canonical="financial")

    def test_unknown_canonical_rejected(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="ありません"):
            add_sector_alias_text(SECTORS_TEXT, alias="x", canonical="nope")

    def test_inline_empty_aliases_expanded(self) -> None:
        new = add_sector_alias_text(SECTORS_TEXT, alias="misc", canonical="other")
        assert _lookup(new)["misc"] == "other"

    def test_special_chars_quoted(self) -> None:
        new = add_sector_alias_text(
            SECTORS_TEXT, alias="AI: infrastructure", canonical="technology"
        )
        assert _lookup(new)["ai: infrastructure"] == "technology"

    def test_empty_alias_rejected(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="空"):
            add_sector_alias_text(SECTORS_TEXT, alias="  ", canonical="financial")


class TestAddSectorCanonical:
    def test_appends_and_resolves(self) -> None:
        new = add_sector_canonical_text(
            SECTORS_TEXT,
            canonical_id="ai_infrastructure",
            display="AI インフラ",
            aliases=["AI infrastructure", "AI/ML Infrastructure"],
        )
        lk = _lookup(new)
        assert lk["ai_infrastructure"] == "ai_infrastructure"
        assert lk["ai infrastructure"] == "ai_infrastructure"
        assert lk["ai/ml infrastructure"] == "ai_infrastructure"

    def test_duplicate_id_rejected(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="既に存在"):
            add_sector_canonical_text(
                SECTORS_TEXT, canonical_id="financial", display="x", aliases=[]
            )

    def test_alias_owned_elsewhere_rejected(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="既に canonical"):
            add_sector_canonical_text(
                SECTORS_TEXT, canonical_id="newcat", display="x", aliases=["finance"]
            )

    def test_invalid_id_rejected(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="不正"):
            add_sector_canonical_text(SECTORS_TEXT, canonical_id="交通", display="交通", aliases=[])

    def test_empty_aliases_ok(self) -> None:
        new = add_sector_canonical_text(
            SECTORS_TEXT, canonical_id="newcat", display="新分野", aliases=[]
        )
        assert _lookup(new)["newcat"] == "newcat"


def _proposal(
    change: dict[str, Any], *, target_yaml: str = "victim_sectors"
) -> TaxonomyProposalRecord:
    return TaxonomyProposalRecord(
        proposal_type="pattern_4",
        tier="tier_1_auto",
        target_yaml=target_yaml,
        proposed_change=json.dumps(change, ensure_ascii=False),
        rationale="test",
        confidence="high",
    )


ACTORS_DATA: dict[str, Any] = {
    "actors": [
        {"id": "apt28", "canonical": "APT28", "aliases": ["Fancy Bear"], "kind": "group"},
    ]
}


class TestApplyTaxonomyProposal:
    def _run(
        self,
        change: dict[str, Any],
        *,
        sectors: str = SECTORS_TEXT,
        actors: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        writes: dict[str, str] = {}
        result = apply_taxonomy_proposal(
            _proposal(change),
            write_yaml=lambda path, content: writes.__setitem__(path, content),
            sectors_text_loader=lambda: sectors,
            actors_data_loader=lambda: dict(actors or ACTORS_DATA),
        )
        return result, writes

    def test_add_alias_writes_sectors_yaml(self) -> None:
        result, writes = self._run(
            {"kind": "add_alias", "alias": "gaming", "canonical": "technology"}
        )
        assert result["applied"] == "yaml_updated"
        assert _lookup(writes["config/victim_sectors.yaml"])["gaming"] == "technology"

    def test_add_alias_already_known_no_write(self) -> None:
        result, writes = self._run(
            {"kind": "add_alias", "alias": "finance", "canonical": "financial"}
        )
        assert result["applied"] == "already_known"
        assert writes == {}

    def test_new_canonical_writes_sectors_yaml(self) -> None:
        result, writes = self._run(
            {
                "kind": "new_canonical",
                "suggested_id": "aviation",
                "display": "航空",
                "aliases": ["aviation", "航空"],
            }
        )
        assert result["applied"] == "yaml_updated"
        assert _lookup(writes["config/victim_sectors.yaml"])["aviation"] == "aviation"

    def test_new_actor_appends_to_dictionary(self) -> None:
        result, writes = self._run(
            {
                "kind": "new_actor",
                "suggested_id": "uat_11795",
                "canonical": "UAT-11795",
                "aliases": ["UAT-11795"],
            }
        )
        assert result == {
            "applied": "yaml_updated",
            "actor_id": "uat_11795",
            "target_yaml": "actor_aliases",
        }
        written = yaml.safe_load(writes["config/actor_aliases.yaml"])
        added = next(a for a in written["actors"] if a["id"] == "uat_11795")
        assert added["canonical"] == "UAT-11795"
        # canonical と同名 alias は重複させない
        assert added["aliases"] == []

    def test_new_actor_generic_word_forced_ambiguous(self) -> None:
        result, writes = self._run(
            {"kind": "new_actor", "suggested_id": "cloud", "canonical": "Cloud", "aliases": []}
        )
        assert result["applied"] == "yaml_updated"
        written = yaml.safe_load(writes["config/actor_aliases.yaml"])
        added = next(a for a in written["actors"] if a["id"] == "cloud")
        assert added["ambiguous"] is True

    def test_new_actor_already_known_is_idempotent(self) -> None:
        result, writes = self._run(
            {"kind": "new_actor", "suggested_id": "apt28x", "canonical": "APT28", "aliases": []}
        )
        assert result["applied"] == "already_known"
        assert writes == {}

    def test_new_actor_alias_collision_rejected(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="衝突"):
            self._run(
                {
                    "kind": "new_actor",
                    "suggested_id": "bear2",
                    "canonical": "Bear Two",
                    "aliases": ["Fancy Bear"],
                }
            )

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(TaxonomyApplyError, match="未知の変更種別"):
            self._run({"kind": "mystery"})

    def test_broken_json_rejected(self) -> None:
        record = TaxonomyProposalRecord(
            proposal_type="pattern_4",
            tier="tier_1_auto",
            target_yaml="victim_sectors",
            proposed_change="{broken",
            rationale="test",
            confidence="high",
        )
        with pytest.raises(TaxonomyApplyError, match="壊れて"):
            apply_taxonomy_proposal(
                record,
                write_yaml=lambda _p, _c: None,
                sectors_text_loader=lambda: SECTORS_TEXT,
                actors_data_loader=lambda: dict(ACTORS_DATA),
            )

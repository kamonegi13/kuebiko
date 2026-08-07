"""actor_editor (Actor 辞書 UI) のテスト — alias 重複検証が核心。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.cti.actor_editor import (
    append_new_actor,
    apply_actor_edit,
    list_actors,
    load_actors_raw,
    render_actors_yaml,
    validate_actor_edit,
)

_SAMPLE = """\
families:
  typhoon: { nation: cn, label: Typhoon 群 }
actors:
  - id: volt_typhoon
    canonical: Volt Typhoon
    aliases: [Vanguard Panda, BRONZE SILHOUETTE]
    mitre_group: G1017
    nation: cn
    sponsor: PRC State-Sponsored
    family: typhoon
  - id: lazarus
    canonical: Lazarus Group
    aliases: [Hidden Cobra, APT38]
    nation: kp
"""


@pytest.fixture
def path(tmp_path: Path) -> Path:
    p = tmp_path / "actor_aliases.yaml"
    p.write_text(_SAMPLE, encoding="utf-8")
    return p


def test_list_actors_returns_editable_fields(path: Path) -> None:
    rows = list_actors(path)
    assert [r["id"] for r in rows] == ["volt_typhoon", "lazarus"]
    volt = rows[0]
    assert volt["canonical"] == "Volt Typhoon"
    assert volt["aliases"] == ["Vanguard Panda", "BRONZE SILHOUETTE"]
    assert volt["sponsor"] == "PRC State-Sponsored"


def test_validate_rejects_empty_canonical(path: Path) -> None:
    data = load_actors_raw(path)
    assert validate_actor_edit(data, "volt_typhoon", "  ", []) is not None


def test_validate_rejects_duplicate_alias_with_other_actor(path: Path) -> None:
    """別 actor (Lazarus) の別名 'APT38' を volt に付けると誤帰属 → 拒否。"""
    data = load_actors_raw(path)
    err = validate_actor_edit(data, "volt_typhoon", "Volt Typhoon", ["APT38"])
    assert err is not None
    assert "APT38" in err and "Lazarus" in err


def test_validate_allows_own_names(path: Path) -> None:
    data = load_actors_raw(path)
    # 自身の既存 alias を保持するのは OK
    assert validate_actor_edit(data, "volt_typhoon", "Volt Typhoon", ["Vanguard Panda"]) is None


def test_validate_rejects_normalized_id_canonical_collision(path: Path) -> None:
    """id/canonical の正規化キー (記号除去 + casefold) 衝突は同一実体の綴り違い → 拒否。

    2026-08-01 の thegentlemen 事故: 既存 `the_gentlemen` (canonical "The Gentlemen")
    に対し綴り違い `thegentlemen` が採用経路の完全一致照合を素通りし id 分裂
    (102 記事二重付与) を起こした。読取層 (resolve_source_slug) は正規化解決するのに
    書込層が生文字列比較だったのが真因。
    """
    data = load_actors_raw(path)
    err = validate_actor_edit(data, "volttyphoon", "Volttyphoon", [])
    assert err is not None
    assert "volt_typhoon" in err


def test_validate_allows_distinct_normalized_keys(path: Path) -> None:
    data = load_actors_raw(path)
    assert validate_actor_edit(data, "brand_new", "Brand New Group", []) is None


def test_append_rejects_normalized_id_duplicate(path: Path) -> None:
    """append_new_actor の id 重複検査も正規化キーで照合する (最終防衛線)。"""
    data = load_actors_raw(path)
    with pytest.raises(ValueError, match="volt_typhoon"):
        append_new_actor(data, {"id": "volttyphoon", "canonical": "X", "aliases": []})


def test_apply_edit_normalizes_and_preserves(path: Path) -> None:
    data = load_actors_raw(path)
    new = apply_actor_edit(
        data,
        "volt_typhoon",
        {"canonical": "Volt Typhoon", "aliases": ["  Vanguard Panda  ", ""], "mitre_group": ""},
    )
    volt = next(a for a in new["actors"] if a["id"] == "volt_typhoon")
    assert volt["aliases"] == ["Vanguard Panda"]  # trim + 空除去
    assert volt["mitre_group"] is None  # 空 → None
    assert volt["sponsor"] == "PRC State-Sponsored"  # 未編集 field は保持
    # families も保持
    assert "typhoon" in new["families"]


def test_apply_edit_missing_actor_raises(path: Path) -> None:
    data = load_actors_raw(path)
    with pytest.raises(KeyError):
        apply_actor_edit(data, "nonexistent", {"canonical": "X"})


def test_render_roundtrips(path: Path, tmp_path: Path) -> None:
    data = load_actors_raw(path)
    out = tmp_path / "out.yaml"
    out.write_text(render_actors_yaml(data), encoding="utf-8")
    back = load_actors_raw(out)
    assert [a["id"] for a in back["actors"]] == ["volt_typhoon", "lazarus"]
    assert back["families"]["typhoon"]["nation"] == "cn"
    # header コメント保持
    assert render_actors_yaml(data).lstrip().startswith("#")


def test_detail_fields_roundtrip(path: Path, tmp_path: Path) -> None:
    """Stage 1: reference 用詳細 field (summary/malware/references 等) の編集 + 保持。"""
    data = load_actors_raw(path)
    new = apply_actor_edit(
        data,
        "lazarus",
        {
            "canonical": "Lazarus Group",
            "summary": "DPRK 偵察総局系の金融・暗号資産狙い APT",
            "motivation": "financial",
            "first_seen": "2009",
            "associated_malware": ["AppleJeus", "  ", "Manuscrypt"],
            "references": ["https://attack.mitre.org/groups/G0032"],
        },
    )
    out = tmp_path / "o.yaml"
    out.write_text(render_actors_yaml(new), encoding="utf-8")
    row = next(r for r in list_actors(out) if r["id"] == "lazarus")
    assert row["summary"].startswith("DPRK")
    assert row["motivation"] == "financial"
    assert row["associated_malware"] == ["AppleJeus", "Manuscrypt"]  # 空除去
    assert row["references"] == ["https://attack.mitre.org/groups/G0032"]
    # 既存 field は保持
    assert row["nation"] == "kp"


class TestAliasConflictStaleGuard:
    """承認時の陳腐化ガード (2026-07-16: バグ期生成の stale 提案が辞書を汚染した実害の回帰)。

    ガード本体は pages.actors_sync_approve 内の owner 突合。ここではその判定素材
    (辞書の mitre_group 所有者の導出) と move_alias が前提とする不変条件を固定する。
    """

    def test_mitre_group_owner_lookup(self) -> None:
        data = {
            "families": {},
            "actors": [
                {"id": "apt35", "canonical": "APT35", "mitre_group": "G0059", "aliases": ["TA453"]},
                {"id": "lamberts", "canonical": "The Lamberts", "aliases": ["Longhorn"]},
            ],
        }
        owner = next(
            (
                str(a.get("id", ""))
                for a in data.get("actors", [])
                if isinstance(a, dict) and str(a.get("mitre_group", "")) == "G0059"
            ),
            "",
        )
        # 辞書は G0059=apt35 — 「G0059=lamberts」を前提とする提案は矛盾として弾ける
        assert owner == "apt35"
        assert owner != "lamberts"


def test_validate_ignores_merged_tombstone(path: Path) -> None:
    """merged 墓標の canonical は継承先の alias と同名で正 — 衝突検証から除外。"""
    data = load_actors_raw(path)
    data["actors"].append(
        {
            "id": "old_cobra",
            "canonical": "Hidden Cobra",  # lazarus の alias と同名 (merge の正常形)
            "aliases": [],
            "status": "merged",
            "merged_into": "lazarus",
        }
    )
    # active な lazarus 側から見ても、墓標側の名前は衝突として扱わない
    assert validate_actor_edit(data, "lazarus", "Lazarus Group", ["Hidden Cobra", "APT38"]) is None


def test_validate_generic_name_requires_ambiguous(path: Path) -> None:
    """一般語 alias (元素名等) は ambiguous=true でなければ保存拒否 (07-21 再発の UI 側防御)。"""
    data = load_actors_raw(path)
    err = validate_actor_edit(data, "lazarus", "Lazarus Group", ["Zinc"], ambiguous=False)
    assert err is not None
    assert "Zinc" in err
    # ambiguous=true なら許容 (Tick 方式)
    assert validate_actor_edit(data, "lazarus", "Lazarus Group", ["Zinc"], ambiguous=True) is None


def test_apply_edit_ambiguous_and_cues_roundtrip(path: Path) -> None:
    data = load_actors_raw(path)
    new_data = apply_actor_edit(
        data,
        "lazarus",
        {"ambiguous": True, "context_cues": ["hacktivist", " ddos "]},
    )
    actor = next(a for a in new_data["actors"] if a["id"] == "lazarus")
    assert actor["ambiguous"] is True
    assert actor["context_cues"] == ["hacktivist", "ddos"]
    rows = list_actors(path)
    # list_actors は bool を正規化して返す (未指定は False)
    assert rows[0]["ambiguous"] is False

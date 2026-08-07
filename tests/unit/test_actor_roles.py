"""アクター役割判定 (報告/防御機関の除外) の単体テスト (2026-07-27、D1)。"""

from __future__ import annotations

from src.cti.actor_roles import reporter_org_actor_ids


def test_allied_reporter_orgs_are_flagged() -> None:
    """同盟国の state_organ (NSA / CyberCommand 等) は報告機関として除外対象になる。"""
    ids = reporter_org_actor_ids()
    assert "us_nsa" in ids
    assert "us_cyber_command" in ids
    assert "israel_mossad" in ids


def test_adversary_state_organs_are_not_excluded() -> None:
    """敵性国家の state_organ (GRU/MSS/RGB 等) は正当な攻撃主体 = 除外しない。"""
    ids = reporter_org_actor_ids()
    assert "russia_gru" not in ids
    assert "prc_mss" not in ids
    assert "dprk_rgb" not in ids


def test_ransom_groups_are_not_excluded() -> None:
    """攻撃グループ (ransom_group 等) は除外しない (family != state_organ)。"""
    ids = reporter_org_actor_ids()
    assert "lockbit" not in ids

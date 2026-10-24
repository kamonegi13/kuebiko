"""被害組織名の別名判定のキャッシュ (org_identity_pairs)。"""

from __future__ import annotations

from pathlib import Path

from src.storage.run_history import RunHistoryRepository


def test_saved_logit_is_read_back(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.put_org_identity_logits({("aa", "bb"): 13.5})

    got = repo.get_org_identity_logits([("aa", "bb"), ("aa", "cc")])

    assert got == {("aa", "bb"): 13.5}


def test_same_pair_is_overwritten(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.put_org_identity_logits({("aa", "bb"): -3.0})
    repo.put_org_identity_logits({("aa", "bb"): 9.0})

    assert repo.get_org_identity_logits([("aa", "bb")]) == {("aa", "bb"): 9.0}


def test_other_form_does_not_see_the_judgment(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.put_org_identity_logits({("aa", "bb"): 9.0}, form="old")

    assert repo.get_org_identity_logits([("aa", "bb")]) == {}


def test_empty_input_is_noop(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    assert repo.get_org_identity_logits([]) == {}
    assert repo.put_org_identity_logits({}) == 0


def test_same_pairs_lists_only_non_negative_margin_of_current_form(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.put_org_identity_logits({("aa", "bb"): 2.0, ("cc", "dd"): -5.0, ("ee", "ff"): 0.0})
    repo.put_org_identity_logits({("gg", "hh"): 9.0}, form="old")

    assert sorted(repo.list_org_identity_same_pairs()) == [("aa", "bb"), ("ee", "ff")]

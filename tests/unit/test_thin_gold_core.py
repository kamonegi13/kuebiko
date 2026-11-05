from scripts.replay.thin_gold_core import (
    allocate_targets,
    apply_hub_cut,
    band_of,
    band_weights,
    draw_sample,
    gate_status_of,
    split_contaminated,
)


def test_band_of_boundaries() -> None:
    assert band_of(0) is None
    assert band_of(1) == "lt300"
    assert band_of(299) == "lt300"
    assert band_of(300) == "300_450"
    assert band_of(449) == "300_450"
    assert band_of(450) == "450_600"
    assert band_of(599) == "450_600"
    assert band_of(600) is None


def test_gate_status() -> None:
    assert gate_status_of("posted", None) == "posted"
    assert gate_status_of("failed", "degenerate_body: thin_body_probe") == "probe_rejected"
    assert gate_status_of("failed", "other") is None


def test_allocate_full() -> None:
    a = {"lt300": 80, "300_450": 90, "450_600": 100}
    assert allocate_targets(a) == {"lt300": 50, "300_450": 50, "450_600": 50}


def test_allocate_low_shortage_goes_to_mid() -> None:
    a = {"lt300": 40, "300_450": 90, "450_600": 100}
    assert allocate_targets(a) == {"lt300": 40, "300_450": 60, "450_600": 50}


def test_allocate_cascade_to_high() -> None:
    a = {"lt300": 40, "300_450": 55, "450_600": 100}
    assert allocate_targets(a) == {"lt300": 40, "300_450": 55, "450_600": 55}


def test_allocate_high_shortage_goes_to_mid() -> None:
    a = {"lt300": 50, "300_450": 100, "450_600": 30}
    assert allocate_targets(a) == {"lt300": 50, "300_450": 70, "450_600": 30}


def test_allocate_never_exceeds_available() -> None:
    a = {"lt300": 5, "300_450": 10, "450_600": 10}
    out = allocate_targets(a)
    assert out == {"lt300": 5, "300_450": 10, "450_600": 10}


def test_draw_deterministic_and_order_independent() -> None:
    ids = {b: [f"{b}-{i:03d}" for i in range(80)] for b in ("lt300", "300_450", "450_600")}
    t = {"lt300": 50, "300_450": 50, "450_600": 50}
    first = draw_sample(ids, t)
    rev = draw_sample({b: list(reversed(v)) for b, v in ids.items()}, t)
    assert first == rev
    assert all(len(set(v)) == 50 for v in first.values())
    assert first != draw_sample(ids, t, seed=1)


def test_weights() -> None:
    w = band_weights(
        {"lt300": 40, "300_450": 100, "450_600": 150}, {"lt300": 40, "300_450": 50, "450_600": 50}
    )
    assert w == {"lt300": 1.0, "300_450": 2.0, "450_600": 3.0}


def test_split_contaminated_drops_whole_component() -> None:
    comp = {"a": "c1", "b": "c1", "c": "c2", "t": "c1"}
    res = split_contaminated(["a", "b", "c"], comp, frozenset({"t"}))
    assert res.excluded == frozenset({"a", "b"})
    assert res.kept == frozenset({"c"})


def test_hub_cut() -> None:
    v = {"a": frozenset({"1", "2", "3", "4"}), "b": frozenset({"1"}), "c": frozenset()}
    assert apply_hub_cut(v, 3) == {"b": frozenset({"1"})}

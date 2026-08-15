"""geocoder Phase 1a (国/地域 代表点) の unit test。

非API・オフライン。国名正規化 (taxonomy_normalizer) → ISO → 首都座標、地域重心、
精度 Tier の符号化を検証する。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.cti.geocoder import _COUNTRY_CENTROIDS, Geocoder, GeoPrecision


def _geo() -> Geocoder:
    return Geocoder()


def test_centroids_in_sync_with_countries_yaml() -> None:
    """drift-guard: countries.yaml の canonical ISO と _COUNTRY_CENTROIDS は 1:1。

    yaml に国を足して centroid を忘れると resolve できても plot 不能になる
    (逆も labels 不在で plot しても国名が出ない)。両者の集合一致を強制する。
    """
    raw = yaml.safe_load(Path("config/cti/countries.yaml").read_text(encoding="utf-8"))
    yaml_isos = set((raw or {}).get("canonical", {}).keys())
    centroid_isos = set(_COUNTRY_CENTROIDS.keys())
    missing_centroid = sorted(yaml_isos - centroid_isos)
    missing_yaml = sorted(centroid_isos - yaml_isos)
    assert not missing_centroid, f"countries.yaml にあるが centroid 欠落: {missing_centroid}"
    assert not missing_yaml, f"centroid にあるが countries.yaml 欠落: {missing_yaml}"


def test_centroids_have_valid_lat_lon() -> None:
    for iso, (lat, lon) in _COUNTRY_CENTROIDS.items():
        assert -90.0 <= lat <= 90.0, f"{iso} lat 範囲外: {lat}"
        assert -180.0 <= lon <= 180.0, f"{iso} lon 範囲外: {lon}"


def test_expanded_countries_resolve() -> None:
    """拡充した国が日本語/英語の両方で解決する (回帰の固定点)。"""
    g = _geo()
    cases = {"イタリア": "IT", "Italy": "IT", "レバノン": "LB", "Singapore": "SG", "香港": "HK"}
    for name, iso in cases.items():
        p = g.resolve(name, kind="country")
        assert p is not None and p.matched_name == iso, f"{name} → {iso}"


def test_country_iso_to_centroid() -> None:
    g = _geo()
    jp = g.country("JP")
    assert jp is not None
    assert round(jp.lat) == 36 and round(jp.lon) == 140  # 東京近傍
    assert jp.precision == GeoPrecision.COUNTRY
    assert jp.source == "centroid"


def test_country_unknown_iso_returns_none() -> None:
    assert _geo().country("ZZ") is None
    assert _geo().country(None) is None
    assert _geo().country("") is None


def test_resolve_country_name_via_normalizer() -> None:
    g = _geo()
    # 日本語/英語/ISO いずれも JP centroid に解決
    for name in ("日本", "Japan", "JP"):
        p = g.resolve(name, kind="country")
        assert p is not None and p.matched_name == "JP", name
        assert p.precision == GeoPrecision.COUNTRY


def test_region_centroid() -> None:
    g = _geo()
    ea = g.region("east_asia")
    assert ea is not None and ea.precision == GeoPrecision.REGION
    assert g.region("europe") is not None
    assert g.region("nowhere") is None


def test_resolve_auto_falls_back_to_country_for_country_name() -> None:
    g = _geo()
    # 地名が国名なら国レベルで返る (city tier 未取込のため)
    p = g.resolve("ロシア", kind="auto")
    assert p is not None and p.matched_name == "RU"


def test_resolve_city_or_org_unresolved_in_phase1a() -> None:
    g = _geo()
    # 都市/組織は DB 未取込 (Phase 1b/1c) のため、国名でなければ None
    assert g.resolve("架空の小さな会社XYZ", kind="org") is None
    assert g.resolve("Shibuya", kind="city") is None  # 都市は GeoNames 取込後


def test_resolve_empty() -> None:
    g = _geo()
    assert g.resolve(None) is None
    assert g.resolve("   ") is None


# ── 3D-a: geo_cities gazetteer による都市解決 ──


def _seeded_cities_con() -> object:
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE geo_cities (country_code TEXT, name_lower TEXT, lat DOUBLE PRECISION, "
        "lon DOUBLE PRECISION, population BIGINT, PRIMARY KEY(country_code,name_lower))"
    )
    con.executemany(
        "INSERT INTO geo_cities VALUES (?,?,?,?,?)",
        [
            ("JP", "osaka", 34.69, 135.5, 2753862),
            ("JP", "大阪", 34.69, 135.5, 2753862),
            ("DE", "munich", 48.14, 11.58, 1505005),
        ],
    )
    con.commit()
    return con


def test_city_resolves_from_gazetteer() -> None:
    g = _geo()
    con = _seeded_cities_con()
    for name in ("Osaka", "大阪", " osaka "):
        p = g.city(con, name, "JP")
        assert p is not None and p.precision == GeoPrecision.CITY, name
        assert round(p.lat) == 35 and round(p.lon) == 136
        assert p.source == "geonames"
    assert g.city(con, "Munich", "de") is not None  # 国 ISO は大小無視
    assert g.city(con, "架空都市", "JP") is None  # 未登録


def test_city_failsafe_inputs_and_missing_table() -> None:
    g = _geo()
    con = _seeded_cities_con()
    assert g.city(con, None, "JP") is None
    assert g.city(con, "Osaka", None) is None
    assert g.city(con, "  ", "JP") is None
    import sqlite3

    empty = sqlite3.connect(":memory:")  # geo_cities 不在 → fail-safe で None
    empty.row_factory = sqlite3.Row
    assert g.city(empty, "Osaka", "JP") is None


def test_org_resolves_and_failsafe() -> None:
    import sqlite3

    g = _geo()
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE geo_orgs (name_lower TEXT PRIMARY KEY, "
        "lat DOUBLE PRECISION, lon DOUBLE PRECISION)"
    )
    con.execute("INSERT INTO geo_orgs VALUES ('morgan stanley', 40.71, -74.01)")
    con.commit()
    p = g.org(con, "Morgan Stanley")
    assert p is not None and p.precision == GeoPrecision.ORG_HQ and p.source == "wikidata"
    assert g.org(con, "無名企業XYZ") is None
    assert g.org(con, None) is None
    empty = sqlite3.connect(":memory:")  # geo_orgs 不在 → fail-safe
    empty.row_factory = sqlite3.Row
    assert g.org(empty, "Morgan Stanley") is None

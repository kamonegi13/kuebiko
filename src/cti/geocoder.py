"""イベントの地理座標解決 (Phase 1a: 国 / 地域 の代表点)。

**非 API・オフライン gazetteer 方式**。お金・egress・rate limit・ToS 制約ゼロ。
- Phase 1a (本ファイル): 国の代表点 (首都座標) + 地域重心を curated 定数で解決。
  既存 victim_country / 地政事象を即「国/地域レベル」でプロットできる。
- Phase 1b/1c (後続): GeoNames (都市/行政区) と Wikidata 組織本社サブセットを
  DB gazetteer に取込み、city / org_hq / facility tier を ``_resolve_db`` 経由で追加。

設計原則 ([[geo_map_design_discussion]]): **精度を Tier で明示**し「点が分かる時だけ
鋭いピン、国しか分からなければ国レベル」と正直に符号化する (首都に鋭いピンを刺して
精度を捏造しない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.cti.taxonomy_normalizer import TaxonomyNormalizer, load_normalizer
from src.logging_config import get_logger

_log = get_logger(__name__)


class GeoPrecision(StrEnum):
    """プロット点の精度 Tier (地図で見た目を変える根拠)。"""

    FACILITY = "facility"  # 施設名一致 (最精密、鋭いピン)
    ORG_HQ = "org_hq"  # 組織本社 (代理点、"本社" と明示)
    CITY = "city"  # 都市
    COUNTRY = "country"  # 国 (国スケール事象 or 詳細不明 → 国バブル)
    REGION = "region"  # 地域 (地政事象等)


@dataclass(frozen=True)
class GeoPoint:
    """解決された地理座標 + 精度メタ。"""

    lat: float
    lon: float
    precision: GeoPrecision
    matched_name: str  # 何に一致したか (監査・表示用)
    source: str  # "centroid" / "geonames" / "wikidata" / "manual"


# 主要国の代表点 = 首都座標 (国スケール事象 / 詳細不明時の honest な点)。
# **config/cti/countries.yaml の canonical 全件と 1:1 で同期する** (drift は
# tests/unit/test_geocoder.py の guard で検知)。GeoNames 取込後も国レベルはこの定数を正と
# する (決定的・再現可能)。IL は係争のため代表点として用いる。
_COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {
    # 東アジア
    "JP": (35.68, 139.69),  # 東京
    "CN": (39.90, 116.40),  # 北京
    "TW": (25.03, 121.57),  # 台北
    "KR": (37.57, 126.98),  # ソウル
    "KP": (39.02, 125.75),  # 平壌
    "HK": (22.32, 114.17),  # 香港
    "MN": (47.92, 106.92),  # ウランバートル
    # 東南アジア
    "VN": (21.03, 105.85),  # ハノイ
    "PH": (14.60, 120.98),  # マニラ
    "TH": (13.75, 100.50),  # バンコク
    "ID": (-6.21, 106.85),  # ジャカルタ
    "MY": (3.14, 101.69),  # クアラルンプール
    "SG": (1.35, 103.82),  # シンガポール
    "KH": (11.56, 104.92),  # プノンペン
    "MM": (19.75, 96.10),  # ネピドー
    "LA": (17.97, 102.60),  # ビエンチャン
    # 南アジア
    "IN": (28.61, 77.21),  # ニューデリー
    "PK": (33.69, 73.06),  # イスラマバード
    "BD": (23.81, 90.41),  # ダッカ
    "LK": (6.93, 79.86),  # コロンボ
    "NP": (27.72, 85.32),  # カトマンズ
    "AF": (34.53, 69.17),  # カブール
    # 中央アジア
    "KZ": (51.17, 71.45),  # アスタナ
    "KG": (42.87, 74.59),  # ビシュケク
    "UZ": (41.31, 69.24),  # タシュケント
    "TJ": (38.56, 68.79),  # ドゥシャンベ
    "TM": (37.95, 58.38),  # アシガバート
    # 中東
    "IR": (35.69, 51.39),  # テヘラン
    "IL": (31.78, 35.22),  # エルサレム
    "SA": (24.71, 46.68),  # リヤド
    "AE": (24.45, 54.38),  # アブダビ
    "QA": (25.29, 51.53),  # ドーハ
    "KW": (29.38, 47.99),  # クウェート市
    "OM": (23.59, 58.41),  # マスカット
    "BH": (26.23, 50.59),  # マナーマ
    "JO": (31.95, 35.93),  # アンマン
    "LB": (33.89, 35.50),  # ベイルート
    "SY": (33.51, 36.29),  # ダマスカス
    "IQ": (33.31, 44.36),  # バグダッド
    "YE": (15.37, 44.19),  # サナア
    "PS": (31.90, 35.20),  # ラマッラ
    "TR": (39.93, 32.86),  # アンカラ
    # ヨーロッパ
    "GB": (51.51, -0.13),  # ロンドン
    "DE": (52.52, 13.40),  # ベルリン
    "FR": (48.85, 2.35),  # パリ
    "NL": (52.37, 4.90),  # アムステルダム
    "PL": (52.23, 21.01),  # ワルシャワ
    "IT": (41.90, 12.50),  # ローマ
    "ES": (40.42, -3.70),  # マドリード
    "PT": (38.72, -9.13),  # リスボン
    "BE": (50.85, 4.35),  # ブリュッセル
    "CH": (46.95, 7.45),  # ベルン
    "AT": (48.21, 16.37),  # ウィーン
    "SE": (59.33, 18.07),  # ストックホルム
    "NO": (59.91, 10.75),  # オスロ
    "DK": (55.68, 12.57),  # コペンハーゲン
    "FI": (60.17, 24.94),  # ヘルシンキ
    "IE": (53.35, -6.26),  # ダブリン
    "IS": (64.15, -21.94),  # レイキャビク
    "LU": (49.61, 6.13),  # ルクセンブルク
    "CZ": (50.08, 14.44),  # プラハ
    "SK": (48.15, 17.11),  # ブラチスラバ
    "HU": (47.50, 19.04),  # ブダペスト
    "RO": (44.43, 26.10),  # ブカレスト
    "BG": (42.70, 23.32),  # ソフィア
    "GR": (37.98, 23.73),  # アテネ
    "HR": (45.81, 15.98),  # ザグレブ
    "SI": (46.06, 14.51),  # リュブリャナ
    "RS": (44.79, 20.45),  # ベオグラード
    "ME": (42.44, 19.26),  # ポドゴリツァ
    "EE": (59.44, 24.75),  # タリン
    "LV": (56.95, 24.11),  # リガ
    "LT": (54.69, 25.28),  # ビリニュス
    "BY": (53.90, 27.57),  # ミンスク
    "MD": (47.01, 28.86),  # キシナウ
    "MC": (43.74, 7.42),  # モナコ
    "UA": (50.45, 30.52),  # キーウ
    "RU": (55.75, 37.62),  # モスクワ
    # コーカサス
    "GE": (41.72, 44.79),  # トビリシ
    "AM": (40.18, 44.51),  # エレバン
    "AZ": (40.41, 49.87),  # バクー
    # アフリカ
    "EG": (30.04, 31.24),  # カイロ
    "ZA": (-25.75, 28.19),  # プレトリア
    "NG": (9.08, 7.40),  # アブジャ
    "DZ": (36.75, 3.06),  # アルジェ
    "MA": (34.02, -6.83),  # ラバト
    "TN": (36.81, 10.18),  # チュニス
    "LY": (32.89, 13.19),  # トリポリ
    "SD": (15.50, 32.56),  # ハルツーム
    "ET": (9.03, 38.74),  # アディスアベバ
    "KE": (-1.29, 36.82),  # ナイロビ
    "TZ": (-6.16, 35.75),  # ドドマ
    "CD": (-4.32, 15.31),  # キンシャサ
    "CF": (4.39, 18.56),  # バンギ
    "GQ": (3.75, 8.78),  # マラボ
    "GH": (5.56, -0.20),  # アクラ
    "ML": (12.65, -8.00),  # バマコ
    "BW": (-24.65, 25.91),  # ハボローネ
    # 北米
    "US": (38.90, -77.04),  # ワシントン DC
    "CA": (45.42, -75.70),  # オタワ
    "MX": (19.43, -99.13),  # メキシコシティ
    # 中南米
    "BR": (-15.79, -47.88),  # ブラジリア
    "AR": (-34.60, -58.38),  # ブエノスアイレス
    "CL": (-33.45, -70.67),  # サンティアゴ
    "CO": (4.71, -74.07),  # ボゴタ
    "PE": (-12.05, -77.04),  # リマ
    "VE": (10.48, -66.90),  # カラカス
    "BO": (-16.50, -68.13),  # ラパス
    "EC": (-0.18, -78.47),  # キト
    "CU": (23.11, -82.37),  # ハバナ
    "PA": (8.98, -79.52),  # パナマ市
    "PY": (-25.28, -57.63),  # アスンシオン
    "UY": (-34.90, -56.19),  # モンテビデオ
    "DO": (18.49, -69.90),  # サントドミンゴ
    "HN": (14.07, -87.21),  # テグシガルパ
    # オセアニア
    "AU": (-35.28, 149.13),  # キャンベラ
    "NZ": (-41.29, 174.78),  # ウェリントン
}

# 地域重心 (overview.py の _REGION_MAP の region_id に対応)。地政事象の地域マーカ用。
_REGION_CENTROIDS: dict[str, tuple[float, float]] = {
    "east_asia": (34.0, 121.0),
    "europe": (50.0, 10.0),
    "mideast": (31.0, 45.0),
    "n_america": (40.0, -100.0),
    "s_asia": (22.0, 78.0),
    "se_asia": (12.0, 105.0),
    "oceania": (-25.0, 134.0),
}


class Geocoder:
    """オフライン gazetteer による座標解決。1a は国/地域、後続で city/org を DB から。"""

    def __init__(self, normalizer: TaxonomyNormalizer | None = None) -> None:
        self._normalizer = normalizer or load_normalizer()

    def country(self, iso: str | None) -> GeoPoint | None:
        """ISO 3166-1 alpha-2 → 国代表点 (首都)。"""
        if not iso:
            return None
        c = _COUNTRY_CENTROIDS.get(iso.strip().upper())
        if c is None:
            return None
        return GeoPoint(c[0], c[1], GeoPrecision.COUNTRY, iso.strip().upper(), "centroid")

    def region(self, region_id: str | None) -> GeoPoint | None:
        """region_id (east_asia 等) → 地域重心。"""
        if not region_id:
            return None
        c = _REGION_CENTROIDS.get(region_id.strip().lower())
        if c is None:
            return None
        return GeoPoint(c[0], c[1], GeoPrecision.REGION, region_id.strip().lower(), "centroid")

    def city(self, con: object, name: str | None, country_iso: str | None) -> GeoPoint | None:
        """都市名 + 国 ISO → 都市代表点 (GeoNames gazetteer ``geo_cities`` を引く)。

        3D-a: 被害都市 (victim_city) を都市レベル (CITY tier) に解決する。同名は人口最大を採用。
        ``con`` は呼出側の DB connection (sqlite3.Row / _PgRow 互換)。geo_cities が未取込
        (dev/tests) や query 失敗時は **None に fail-safe** し、呼出側は国レベルに fallback する。
        """
        if not name or not name.strip() or not country_iso or not country_iso.strip():
            return None
        cc = country_iso.strip().upper()
        nm = name.strip().lower()
        try:
            row = con.execute(  # type: ignore[attr-defined]
                "SELECT lat, lon FROM geo_cities WHERE country_code=? AND name_lower=? "
                "ORDER BY population DESC LIMIT 1",
                (cc, nm),
            ).fetchone()
        except Exception as e:  # noqa: BLE001 — gazetteer 未取込/query 失敗は国 fallback
            _log.debug("geo_cities_lookup_failed", error=str(e))
            return None
        if row is None:
            return None
        return GeoPoint(
            float(row["lat"]), float(row["lon"]), GeoPrecision.CITY, name.strip(), "geonames"
        )

    def org(self, con: object, name: str | None) -> GeoPoint | None:
        """組織名 → 本社代表点 (Wikidata gazetteer ``geo_orgs`` を引く)。

        3D-b: 被害組織 (victim_org) を組織本社レベル (ORG_HQ tier) に解決する。Wikidata に
        本社座標がある **著名組織のみ** が対象。geo_orgs 未取込 (WDQS outage 等) や query 失敗時は
        **None に fail-safe** し、呼出側は都市/国レベルに fallback する。
        """
        if not name or not name.strip():
            return None
        nm = name.strip().lower()
        try:
            row = con.execute(  # type: ignore[attr-defined]
                "SELECT lat, lon FROM geo_orgs WHERE name_lower=? LIMIT 1", (nm,)
            ).fetchone()
        except Exception as e:  # noqa: BLE001 — gazetteer 未取込/query 失敗は fallback
            _log.debug("geo_orgs_lookup_failed", error=str(e))
            return None
        if row is None:
            return None
        return GeoPoint(
            float(row["lat"]), float(row["lon"]), GeoPrecision.ORG_HQ, name.strip(), "wikidata"
        )

    def resolve(
        self,
        name: str | None,
        *,
        kind: str = "auto",
        country_hint: str | None = None,
    ) -> GeoPoint | None:
        """名前 → GeoPoint。精度の高い tier から順に試す。

        kind: "country" / "region" / "city" / "org" / "facility" / "auto"。
        country_hint: city/org 解決の曖昧性を絞る ISO (後続 DB tier で使用)。
        1a では city/org/facility は DB 未取込のため None (国 fallback は呼び出し側で
        victim_country を country() に渡す)。
        """
        if not name or not name.strip():
            return None
        cleaned = name.strip()

        if kind == "region":
            return self.region(cleaned)
        if kind == "country":
            iso, _ = self._normalizer.normalize_country(cleaned)
            return self.country(iso)

        # city / org / facility: 後続 (GeoNames / Wikidata) で DB から解決する seam。
        db = self._resolve_db(cleaned, kind=kind, country_hint=country_hint)
        if db is not None:
            return db

        # auto: 国名として解決できれば国レベルで返す (地名が国名のケース)。
        if kind in ("auto", "city"):
            iso, _ = self._normalizer.normalize_country(cleaned)
            if iso:
                return self.country(iso)
        return None

    def _resolve_db(self, name: str, *, kind: str, country_hint: str | None) -> GeoPoint | None:
        """city / org_hq / facility の DB gazetteer 解決 (Phase 1b/1c で実装)。

        現状は未取込のため常に None。GeoNames (city) / Wikidata (org_hq) /
        法人番号+位置参照情報 (facility, 後続) をここに配線する。
        """
        return None

"""Phase H: PMESII-PT 軸と Diamond victim の正規化ロジック (2026-05-19)。

raw text -> canonical id 変換、alias 辞書、union merge を提供する。
yaml ベース (Web UI から編集可能、CLAUDE.md §12 allowlist 内)。

設計:
    - canonical + raw の二重持ち (集計性と拡張性の両立)
    - 未知値は ``canonical='uncategorized'`` + raw 保持 → 週次 taxonomy review で LLM 提案
    - merge_axes は union 戦略 (Recall 重視、over-tagging 容認)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_CONFIG_DIR = Path("config")

# PMESII-PT 8 軸 (確定軸、ここから外れる値は invalid)
PMESII_AXES: frozenset[str] = frozenset(
    {"P", "M", "E", "S", "I-infra", "I-cyber", "P-env", "T"},
)

# 未知値の placeholder canonical id (sector のみ、country は None)
UNCATEGORIZED_SECTOR = "uncategorized"

# 抽出 LLM が「特定できなかった」を null でなく文字列で返す番兵語 (sector/country 共通)。
# 実体ではないため空入力と同義に扱う — uncategorized+raw 保存もせず、taxonomy review の
# 提案対象にもしない。2026-07-12 根治: 'Not Found' が raw に 167 件蓄積し、weekly review が
# 「新カテゴリ not_found」等を毎週提案する事故の上流。境界 (正規化器) で null 化するのが正
# (提案側の stoplist だけでは raw 汚染が残り続ける)。
NULL_SENTINELS: frozenset[str] = frozenset(
    {
        "not found",
        "notfound",
        "unknown",
        "unspecified",
        "unidentified",
        "undetermined",
        "n/a",
        "na",
        "none",
        "null",
        "-",
        "不明",
        "なし",
        "不詳",
        "特定できず",
        "該当なし",
    }
)

# victim_country のスコープ語 (監査 2026-08-01 ⑥)。briefing/summarizer.j2 が複数国攻撃に
# 指示している語彙 + 実データ raw 上位 (global 107 / eu 13 件/30日)。ISO2 には
# 写像しない (countries.yaml の意味 = 単一国を保つ) — victim_country_scope 列で持つ。
_SCOPE_GLOBAL_TOKENS: frozenset[str] = frozenset(
    {"global", "worldwide", "world", "international", "世界", "全世界", "世界各国", "多数国"}
)
_SCOPE_REGIONAL_TOKENS: frozenset[str] = frozenset(
    {
        "eu",
        "europe",
        "european union",
        "欧州",
        "ヨーロッパ",
        "apac",
        "asia",
        "asia-pacific",
        "アジア",
        "アジア太平洋",
        "middle east",
        "中東",
        "africa",
        "アフリカ",
        "latin america",
        "中南米",
        "南米",
        "north america",
        "北米",
        "nato",
        "asean",
        "gulf",
        "湾岸諸国",
    }
)


@dataclass(frozen=True)
class TaxonomyNormalizer:
    """yaml ベースの 正規化辞書 (frozen、process-life cache 想定)。

    内部表現:
        sector_lookup: {alias_lower: canonical_id}
        country_lookup: {alias_lower: iso_code}
        feed_default_axes: {feed_title: [axes]}
        category_default_axes: {category: [axes]}
    """

    sector_lookup: dict[str, str]
    country_lookup: dict[str, str]
    feed_default_axes: dict[str, list[str]]
    category_default_axes: dict[str, list[str]]
    sector_canonical_ids: tuple[str, ...]
    country_canonical_ids: tuple[str, ...]

    # ----- sector -----

    def normalize_sector(self, raw: str | None) -> tuple[str | None, str | None]:
        """raw text を sector canonical id に正規化。

        Returns:
            (canonical_id, raw):
                - 空入力: (None, None)
                - 既知 alias マッチ: (canonical, raw)
                - 未知値: ('uncategorized', raw) — taxonomy review で扱う
        """
        if not raw or not raw.strip():
            return None, None
        cleaned = raw.strip()
        if cleaned.lower() in NULL_SENTINELS:
            return None, None  # 番兵語 = 「特定できず」の別表記。空入力と同義に扱う
        canonical = self.sector_lookup.get(cleaned.lower())
        if canonical is not None:
            return canonical, cleaned
        # R-E (曖昧入力の正規化): 完全一致失敗時の保守的 fuzzy fallback。LLM の自由表記
        # 揺れ (製造業 / 政府・公共サービス / critical manufacturing 等) を、既知 alias の
        # 部分一致 (最長優先) で吸収してから uncategorized に落とす。ASCII alias は誤マッチ
        # 抑止のため min 5 文字、CJK は 2 文字。「存在≠完全一致」を緩和するが保守的。
        fuzzy = self._fuzzy_sector(cleaned.lower())
        if fuzzy is not None:
            return fuzzy, cleaned
        # 未知値: uncategorized で保存、raw を残す
        return UNCATEGORIZED_SECTOR, cleaned

    def _fuzzy_sector(self, needle: str) -> str | None:
        """既知 alias が raw text に含まれれば、その canonical を返す (最長一致)。

        ASCII alias は **単語境界** マッチ (hospital が hospitality を誤って医療にしない等の
        部分語誤爆を防ぐ)。CJK alias は単語境界が無いため部分文字列マッチ (製造→製造業)。
        """
        best_alias = ""
        best_canonical: str | None = None
        for alias, canonical in self.sector_lookup.items():
            is_cjk = any(ord(c) > 0x3000 for c in alias)
            if is_cjk:
                if len(alias) < 2 or alias not in needle:
                    continue
            else:
                # ASCII: min 5 文字 + 単語境界 (部分語誤爆防止)
                if len(alias) < 5 or not re.search(rf"\b{re.escape(alias)}\b", needle):
                    continue
            if len(alias) > len(best_alias):
                best_alias, best_canonical = alias, canonical
        return best_canonical

    def list_sector_canonical(self) -> tuple[str, ...]:
        """既知の canonical id 一覧 (UI dropdown 用)。"""
        return self.sector_canonical_ids

    # ----- country -----

    def normalize_country(self, raw: str | None) -> tuple[str | None, str | None]:
        """raw text を country ISO 3166-1 alpha-2 に正規化。

        Returns:
            (iso_code, raw):
                - 空入力: (None, None)
                - 既知 alias マッチ: (iso, raw)
                - 未知値: (None, raw) — taxonomy review で扱う (sector と違い uncategorized なし)
        """
        if not raw or not raw.strip():
            return None, None
        cleaned = raw.strip()
        if cleaned.lower() in NULL_SENTINELS:
            return None, None  # 番兵語 = 「特定できず」の別表記。空入力と同義に扱う
        iso = self.country_lookup.get(cleaned.lower())
        if iso is not None:
            return iso, cleaned
        # 保守的フォールバック: LLM 出力が JSON 風に漏れ "US'," / "Japan'," / "[US]" の
        # ように余分な引用符・括弧・末尾カンマを纏ったケースを救済する。**前後の余剰文字を
        # 剥がすだけ** (内部カンマは触らない → "US, Canada" のような複数国は依然 unresolved)。
        stripped = cleaned.strip(" \t\r\n'\"`[](){}.,;:")
        if stripped and stripped != cleaned:
            iso = self.country_lookup.get(stripped.lower())
            if iso is not None:
                return iso, cleaned
        return None, cleaned

    def list_country_canonical(self) -> tuple[str, ...]:
        """既知の ISO 一覧 (UI dropdown 用)。"""
        return self.country_canonical_ids

    def normalize_country_scope(self, raw: str | None) -> tuple[str | None, tuple[str, ...]]:
        """ISO2 に解決できない victim_country raw の「スコープ」を判定する。

        監査 2026-08-01 ⑥: briefing/summarizer.j2 は複数国攻撃に "global"/"EU"/"APAC" を
        出すよう指示しているのに正規化器に受け皿が無く、月 175 件が黙って
        victim_country_iso=NULL に落ちていた断線への対処。ISO2 の意味 (単一国) は
        不変のまま、スコープを別値で表現する (countries.yaml へ疑似コードを足すと
        地図 centroid drift-guard / nation vocab 汚染を起こすため不採用)。

        Returns:
            (scope, isos):
                - ("global", ()) — 全世界規模
                - ("regional", ()) — 地域ブロック (EU/APAC/中東 等)
                - ("multi", (iso, ...)) — 複数国列挙。isos = 解決できた構成国
                - (None, ()) — スコープでもない未知値 (従来どおり taxonomy review 対象)
        """
        if not raw or not raw.strip():
            return None, ()
        cleaned = raw.strip().lower().strip(" \t\r\n'\"`[](){}.,;:")
        if cleaned in _SCOPE_GLOBAL_TOKENS:
            return "global", ()
        if cleaned in _SCOPE_REGIONAL_TOKENS:
            return "regional", ()
        # 複数国列挙: 区切りで分解し 2 か国以上が ISO 解決できれば multi
        parts = [p.strip() for p in re.split(r"[,、/&]|\band\b|・", cleaned) if p.strip()]
        if len(parts) >= 2:
            isos: list[str] = []
            for p in parts:
                iso = self.country_lookup.get(p)
                if iso is not None and iso not in isos:
                    isos.append(iso)
            if len(isos) >= 2:
                return "multi", tuple(isos)
        return None, ()

    # ----- PMESII-PT axes -----

    def merge_axes(
        self,
        *,
        llm_axes: list[str] | None,
        feed_title: str | None,
        category: str | None,
    ) -> list[str]:
        """LLM + feed default + category default を union して valid 軸のみ返す。

        union 戦略は memory cti_pir.md の Recall 重視と一貫。over-tagging 容認。
        """
        merged: set[str] = set()
        for axis in llm_axes or []:
            if isinstance(axis, str) and axis in PMESII_AXES:
                merged.add(axis)
        if feed_title:
            for axis in self.feed_default_axes.get(feed_title, []):
                if axis in PMESII_AXES:
                    merged.add(axis)
        if category:
            for axis in self.category_default_axes.get(category, []):
                if axis in PMESII_AXES:
                    merged.add(axis)
        # 順序固定 (assert 容易性)
        return sorted(merged, key=lambda a: list(PMESII_AXES).index(a) if a in PMESII_AXES else 99)


def _load_yaml_dict(path: Path) -> dict[str, object]:
    if not path.exists():
        _log.info("taxonomy_yaml_missing", path=str(path))
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except yaml.YAMLError as e:
        _log.warning("taxonomy_yaml_parse_failed", path=str(path), error=str(e))
    return {}


def _canonical_display(raw: dict[str, object]) -> dict[str, str]:
    """yaml の `canonical: {id: {display: ...}}` から id→display を取り出す。"""
    canon = raw.get("canonical", {})
    out: dict[str, str] = {}
    if isinstance(canon, dict):
        for cid, val in canon.items():
            disp = val.get("display") if isinstance(val, dict) else None
            out[str(cid)] = disp if isinstance(disp, str) and disp else str(cid)
    return out


def load_sector_display(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, str]:
    """canonical sector id → 日本語 display (victim_sectors.yaml)。表示 vocab の SSoT。"""
    return _canonical_display(_load_yaml_dict(config_dir / "cti/victim_sectors.yaml"))


def load_country_display(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, str]:
    """canonical ISO2 → 日本語 display (countries.yaml)。表示 vocab の SSoT (nation も再利用)。"""
    return _canonical_display(_load_yaml_dict(config_dir / "cti/countries.yaml"))


def _build_alias_lookup(
    raw: dict[str, object],
    *,
    include_canonical_id_as_alias: bool = True,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """yaml `canonical` 構造 → {alias_lower: canonical_id} の lookup を構築。

    canonical id 自身も alias として登録 (例: 'JP' → 'JP')。
    """
    canonical = raw.get("canonical", {})
    if not isinstance(canonical, dict):
        return {}, ()
    lookup: dict[str, str] = {}
    canonical_ids: list[str] = []
    for canonical_id, body in canonical.items():
        if not isinstance(canonical_id, str):
            continue
        canonical_ids.append(canonical_id)
        if include_canonical_id_as_alias:
            lookup[canonical_id.lower()] = canonical_id
        if not isinstance(body, dict):
            continue
        for alias in body.get("aliases", []) or []:
            if isinstance(alias, str) and alias.strip():
                lookup[alias.strip().lower()] = canonical_id
    return lookup, tuple(canonical_ids)


def load_normalizer(config_dir: Path = DEFAULT_CONFIG_DIR) -> TaxonomyNormalizer:
    """設定 yaml を読み込んで TaxonomyNormalizer を構築する。

    yaml 不在時は空の lookup で動作 (uncategorized で受ける)。
    """
    sectors_raw = _load_yaml_dict(config_dir / "cti/victim_sectors.yaml")
    countries_raw = _load_yaml_dict(config_dir / "cti/countries.yaml")
    mapping_raw = _load_yaml_dict(config_dir / "cti/pmesii_default_mapping.yaml")

    sector_lookup, sector_ids = _build_alias_lookup(sectors_raw)
    country_lookup, country_ids = _build_alias_lookup(countries_raw)

    feed_axes_raw = mapping_raw.get("feed_to_axes", {}) or {}
    category_axes_raw = mapping_raw.get("category_to_axes", {}) or {}

    feed_axes: dict[str, list[str]] = {}
    if isinstance(feed_axes_raw, dict):
        for k, v in feed_axes_raw.items():
            if isinstance(k, str) and isinstance(v, list):
                feed_axes[k] = [a for a in v if isinstance(a, str)]

    category_axes: dict[str, list[str]] = {}
    if isinstance(category_axes_raw, dict):
        for k, v in category_axes_raw.items():
            if isinstance(k, str) and isinstance(v, list):
                category_axes[k] = [a for a in v if isinstance(a, str)]

    _log.info(
        "taxonomy_normalizer_loaded",
        sectors=len(sector_ids),
        countries=len(country_ids),
        feed_mappings=len(feed_axes),
        category_mappings=len(category_axes),
    )

    return TaxonomyNormalizer(
        sector_lookup=sector_lookup,
        country_lookup=country_lookup,
        feed_default_axes=feed_axes,
        category_default_axes=category_axes,
        sector_canonical_ids=sector_ids,
        country_canonical_ids=country_ids,
    )

"""語彙レジストリの強制テスト (docs/vocabulary_label_architecture.md §4.6)。

肝: 「backend が値を足してラベルを忘れる」を **同一言語 (Python) 内で** 検出する。
canonical (正規値集合) を持つ vocab は、その値集合と登録ラベルのキーが完全一致すること。
これが赤くなったら、新しい enum 値にラベルを付け忘れている (= 旧来 frontend で「生の
英語が黙って出る」の発生源)。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, get_args

from src.storage.records import RunStatus
from src.vocab import all_vocabularies, get_vocabulary, registered_names
from src.vocab.registry import vocabularies_with_canonical


def test_registry_is_non_empty() -> None:
    assert registered_names()  # 少なくとも 1 つ登録されている


def test_canonical_vocabs_cover_exactly_their_value_set() -> None:
    """canonical を持つ全 vocab で「登録ラベルのキー == 正規値集合」。

    これが本再設計の強制の核心。値を足してラベルを忘れると、ここが不一致で赤くなる。
    """
    offenders: list[str] = []
    for vocab in vocabularies_with_canonical():
        assert vocab.canonical is not None  # for mypy
        registered = set(vocab.values())
        canonical = set(vocab.canonical)
        missing = canonical - registered  # ラベル付け忘れ
        extra = registered - canonical  # 存在しない値にラベル
        if missing or extra:
            offenders.append(f"{vocab.name}: missing={sorted(missing)} extra={sorted(extra)}")
    assert not offenders, "vocab とバックエンド正規値集合の不一致:\n" + "\n".join(offenders)


def test_run_status_matches_backend_literal() -> None:
    """run_status vocab が RunStatus Literal と厳密一致する (Phase 1 代表)。"""
    vocab = get_vocabulary("run_status")
    assert vocab is not None
    assert set(vocab.values()) == set(get_args(RunStatus))


def test_run_status_labels_unchanged() -> None:
    """移行でラベル文言が変わっていない (旧 frontend RUN_STATUS_LABELS と一致)。"""
    vocab = get_vocabulary("run_status")
    assert vocab is not None
    labels = {item.value: item.label for item in vocab.items}
    assert labels == {
        "succeeded": "成功",
        "failed": "失敗",
        "running": "実行中",
        "partial_failure": "一部失敗",
    }


def test_serialize_shape() -> None:
    """配信形式が {name: [{value,label,order}]} で order を含む。"""
    payload = all_vocabularies()
    assert "run_status" in payload
    rows = payload["run_status"]
    assert rows and all({"value", "label", "order"} <= set(row) for row in rows)
    assert [row["order"] for row in rows] == list(range(len(rows)))


def test_importance_canonical_matches_backend_literal() -> None:
    """importance vocab の triage レベルが discord_publisher.Importance と一致する。

    importance は複数箇所に定義があるため (config_loader.IMPORTANCE_LEVELS /
    discord_publisher.Importance / articles_feed._ALLOWED_IMPORTANCE)、SSoT が割れないよう
    突合する。"unknown" は sentinel なので triage レベル比較からは除く。
    """
    from src.config_loader import IMPORTANCE_LEVELS
    from src.tools.discord_publisher import Importance
    from src.ui.api.articles_feed import _ALLOWED_IMPORTANCE

    assert frozenset(IMPORTANCE_LEVELS) == frozenset(get_args(Importance))
    assert set(_ALLOWED_IMPORTANCE) == set(IMPORTANCE_LEVELS)

    vocab = get_vocabulary("importance")
    assert vocab is not None
    assert set(vocab.values()) == frozenset(IMPORTANCE_LEVELS) | {"unknown"}


def test_capability_band_canonical_matches_backend_literal() -> None:
    """capability_band vocab が actor_threat.CapabilityBand と厳密一致する。"""
    from src.ui.services.actor_threat import CapabilityBand

    vocab = get_vocabulary("capability_band")
    assert vocab is not None
    assert set(vocab.values()) == set(get_args(CapabilityBand))


def test_severity_scales_use_english_labels() -> None:
    """CTI 重大度/能力スケールは英語維持 (docs §2.6.1・2026-07-21 方針)。"""
    for name in ("importance", "capability_band"):
        vocab = get_vocabulary(name)
        assert vocab is not None
        labels = {item.value: item.label for item in vocab.items}
        assert labels == {"high": "High", "medium": "Medium", "low": "Low", "unknown": "Unknown"}


def test_operational_vocabs_bind_to_backend_sources() -> None:
    """運用系 vocab が backend の Literal/定数を網羅する (drift 検出)。"""
    from src.config_loader import KNOWN_ARTICLE_CATEGORIES
    from src.storage.records import ArticleStatus
    from src.ui.services.health import CheckStatus

    art = get_vocabulary("article_status")
    assert art is not None
    # ArticleStatus の全値にラベルがある (marked_read の欠落回帰を防ぐ)。
    assert set(art.values()) == set(get_args(ArticleStatus))
    assert art.label_for("marked_read") == "既読"

    health = get_vocabulary("health_status")
    assert health is not None
    assert set(get_args(CheckStatus)) <= set(health.values())

    cat = get_vocabulary("category")
    assert cat is not None
    assert set(KNOWN_ARTICLE_CATEGORIES) <= set(cat.values())


def test_operational_vocabs_stay_japanese() -> None:
    """運用・記述系は日本語維持 (英語化しない — docs §2.6.1)。"""
    stance = get_vocabulary("stance")
    assert stance is not None
    assert stance.label_for("propaganda") == "プロパガンダ"
    trigger = get_vocabulary("trigger")
    assert trigger is not None
    assert trigger.label_for("scheduler") == "自動実行"


def test_confidence_covers_every_backend_literal() -> None:
    """confidence vocab が backend の **全 Literal 定義** を被覆する (2 scale / 5 定義)。

    ICD-203 (grounded synthesis) だけが high/**moderate**/low で、他は high/medium/low。
    値空間の定義が 5 箇所に散っており SSoT を 1 つに畳めていないため、
    **新しい定義や値が増えたらここで気付く** ことだけが drift の歯止め
    (period_type と同じ方式)。定義を足したらこの表にも 1 行足す。
    """
    from src.cti.diamond_model import Confidence as DiamondConfidence
    from src.cti.llm_routing_flags import Confidence as RoutingFlagsConfidence
    from src.cti.source_basis import Confidence as SourceBasisConfidence
    from src.pir.compiler import ConfidenceLevel as PirConfidence
    from src.synthesis.grounded.estimate import Confidence as GroundedConfidence

    vocab = get_vocabulary("confidence")
    assert vocab is not None
    keys = set(vocab.values())
    literals = {
        "synthesis.grounded.estimate.Confidence": GroundedConfidence,  # high/moderate/low
        "cti.diamond_model.Confidence": DiamondConfidence,  # high/medium/low
        "cti.llm_routing_flags.Confidence": RoutingFlagsConfidence,
        "cti.source_basis.Confidence": SourceBasisConfidence,
        "pir.compiler.ConfidenceLevel": PirConfidence,
    }
    uncovered = {name: sorted(set(get_args(literal)) - keys) for name, literal in literals.items()}
    assert not any(uncovered.values()), f"ラベル未登録の確度値: {uncovered}"
    labels = {i.value: i.label for i in vocab.items}
    assert labels == {"high": "High", "medium": "Medium", "moderate": "Moderate", "low": "Low"}


def test_icd203_keyed_maps_cover_every_grounded_confidence_value() -> None:
    """ICD-203 の確度でキーを引く map に取りこぼしを作らない。

    どれも ``.get(x, default)`` か ``dict[str, …]`` で引くため、値が増えても例外にならず
    **静かに既定値へ落ちる** (落ちたことは出力に出ない)。2 scale が併存する以上、
    「moderate を期待する map に medium が来る」は起こりうる事故なので、キー集合を固定する。
    """
    from src.assessment import forecast, ledger, salience
    from src.synthesis.grounded import estimate, render
    from src.synthesis.grounded.estimate import Confidence as GroundedConfidence

    grounded = set(get_args(GroundedConfidence))
    # 値の型 (int/float/str) も key の型 (str / Literal) も map ごとに違うため Any。
    keyed_by_icd203: dict[str, Mapping[Any, object]] = {
        "assessment.forecast._CONF_TO_UI": forecast._CONF_TO_UI,
        "assessment.ledger._CONF_RANK": ledger._CONF_RANK,
        "assessment.salience._CONF_EPISTEMIC": salience._CONF_EPISTEMIC,
        "synthesis.grounded.estimate._CONF_RANK": estimate._CONF_RANK,
        "synthesis.grounded.estimate._STEP_UP": estimate._STEP_UP,
        "synthesis.grounded.render._CONF_JA": render._CONF_JA,
    }
    mismatched = {
        name: sorted(set(mapping) ^ grounded)
        for name, mapping in keyed_by_icd203.items()
        if set(mapping) != grounded
    }
    assert not mismatched, f"ICD-203 確度のキー集合とずれた map: {mismatched}"


def test_cross_scale_bridges_are_total_and_land_in_the_target_scale() -> None:
    """2 つの scale をまたぐ写像は、入力側で全域・出力側で相手の値空間に収まること。

    ``_SB_CEILING`` (source_basis high/medium/low → ICD-203) と ``_CONF_TO_UI``
    (ICD-203 → UI の high/medium/low) が唯一の往復路。ここが穴あきだと
    ``.get(…, "moderate")`` の既定値に黙って落ちる。
    """
    from src.assessment.forecast import _CONF_TO_UI
    from src.cti.diamond_model import Confidence as UiConfidence
    from src.cti.source_basis import Confidence as SourceBasisConfidence
    from src.synthesis.grounded.estimate import _SB_CEILING
    from src.synthesis.grounded.estimate import Confidence as GroundedConfidence

    assert set(_SB_CEILING) == set(get_args(SourceBasisConfidence))
    assert set(_SB_CEILING.values()) <= set(get_args(GroundedConfidence))
    assert set(_CONF_TO_UI) == set(get_args(GroundedConfidence))
    assert set(_CONF_TO_UI.values()) <= set(get_args(UiConfidence))


def test_period_type_matches_all_backend_constants() -> None:
    """period_type の複数分散定数がすべて vocab と一致する (SSoT 未指定の drift 検出)。"""
    from src.pir.models import SpotlightWindow
    from src.scheduler.job_recovery import Cadence
    from src.spotlight.models import SpotlightPeriod
    from src.synthesis.runner import _VALID_PERIODS

    vocab = get_vocabulary("period_type")
    assert vocab is not None
    keys = set(vocab.values())
    assert set(_VALID_PERIODS) == keys
    assert set(get_args(SpotlightPeriod)) == keys
    assert set(get_args(SpotlightWindow)) == keys
    assert set(get_args(Cadence)) == keys


def test_article_type_matches_backend_literal() -> None:
    """article_type vocab が ArticleType と一致 (advisory 欠落回帰を防ぐ)。"""
    from src.cti.article_classifier import ArticleType

    vocab = get_vocabulary("article_type")
    assert vocab is not None
    assert set(vocab.values()) == set(get_args(ArticleType))
    assert vocab.label_for("advisory") == "アドバイザリ"


def test_longtail_vocabs_cover_backend_literals() -> None:
    """long-tail vocab が対応する backend Literal を被覆する (backend の値追加を検出)。

    vocab はラベル表示の gap-fill (intent の unknown、assigned_by の llm 等) で Literal より
    多いことがあるため subset (Literal ⊆ vocab) で確認する。逆向き (vocab の余剰) は
    generic な test_canonical_vocabs_cover_exactly_their_value_set が canonical で pin する。
    """
    from src.assessment.situation_store import AssignedBy, DeltaType, SituationStatus
    from src.cti.diamond_model import SocioPoliticalIntent
    from src.forecast.analytics import TrendDirection
    from src.forecast.models import ForecastScope
    from src.scheduler.job_registry import JobKind, Protection
    from src.synthesis.grounded.estimate import Polarity
    from src.ui.services.actor_threat import ActivityState, Tier

    bindings = {
        "intent": SocioPoliticalIntent,
        "delta_type": DeltaType,
        "situation_status": SituationStatus,
        "assigned_by": AssignedBy,
        "polarity": Polarity,
        "forecast_scope": ForecastScope,
        "forecast_direction": TrendDirection,
        "activity_state": ActivityState,
        "threat_tier": Tier,
        "job_kind": JobKind,
        "job_protection": Protection,
    }
    offenders: list[str] = []
    for name, literal in bindings.items():
        vocab = get_vocabulary(name)
        assert vocab is not None, name
        missing = set(get_args(literal)) - set(vocab.values())
        if missing:
            offenders.append(f"{name}: vocab に無い backend 値 {sorted(missing)}")
    assert not offenders, "\n".join(offenders)


def test_transport_covers_backend_literal() -> None:
    """transport vocab が TransportT を被覆する (+ 表示変種 atom)。"""
    from src.sources.source_store import TransportT

    vocab = get_vocabulary("transport")
    assert vocab is not None
    assert set(get_args(TransportT)) <= set(vocab.values())
    assert vocab.label_for("rss") == "RSS"  # CTI/tech 語彙は英語維持


def test_dynamic_b_vocabs_served_from_config() -> None:
    """config 由来 (B) の sector/country が config の display と一致して配信される。"""
    from src.cti.taxonomy_normalizer import load_country_display, load_sector_display

    served = all_vocabularies()
    assert "sector" in served and "country" in served

    sector = get_vocabulary("sector")
    assert sector is not None
    assert set(sector.values()) == set(load_sector_display())

    country = get_vocabulary("country")
    assert country is not None
    assert set(country.values()) == set(load_country_display())
    # 固有名詞は日本語維持 (severity ではない)。キーは大文字 ISO2 (countries.yaml canonical)。
    # nation は小文字コードを toUpperCase してこの vocab を再利用する (別 vocab は作らない)。
    assert country.label_for("JP") == "日本"
    assert country.label_for("CN") == "中国"


def test_label_for_falls_back_to_raw_value() -> None:
    """未知値は原値へ fallback (frontend humanizer と対称・空欄化しない)。"""
    vocab = get_vocabulary("run_status")
    assert vocab is not None
    assert vocab.label_for("succeeded") == "成功"
    assert vocab.label_for("some_new_value") == "some_new_value"


def test_ach_hypothesis_covers_full_menu_with_japanese_labels() -> None:
    """ach_hypothesis は HYPOTHESIS_MENU (値の源泉) を全被覆し、全ラベルが日本語であること。

    regression (2026-07-23): 静的複製時代に POSTURE 系 3 仮説が欠落し、ACH 表示に
    posture_global_no_jp_evidence 等の生 ID が漏出した。導出化 + この被覆テストで再発防止。
    """
    from src.synthesis.grounded.hypotheses import HYPOTHESIS_MENU

    vocab = get_vocabulary("ach_hypothesis")
    assert vocab is not None
    labels = {i.value: i.label for i in vocab.items}
    assert set(labels) == {h.id for h in HYPOTHESIS_MENU}
    assert labels["posture_global_no_jp_evidence"] == "世界的に活動・日本標的の直接証拠なし"
    # 生 ASCII token をラベルとして配信しない (UI 表示規約: 生 enum 直接表示の禁止)
    non_japanese = [v for v, lbl in labels.items() if not any(ord(c) > 0x7F for c in lbl)]
    assert not non_japanese, f"日本語ラベルでない仮説: {non_japanese}"

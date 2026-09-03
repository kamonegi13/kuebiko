"""記事の種別 (event_kind) — LLM による数値化を群化の特徴量に渡す。

⭐ 2026-09-03 の利用者提案「記事を LLM で数値化し特徴に」の採用分。利用者が
裁定してきた境界 —「修正した vs 悪用した」「一括勧告 vs 個別事象」「統計 vs
出来事」— はすべて記事の**種別**の話であり、種別の組を特徴にすればその裁定が
特徴空間に入る。

較正 (2026-09-03・365 組 CV・seed 3 種): v3 91.2±0.7% → +種別対 92.1±0.1%、
ハブ様ペア 87.9% → 89.7%。ハブ度/CVE 数の特徴は種別対への上乗せ効果なしで不採用。

⚠ 分類は 26B (fast ティア)・記事ごとに 1 回・DB にキャッシュする。分類に失敗した
記事は "other" (学習時と同じ退避先 — 欠測の意味を学習と本番で揃える)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

#: 種別の SSoT。並びと綴りを変えるときは学習済みモデルも作り直すこと。
KINDS: tuple[str, ...] = ("advisory", "exploitation", "breach", "stats", "roundup", "other")
FALLBACK_KIND = "other"

SYSTEM = "\n".join(
    (
        "あなたは日本の CTI アナリストです。記事を 1 つの種別に分類します。",
        "- advisory: ベンダ/当局の勧告・修正の告知 (一括の脆弱性修正、KEV 追加を含む)",
        "- exploitation: 特定の脆弱性が悪用されている・攻撃が進行しているという報告",
        "- breach: 特定組織の被害・侵害・流出の公表や掲載",
        "- stats: 露出台数・集計・調査統計の報告",
        "- roundup: 週刊/日次のまとめ・ダイジェスト・ニュースレター",
        "- other: 上のどれでもない",
    )
)

#: 種別対から作る特徴の名前 (pair_features が FEATURE_NAMES へ連結する)。
KIND_FEATURE_NAMES: tuple[str, ...] = (
    "kind_same",
    "kind_advisory_vs_incident",
    "kind_roundup_one",
    "kind_stats_one",
)


class KindVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str


def kind_pair_features(kind_a: str, kind_b: str) -> list[float]:
    """種別対 → 特徴 4 つ。**学習時とここの定義が一致していること** (SSoT はここ)。"""
    pair = {kind_a, kind_b}
    return [
        float(kind_a == kind_b),
        float("advisory" in pair and bool(pair & {"exploitation", "breach"})),
        float("roundup" in pair and kind_a != kind_b),
        float("stats" in pair and kind_a != kind_b),
    ]


async def classify(llm: LLMClient, title: str, summary: str) -> str:
    """記事 1 本の種別。失敗は FALLBACK_KIND (呼び手を止めない)。"""
    prompt = (
        f"見出し: {title}\n要約: {(summary or '')[:400]}\n\n"
        f"種別を 1 つ選んでください: {', '.join(KINDS)}"
    )
    try:
        v = await llm.generate_structured(
            prompt, KindVerdict, system=SYSTEM, max_attempts=2, think=False
        )
        return v.kind if v.kind in KINDS else FALLBACK_KIND
    except Exception as e:  # noqa: BLE001 — 分類の失敗で群化を止めない
        _log.warning("event_kind_classify_failed", error=str(e)[:120])
        return FALLBACK_KIND

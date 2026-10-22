# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""深刻さの判定 C (Jev 型の問い + ML) の shadow 部品 (2026-10-22)。

比較 (data/eval/severity_method_compare_2026_10_21.md) で固定した 9 問を fast ティアの SFT に
``decision_probe`` で当て (logprob の P(はい))、付帯情報と合わせた RF で「S3 か」の確率を出す。
**記録だけ**で、配信・重要度・``derive_severity`` の判定には一切使わない (shadow)。

- 問いの文面は比較で固定した ``data/mlx/sevcmp_probe.py`` のものをそのまま使う。**言い回しを直さない**
  (直すと RF の学習時の特徴とずれる。直すなら学習し直す)。
- RF は ``config/models/severity_s3_model.json`` (``scripts/train_severity_s3_model.py``)。**問いを当てる
  モデルごとの確率で学習する**ので、JSON に学習時のモデル名 (``probe_model``) を持つ。割当が変わって
  別のモデルで当てたときは記録に不一致の印を付ける (その確率は較正が合わない)。
- 非サイバー・派生記事・まとめ記事は A の決まりごとで先に外し、問いを当てない (GPU 節約・
  ML が非サイバーを稀に S3 にする弱点を塞ぐ)。
停止: ``SEVERITY_C_SHADOW=0``。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.assessment.assign_model import AssignModel
from src.logging_config import get_logger
from src.tools.tree_forest import forest_probability

_log = get_logger(__name__)

SHADOW_ENV = "SEVERITY_C_SHADOW"
DEFAULT_MODEL_PATH = Path("config/models/severity_s3_model.json")
#: 本文の上限 (深刻度の軸の本文入力 ``severity_axes.BODY_MAX_CHARS`` と同じ)
BODY_MAX_CHARS = 12000
CHOICES = ("はい", "いいえ")
#: ロジットに直すときの確率の下限 (学習時と同じ)
LOGIT_EPS = 1e-4
#: 数値特徴の正規化 (学習時と同じ)
CVSS_MAX = 10.0
CVSS_CRITICAL = 9.0
LOSS_LOG_SCALE = 25.0
CVE_COUNT_CAP = 20
NUMERIC_FEATURES = ("kev", "cvss", "cvss_ge9", "log_loss", "n_cves")
#: 記事の種類の列の接頭辞 (カテゴリ・記事の型・分類の種類の one-hot)
CATEGORY_PREFIX, TYPE_PREFIX, KIND_PREFIX = "cat=", "type=", "kind="
PROBE_PREFIX = "p."

#: 問い (id, 文)。版 .5 の定め: 確認 = 被害者・当局・独立研究者の直接の証拠 (攻撃者の主張は除く) /
#: 重要インフラ = 電力・ガス・水道・通信・交通・医療・政府・銀行等 (暗号資産取引所は除く) / 直近 = 7 日以内 /
#: 脆弱性 = 悪用の観測を先に見る (SSVC)。固定した時刻: 2026-10-21 23:44 JST。
QUESTIONS: tuple[tuple[str, str], ...] = (
    (
        "cyber",
        "この記事の主題は、サイバーセキュリティ上の出来事 (サイバー攻撃・侵害・情報漏えい・脆弱性・マルウェア・"
        "攻撃キャンペーン) ですか。軍事行動・外交・政策・経済の記事で、サイバーが主題でないものは「いいえ」です。",
    ),
    (
        "new_event",
        "この記事は、具体的な新しい出来事 (新しい攻撃・被害・脆弱性の公表・新しい手口の解明) を報じていますか。"
        "論評・解説・入門・複数の既知の話題のまとめ・統計や傾向の報告・製品の宣伝が中心なら「いいえ」です。",
    ),
    (
        "harm_confirmed",
        "実際の被害 (不正アクセス・情報の漏えい・業務の停止・データの破壊・金銭の窃取) が起きたことが、"
        "被害組織・当局・独立した研究者によって確認されていると書かれていますか。攻撃者の主張だけ、"
        "または被害の可能性だけなら「いいえ」です。",
    ),
    (
        "exploited",
        "記事が扱う脆弱性や攻撃手法が、実際の攻撃ですでに悪用されている (in the wild) と書かれていますか。"
        "概念実証 (PoC) の公開だけ、または悪用の可能性だけなら「いいえ」です。",
    ),
    (
        "wide",
        "被害が複数の組織、特定の業界全体、または国の規模に及んでいると書かれていますか。単一の組織・個人の被害、"
        "または被害者のいない記事なら「いいえ」です。",
    ),
    (
        "critical",
        "攻撃や被害の対象に、政府機関・防衛・重要インフラ (電力・ガス・水道・通信・交通・医療・銀行や決済) が"
        "含まれていますか。",
    ),
    ("state", "攻撃の主体が、国家または国家の支援を受けた集団だと記事が明示していますか。"),
    (
        "large",
        "被害の規模が大きいと書かれていますか (10 万人・10 万件以上の情報、1 億ドル以上の被害額、大きな組織の業務停止、"
        "または広く使われる製品やソフトウェアの正規の配布経路の汚染)。",
    ),
    (
        "severe",
        "この記事が報じる事象は、サイバーの深刻さとして「重大」ですか。重大の例: 実環境で悪用されている脆弱性、"
        "確認された広範な被害、国家系による政府・防衛・重要インフラの侵害、重要インフラの停止、大規模な情報漏えい。"
        "単一組織の侵害・PoC だけ・CVSS が高いだけ・サイバー以外の記事は重大ではありません。",
    ),
)

QUESTION_IDS: tuple[str, ...] = tuple(qid for qid, _ in QUESTIONS)
#: 「S3 か」の確率の列名 (記録の probs_json の並びにも使う)
SKIP_ROUNDUP = "roundup"


def shadow_enabled() -> bool:
    """``SEVERITY_C_SHADOW=0`` で停止 (既定 on)。"""
    return os.environ.get(SHADOW_ENV, "1") != "0"


def build_question(title: str, published: str | None, body: str, question: str) -> str:
    """1 問の prompt。本文を先・問いを後に置き、同じ記事の問いどうしで前置きのキャッシュを効かせる。"""
    text = (body or "")[:BODY_MAX_CHARS]
    return (
        f"次の記事を読んで、問いに答えてください。\n\n# 記事\n見出し: {title}\n"
        f"公開日: {published or '不明'}\n本文:\n{text}\n\n# 問い\n{question}"
    )


@dataclass(frozen=True)
class ArticleMeta:
    """RF に入れる付帯情報 (すべて本番の関数の出力 — ``importance_v2.build_inputs`` 等)。"""

    category: str
    article_type: str
    event_kind: str
    on_kev: bool
    max_cvss: float
    loss_usd: float
    n_cves: int


def _logit(p: float | None) -> float:
    """P(はい) → logit。欠測は中立 (P=0.5)。学習時と同じ。"""
    if p is None:
        return 0.0
    q = min(1 - LOGIT_EPS, max(LOGIT_EPS, p))
    return math.log(q / (1 - q))


def _numeric(name: str, meta: ArticleMeta) -> float:
    if name == "kev":
        return float(meta.on_kev)
    if name == "cvss":
        return meta.max_cvss / CVSS_MAX
    if name == "cvss_ge9":
        return float(meta.max_cvss >= CVSS_CRITICAL)
    if name == "log_loss":
        return math.log1p(meta.loss_usd) / LOSS_LOG_SCALE
    return min(meta.n_cves, CVE_COUNT_CAP) / CVE_COUNT_CAP  # n_cves


def feature_vector(
    names: Sequence[str], probs: Mapping[str, float | None], meta: ArticleMeta
) -> list[float]:
    """特徴量の列名の並びどおりに値を作る (学習 ``scripts/train_severity_s3_model.py`` と共有)。

    未知の one-hot の値 (学習になかった category 等) は全部 0 になる。未知の列名は ``ValueError``。
    """
    one_hot = {
        CATEGORY_PREFIX: meta.category,
        TYPE_PREFIX: meta.article_type,
        KIND_PREFIX: meta.event_kind,
    }
    out: list[float] = []
    for name in names:
        if name.startswith(PROBE_PREFIX):
            out.append(_logit(probs.get(name[len(PROBE_PREFIX) :])))
        elif name in NUMERIC_FEATURES:
            out.append(_numeric(name, meta))
        else:
            prefix = next((p for p in one_hot if name.startswith(p)), None)
            if prefix is None:
                raise ValueError(f"未知の特徴量: {name}")
            out.append(1.0 if one_hot[prefix] == name[len(prefix) :] else 0.0)
    return out


@dataclass(frozen=True)
class SeverityCModel:
    """S3 の確率の RF (JSON 木の numpy 推論、sklearn は本番に持ち込まない)。"""

    inner: AssignModel
    probe_model: str
    version: str

    @property
    def threshold(self) -> float:
        return self.inner.threshold

    def probability(self, probs: Mapping[str, float | None], meta: ArticleMeta) -> float:
        x = feature_vector(self.inner.feature_names, probs, meta)
        return forest_probability(self.inner._trees, x)  # noqa: SLF001 — 木の JSON は同じ形


@lru_cache(maxsize=4)
def load_severity_c_model(path: str | None = None) -> SeverityCModel | None:
    """モデルを読む。無い・壊れている・列名が未知 → None (呼び手は記録しない)。"""
    target = Path(path) if path else DEFAULT_MODEL_PATH
    if not target.exists():
        return None
    try:
        raw: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        names = tuple(raw["feature_names"])
        probe_model = str(raw["probe_model"])
        # 未知の列名は推論の時に落ちるので、読む時に検証する
        feature_vector(
            names, {}, ArticleMeta("", "", "", on_kev=False, max_cvss=0.0, loss_usd=0.0, n_cves=0)
        )
        inner = AssignModel(
            feature_names=names, threshold=float(raw["threshold"]), _trees=tuple(raw["trees"])
        )
        return SeverityCModel(inner, probe_model, str(raw.get("model_version", raw.get("version"))))
    except Exception as e:  # noqa: BLE001 — 壊れたモデルで毎時の段を止めない
        _log.warning("severity_c_model_load_failed", path=str(target), error=str(e)[:200])
        return None


def scope_skip_reason(basis: str, event_kind: str) -> str | None:
    """問いを当てない記事の理由 (A の決まりごとが付けない記事)。当てるなら None。

    ``basis`` は ``importance_v2.derive_severity`` の根拠名 (``non_cyber`` / ``derivative`` が対象外)。
    まとめ記事は分類 (``event_kind=roundup``) でも外す。
    """
    from src.eventnews.models import ROUNDUP_KIND

    if basis in ("non_cyber", "derivative"):
        return basis
    if event_kind == ROUNDUP_KIND:
        return SKIP_ROUNDUP
    return None


async def probe_questions(
    *, title: str, published: str | None, body: str, config: Any, client: Any
) -> dict[str, float | None]:
    """9 問の P(はい)。失敗した問いは None (``decision_probe`` が握って None を返す)。"""
    from src.tools.decision_probe import probe_choice
    from src.tools.model_tiers import Step

    out: dict[str, float | None] = {}
    for qid, question in QUESTIONS:
        probs = await probe_choice(
            Step.SEVERITY_AXES,
            build_question(title, published, body, question),
            CHOICES,
            config=config,
            client=client,
        )
        out[qid] = None if probs is None else probs[CHOICES[0]]
    return out

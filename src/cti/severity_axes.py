"""深刻度の軸 — 記事が報じる事象を、決まった選択肢の欄に分解する (2026-09-25)。

背景: triage の重要度は「日本なら規模を問わず最低 medium」の規則で**深刻さと関連性を混ぜて**
いて、medium の 7 割は審判の重要度 0-1 だった (日本も日本以外も同率)。3 段の単語 1 つでは
detect が日本の小さな事案と追うべき事案を分けられない。CTI の標準 (UK NCSC の分類・CISA
NCISS) に倣い、深刻さを**客観的な欄**に分けて持つ。**日本かどうかは入れない** (関連性は
``japan_relevance`` の別軸)。

実測 (data/mlx/severity_*、2026-09-24/25): detect ML に軸を足すと、1 日の開設数を据え置いたまま
精度 54→62%・回収 65→75%・日本の小さな事案の誤開設 45→27 件 (評価 731 件・13 日)。
**ローカル (s17、追加学習なし) の軸で ML を学習・推論して Sonnet の軸とほぼ同等** (AUC 0.919 対
0.922)。s17 は「被害者のいない記事にも被害を付ける」偏りを持つが一貫しており、ML が織り込む。
⚠ そのため **軸を付けるモデルを替えたら detect ML も作り直す** (癖ごと学習している)。
detect_model.json の ``axes_model`` と照合し、食い違えば警告する。

⚠ プロンプトとスキーマは**測定時のまま**。``magnitude`` 欄は特徴量に使わない (LLM は数を取り違えた
— 規模は ``magnitude_log10`` が本文の数字から機械的に取る) が、欄を消すと他の欄の出力が
測定時から動きうるので残している。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

Scope = Literal["no_victim", "single_org", "multi_org_or_provider", "sector_wide", "national"]
Impact = Literal[
    "none", "attempted", "unauthorized_access", "data_exposure", "disruption", "destructive"
]
Confirmation = Literal[
    "confirmed", "claimed_only", "possible", "no_impact_stated", "not_applicable"
]
Magnitude = Literal["unknown", "lt_1k", "1k_100k", "100k_1m", "ge_1m"]
Exploitation = Literal["not_applicable", "disclosed_only", "poc", "exploited_in_wild"]
Actor = Literal[
    "state", "criminal", "hacktivist", "insider_or_accident", "unknown", "not_applicable"
]
Target = Literal[
    "government",
    "critical_infra",
    "defense",
    "ics_ot_product",
    "enterprise_product",
    "general_org",
    "individuals",
    "not_applicable",
]

#: 特徴量に使う欄と選択肢の SSoT (並び = 特徴量の列順。変えたら detect ML を作り直す)。
AXES: dict[str, tuple[str, ...]] = {
    "scope": get_args(Scope),
    "impact": get_args(Impact),
    "confirmation": get_args(Confirmation),
    "exploitation": get_args(Exploitation),
    "actor": get_args(Actor),
    "target": get_args(Target),
}
#: DB に保存する欄 (特徴量に使わない magnitude も監査用に残す)
STORED_FIELDS: tuple[str, ...] = (*AXES, "magnitude")

AXIS_FEATURE_NAMES: tuple[str, ...] = (
    *(f"axis_{k}={o}" for k, opts in AXES.items() for o in opts),
    "axis_magnitude_log10",
)


class SeverityAxes(BaseModel):
    """LLM の出力 (構造化)。選択肢の外は pydantic が弾く。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    scope: Scope
    impact: Impact
    confirmation: Confirmation
    magnitude: Magnitude
    exploitation: Exploitation
    actor: Actor
    target: Target


PROMPT = (
    "あなたは CTI アナリストです。次の記事が報じる事象について、下の 7 つの欄を\n"
    "**記事に書かれた事実だけから**選んでください。推測で埋めず、書かれていなければ unknown / "
    "not_applicable\n"
    "などを選ぶこと。重要かどうか・日本に関係するかは判断しない (別の工程で扱う)。\n"
    "\n"
    "# 記事\n"
    "見出し: {title}\n"
    "要約: {summary}\n"
    "\n"
    "# 欄\n"
    "1. scope (被害の広がり)\n"
    "   - no_victim: 被害者がいない (脆弱性の公表・勧告・研究・政策・統計など)\n"
    "   - single_org: 単一の組織・個人\n"
    "   - multi_org_or_provider: 複数の組織、"
    "または提供者 (SaaS・クラウド・ホスティング・委託先・共通基盤・\n"
    "     広く使われるソフトウェア) を経由して他の組織へ及ぶ。\n"
    "     ⚠ **被害者が提供者自身で、その利用者・顧客のアカウントやデータに影響が及ぶ場合もこれ**\n"
    "     (レンタルサーバ事業者の侵害で利用者のサイトが影響を受けた、など)\n"
    "   - sector_wide: 特定の業界全体に及ぶ\n"
    "   - national: 国家規模・多国に及ぶ (国の基幹サービス・多国にまたがる作戦)\n"
    "2. impact (被害の性質、最も重いもの)\n"
    "   - none: 被害なし / attempted: 攻撃の試行・未遂・フィッシングの送信\n"
    "   - unauthorized_access: 不正アクセス・アカウント乗っ取り・改ざん (漏えいの記述なし)\n"
    "   - data_exposure: 情報の漏えい・流出 / disruption: 業務・サービスの停止\n"
    "   - destructive: ランサムウェアによる暗号化・データの破壊・物理的被害\n"
    "3. confirmation (実害の確認)\n"
    "   - confirmed: 被害者または当局が被害を確認 / claimed_only: 攻撃者の主張のみ\n"
    "   - possible: 可能性がある・調査中 / "
    "no_impact_stated: 影響がない・被害は確認されていないと公表\n"
    "   - not_applicable: 被害者がいない\n"
    "4. magnitude (影響を受けた件数・人数・アカウント数の桁。**複数の数字があれば最大のもの**\n"
    "   (「最大」「可能性」の数も含む)。書かれていなければ unknown)\n"
    "5. exploitation (脆弱性の場合の悪用状況。脆弱性の話でなければ not_applicable)\n"
    "   - disclosed_only: 公表・修正のみ / poc: 実証コードの公開 / "
    "exploited_in_wild: 実際の悪用が観測\n"
    "6. actor (行為者)\n"
    "   - state: 国家・国家支援・APT / criminal: 犯罪集団・ランサムウェア集団\n"
    "   - hacktivist: ハクティビスト / "
    "insider_or_accident: 内部不正・人為的ミス・紛失・設定不備\n"
    "   - unknown: 不明 / not_applicable: 行為者がいない (脆弱性の公表など)\n"
    "7. target (標的・被害者の種類。脆弱性の話なら影響を受ける製品の種類)\n"
    "   - government: 政府機関・自治体・議会 / "
    "critical_infra: 電力・ガス・水道・通信・交通・医療・金融など\n"
    "     重要インフラ事業者 / defense: 軍・防衛関連 / "
    "ics_ot_product: 制御システム (ICS/OT) 製品の脆弱性\n"
    "   - enterprise_product: 企業で広く使われる製品・サービスの脆弱性"
    " (ネットワーク機器・サーバ製品・SaaS 等)\n"
    "   - general_org: 上記以外の企業・団体 / individuals: 個人・消費者 / "
    "not_applicable: 標的がない\n"
)
#: 測定時の入力の上限 (要約) と出力の上限
_SUMMARY_MAX_CHARS = 1500
_MAX_TOKENS = 400

_NUM = re.compile(
    r"([0-9][0-9,\.]*)\s*(万|億)?\s*(?:件|人|名|アカウント|レコード|ファイル|組織|社)"
)
_UNIT = {"万": 1e4, "億": 1e8}


def build_prompt(title: str, summary: str) -> str:
    return PROMPT.format(title=title, summary=(summary or "")[:_SUMMARY_MAX_CHARS])


async def classify_axes(llm: LLMClient, title: str, summary: str) -> SeverityAxes | None:
    """記事 1 本の軸。失敗は None (呼び手を止めない。欠測は特徴量で全 0 になる)。"""
    try:
        return await llm.generate_structured(
            build_prompt(title, summary),
            SeverityAxes,
            temperature=0.0,
            max_tokens=_MAX_TOKENS,
            think=False,
        )
    except Exception as exc:  # noqa: BLE001 — 軸の失敗で detect / 毎時の段を止めない
        _log.warning("severity_axes_classify_failed", error=type(exc).__name__)
        return None


def magnitude_log10(text: str) -> float:
    """本文中の件数・人数の最大の桁 (log10)。数が無ければ 0。

    LLM に数を読ませない — 試行で「最大 120 万アカウント」を別の数字 (951) で答えた。
    """
    best = 0.0
    for m in _NUM.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        v *= _UNIT.get(m.group(2) or "", 1.0)
        best = max(best, math.log10(v + 1))
    return best


def axis_feature_vector(axes: Mapping[str, str] | None, text: str) -> list[float]:
    """軸 → 特徴量 (one-hot + 規模)。順序は ``AXIS_FEATURE_NAMES`` と 1:1。

    軸が無い記事 (未分類・失敗) は one-hot を全 0 にする (規模は本文から取れるので付ける)。
    """
    one_hot = [
        1.0 if axes is not None and axes.get(k) == o else 0.0
        for k, opts in AXES.items()
        for o in opts
    ]
    return [*one_hot, magnitude_log10(text)]

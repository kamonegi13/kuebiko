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
import os
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
#: s23 で足す欄 (2026-10-08、本文入力時のみ LLM が埋める)。
#: 設計: docs/importance_relevance_redesign.md §4.1・docs/research/llm_training/
#: next_models_s22_n20.md §9.3
VictimSize = Literal["large", "medium", "small", "none", "unknown"]
Recoverability = Literal["regular", "extended", "not_recoverable", "not_applicable", "unknown"]

#: 特徴量に使う欄と選択肢の SSoT (並び = 特徴量の列順。変えたら detect ML を作り直す)。
AXES: dict[str, tuple[str, ...]] = {
    "scope": get_args(Scope),
    "impact": get_args(Impact),
    "confirmation": get_args(Confirmation),
    "exploitation": get_args(Exploitation),
    "actor": get_args(Actor),
    "target": get_args(Target),
}
#: DB に保存する欄 (特徴量に使わない magnitude も監査用に残す)。s21 以前の 7 欄 + 1 — 変えない
#: (ML の特徴量名の SSoT なので、s23 の新欄は別に ``EXTRA_FIELDS`` で持つ)
STORED_FIELDS: tuple[str, ...] = (*AXES, "magnitude")

AXIS_FEATURE_NAMES: tuple[str, ...] = (
    *(f"axis_{k}={o}" for k, opts in AXES.items() for o in opts),
    "axis_magnitude_log10",
)

#: s23 で本文から足す 5 欄 (2026-10-08)。``AXES_FROM_BODY=1`` のときだけ LLM が埋める。
#: 欠測は DB に NULL のまま保存する (false に既定すると消費者が「無い」と読んでしまう)
EXTRA_FIELDS: tuple[str, ...] = (
    "recent_action",
    "victim_size",
    "recoverability",
    "credential_compromise",
    "distribution_compromise",
)
#: EXTRA_FIELDS のうち真偽値 (DB は INTEGER 0/1/NULL。``get_severity_axes`` は "true"/"false" の
#: 文字列で返す)
BOOL_EXTRA_FIELDS: frozenset[str] = frozenset(
    {"recent_action", "credential_compromise", "distribution_compromise"}
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
    # s23 の 5 欄 (本文入力時のみ埋まる。要約入力 (既定) では prompt が聞かないので常に None)
    recent_action: bool | None = None
    victim_size: VictimSize | None = None
    recoverability: Recoverability | None = None
    credential_compromise: bool | None = None
    distribution_compromise: bool | None = None


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
    "     ⚠ **同じ攻撃・事案が複数の組織に及ぶときだけ**。互いに関係の示されない別々の\n"
    "     単独事案を 1 本にまとめた記事は、最も重い 1 件で選ぶ (single_org)。摘発・起訴の\n"
    "     記事で触れる過去の被害は数えない (2026-10-08)\n"
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

#: s23 で足す欄の定義文 (2026-10-08)。data/mlx/build_axes_teacher.py FIELDS のコード所有コピー ——
#: 教師も同じ定義文で付けているため (axes_teacher_v2 以降)、文言を変えるときは両方を直す
EXTRA_FIELDS_PROMPT = (
    "8. recent_action (直近の行動。true/false)\n"
    "   見出しや冒頭が、記事の公開日から 7 日以内の具体的な行動 (いつ・どこで・誰が) を報じて\n"
    "   いるか。書かれていなければ false\n"
    "9. victim_size (被害組織の規模)\n"
    "   large: 大企業・大規模組織・中央省庁 / medium: 中堅の組織・地方自治体 / "
    "small: 小規模な組織・個人\n"
    "   none: 被害組織がいない / unknown: 書かれていない\n"
    "10. recoverability (回復の見込み、NCISS の考え方)\n"
    "   regular: 通常の手順で回復 / extended: 長期間・外部の支援が要る / "
    "not_recoverable: 回復できない\n"
    "   (漏えいしたデータの公開など) / not_applicable: 被害なし / unknown: 書かれていない\n"
    "11. credential_compromise (管理者などの認証情報・鍵・トークンが盗まれたか。true/false)\n"
    "12. distribution_compromise (配布経路の汚染。true/false)\n"
    "   正規のソフトウェア・パッケージの公式な配布経路 (リリースの仕組み・更新サーバ・正規の\n"
    "   パッケージの公開アカウント) から悪性の版が配られたことが本文で確認できるか。名前を似せた\n"
    "   偽のパッケージ・攻撃者が自分で公開したパッケージは false\n"
)

#: 本文の入力上限 (教師作成時と同じ値、data/mlx/build_axes_teacher.py の BODY_MAX)
BODY_MAX_CHARS = 12000
#: 本文入力版の出力上限 (7 欄 → 12 欄で増える分、要約版 (400) より少し多く取る)
_BODY_MAX_TOKENS = 550

#: 本文入力版の prompt (``AXES_FROM_BODY=1``)。既存 7 欄の定義文は ``PROMPT`` と同一の文字列を
#: 再利用する (DRY — 教師・既存の判定基準と文言をずらさない)
PROMPT_BODY = (
    "あなたは CTI アナリストです。次の記事が報じる事象について、下の 12 の欄を\n"
    "**記事に書かれた事実だけから**選んでください。推測で埋めず、書かれていなければ unknown / "
    "false /\n"
    "not_applicable などを選ぶこと。重要かどうか・日本に関係するかは判断しない "
    "(別の工程で扱う)。\n"
    "\n"
    "# 記事\n"
    "見出し: {title}\n"
    "本文:\n"
    "{body}{cut}\n"
    "\n"
    "# 欄\n" + PROMPT.split("# 欄\n", 1)[1] + EXTRA_FIELDS_PROMPT
)

_AXES_FROM_BODY_FLAG = "AXES_FROM_BODY"


def axes_from_body_enabled() -> bool:
    """``AXES_FROM_BODY=1`` のときだけ本文入力の prompt を使う (既定 0 = 要約のまま)。"""
    return os.environ.get(_AXES_FROM_BODY_FLAG, "0") == "1"


def build_prompt(title: str, summary: str) -> str:
    return PROMPT.format(title=title, summary=(summary or "")[:_SUMMARY_MAX_CHARS])


def build_body_prompt(title: str, body: str) -> str:
    """本文入力版の prompt。本文は教師作成時と同じ上限で切る (``BODY_MAX_CHARS``)。"""
    text = body or ""
    cut = "" if len(text) <= BODY_MAX_CHARS else "\n(本文はここで切れています)"
    return PROMPT_BODY.format(title=title, body=text[:BODY_MAX_CHARS], cut=cut)


async def classify_axes(
    llm: LLMClient, title: str, summary: str, body: str = ""
) -> SeverityAxes | None:
    """記事 1 本の軸。失敗は None (呼び手を止めない。欠測は特徴量で全 0 になる)。

    ``AXES_FROM_BODY=1`` かつ ``body`` が渡されたときだけ本文入力の prompt (s23、
    12 欄) を使う。既定 (0、または body なし) は見出し+要約のまま
    (``build_prompt`` と byte-identical — 今日の本番挙動を変えない)。
    """
    use_body = axes_from_body_enabled() and bool(body)
    prompt = build_body_prompt(title, body) if use_body else build_prompt(title, summary)
    max_tokens = _BODY_MAX_TOKENS if use_body else _MAX_TOKENS
    try:
        return await llm.generate_structured(
            prompt,
            SeverityAxes,
            temperature=0.0,
            max_tokens=max_tokens,
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

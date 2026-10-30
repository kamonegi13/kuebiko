"""地政学の救済の意味の判定 (GEO-RESCUE-JUDGED v2、2026-10-30)。

**本番の救済は変えず、影の記録 (``GEO_RESCUE_JUDGED_SHADOW=1``、``geo_rescue_shadow``) でだけ使う**
(2026-10-31 の設計の見直し: replay の測定をやめ、本番の候補で記録して後で盲検の標本と比べる)。
flat が救わず pir が救った記事にだけ、
(1) 語の関門 (``geo_rescue_cues.geo_cue_reasons``、決定論) を当て、手掛かりが書かれていれば
(2) 救済と同じモデル (``Step.GEO_RESCUE_JUDGE`` → ``STEP_INHERITS`` で ``step:triage_geo_rescue``
の割当) に狭い問いを logprob で問い (``decision_probe.probe_choice``、生成しない)、
**手掛かりの種類に応じた問いのどれかが Yes** なら救う::

    rescued = flat_rescued OR (pir_rescued AND (
        (geo_cue AND (QW OR QC)) OR (cyber_geo_cue AND QD)))

geo_cue = 定義 A〜C の手掛かり (注視国・代理勢力・戦域・枠組み 等)、cyber_geo_cue = 定義 D の
手掛かり (``geo_rescue_cues.is_cyber_geo_cue``)。両方ある記事には 3 問とも当てる。

- QW (``Q_WATCHED_ACTION``) = 主に報じているのが、注視国 (またはその側の勢力) の安全保障上の
  行動・決定か、注視国が当事者の戦争での相手側の軍事行動か。国内の政治・社会・文化、
  経済・貿易・産業政策、儀礼的な外交、発言・論評だけなら No
- QC (``Q_ALLIED_RESPONSE``) = 主に報じているのが、日本・同盟国・同志国・台湾の決定・調達・
  配備・演習等で、**記事自身が**理由・目的・場所として注視国・その行為・抑止の枠組み・戦域を
  書いているものか。書いていない一般の調達・試験・産業の話なら No

- QD (``Q_CYBER_GEO``) = 主に報じているのが、国家のサイバー戦略・態勢の決定、国家 (またはその
  側の勢力) の影響工作、重要インフラへの破壊工作・物理またはハイブリッドの攻撃の、新しい決定・
  行動・事案か。企業の製品・契約、一般の犯罪・脆弱性、発言・論評だけなら No

**QD を D の手掛かりのある記事だけに当てる理由** (全件に OR で足さない): v1 は「書かれていない
結び付きをモデルが推す」ことで誤った。QD を手掛かりの無い一般の軍事の記事にも当てると、
「この軍の近代化はサイバーと地政学の連関か」を推させることになり同じ型の誤りを招く。D の語が
書かれていることを前提にすれば、QD は「その語が中心の新しい出来事か」だけを問えばよい。
D の記事は注視国の名前を持たないことがある (同盟国の国家サイバー戦略など) ので、D の手掛かりは
A〜C の手掛かりとは独立に関門を開ける。

v1 (Q1 = 新しい事実か ∧ Q2 = 注視国か同盟国の対応か) は誤った救済 22.0% で不合格だった。
Q1 は両方の正解でほぼ全件 Yes (AUC 0.51) で何も絞らず、誤りの 65% は「結び付きの書かれていない
同盟国の一般の軍事」を Q2 が地理から推して通したもの、19% は注視国の国内・経済の話を
「注視国の行動」として通したもの (data/eval/geo_rescue_judged_2026_10_30/error_analysis.md)。
v2 は前者を語の関門で、後者を QW の除外の文で受ける。新しい事実の条件は別の問いにせず、
各問の除外の文 (発言・論評だけなら No) に入れた。

しきい値は**固定の 0.5** (事前登録 ``docs/replay_prereg/geo_rescue_shadow.md`` で宣言)。
``probe_thresholds.json`` には入れない (較正していない)。判定が読めない (``None``) 問いは No
(救わない = flat の判定に戻る側)。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.pipeline.geo_rescue_cues import geo_cue_reasons, is_cyber_geo_cue
from src.tools.decision_probe import probe_choice
from src.tools.model_tiers import Step

if TYPE_CHECKING:
    from src.config_loader import AppConfig

#: 3 問とも P(Yes) がこの値以上で Yes (事前登録で固定、標本で調整しない)
GEO_JUDGE_THRESHOLD = 0.5
#: 選択肢 (各 1 トークンの英語の語。日本語の「いいえ」は複数トークンに分かれる)
GEO_JUDGE_CHOICES: tuple[str, str] = ("Yes", "No")
_YES = GEO_JUDGE_CHOICES[0]

#: QW (事前登録に原文のまま載せる。変えるなら事前登録の変更履歴に書く)
Q_WATCHED_ACTION = (
    "質問: この記事が主に報じている出来事は、次のどちらかか。"
    "(1) 中国・ロシア・北朝鮮・イランの政府・軍・情報機関、またはその側に立つ勢力"
    " (フーシ派・ヒズボラ・イラクの親イラン民兵・ワグネルやアフリカ軍団・"
    "親ロシアのハクティビストや破壊工作の網・中国の海上民兵や影響工作の網・"
    "北朝鮮の IT 労働者や外貨稼ぎの網など) が行った安全保障上の行動・決定。"
    "軍事行動・演習・配備、威嚇や報復の予告、武器や軍事技術の移転"
    " (注視国の兵器が他国に渡った・配備されたことの確認を含む)、制裁や報復の輸出規制などの"
    "経済的な威圧、影響工作、サイバー作戦、破壊工作、日本・同盟国・台湾への圧力を指す。"
    "(2) 注視国が当事者の戦争での、相手側の軍事行動 (反撃・防空・攻撃)。"
    "注視国の国内の政治・司法・人権・社会・文化の話、威圧や報復を伴わない経済・貿易・投資・"
    "産業政策の話、儀礼的な外交や会談の予定、発言・論評・見通しの紹介だけ"
    " (威嚇や報復の予告を除く) なら No と答える。"
)
#: QC (同上)
Q_ALLIED_RESPONSE = (
    "質問: この記事が主に報じている出来事は、日本・同盟国・同志国 (NATO・EU・G7・Quad の加盟国)・"
    "台湾の政府・軍の決定・調達・配備・演習・訓練・組織改編・支援・制裁・規制で、"
    "しかも記事自身が、その理由・目的・場所として次のどれかを書いているものか。"
    "(a) 中国・ロシア・北朝鮮・イラン、またはその側の勢力"
    " (b) それらによる侵入・領空侵犯・無人機の飛来・攻撃・威嚇"
    " (c) それらへの抑止・対抗の枠組み (AUKUS・NATO の東側の抑止や前方展開・"
    "日米・米韓・日米比の抑止・対ロシアのウクライナ支援など)"
    " (d) 台湾海峡・東シナ海・南シナ海・朝鮮半島・バルト海・黒海・紅海・湾岸での配備・監視・演習。"
    "記事が理由・目的・場所として書いていない一般の調達・契約・生産・試験・組織改編・予算や、"
    "企業の製品・工場の話なら、国や地域の名前が出ていても No と答える。"
)
#: QD (同上。定義 D = サイバーと地政学の連関)
Q_CYBER_GEO = (
    "質問: この記事が主に報じている出来事は、"
    "サイバーと国家の安全保障が結び付く次のどれかについての、新しい決定・行動・事案か。"
    "(1) 国家 (政府・軍) のサイバー戦略・サイバー作戦の態勢・"
    "ドクトリンの決定や変更 (2) 国家またはその側に立つ勢力による影響工作・偽情報の作戦"
    " (3) 重要インフラ (海底ケーブル・電力網・パイプライン・通信など) への破壊工作、"
    "または物理的・ハイブリッドの攻撃や脅威。"
    "企業の製品・契約・資金調達の話、国家と結び付かない一般のサイバー犯罪や脆弱性の話、"
    "発言・論評・見通しの紹介だけで新しい決定や事案が無いなら No と答える。"
)

#: 本文として使う最小の字数 (これ未満は「本文なし」。事前登録 §4)
MIN_BODY_CHARS = 200
#: 同意 (cookie) の画面の語 (事前登録の追補。冒頭だけ見る。ボット確認の語は本番の
#: ``briefing._BLOCK_PAGE_RE`` をそのまま使う)
_CONSENT_PAGE_RE = re.compile(
    r"we value your privacy|accept all cookies|cookie (?:settings|preferences|consent)|"
    r"manage (?:your )?consent|before you continue to|consent to the use of cookies",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GeoJudgeResult:
    """語の関門の手掛かりと 3 問の P(Yes)。問わなかった・読めなかった問いは ``None``。"""

    cues: tuple[str, ...]
    p_watched_action: float | None
    p_allied_response: float | None
    p_cyber_geo: float | None = None

    @property
    def has_cue(self) -> bool:
        return bool(self.cues)

    @property
    def has_geo_cue(self) -> bool:
        """定義 A〜C の手掛かり (QW・QC に回す) があるか。"""
        return any(not is_cyber_geo_cue(c) for c in self.cues)

    @property
    def has_cyber_geo_cue(self) -> bool:
        """定義 D の手掛かり (QD に回す) があるか。"""
        return any(is_cyber_geo_cue(c) for c in self.cues)

    @property
    def watched_action(self) -> bool:
        return _is_yes(self.p_watched_action)

    @property
    def allied_response(self) -> bool:
        return _is_yes(self.p_allied_response)

    @property
    def cyber_geo(self) -> bool:
        return _is_yes(self.p_cyber_geo)

    @property
    def passed(self) -> bool:
        """手掛かりの種類に応じた問いのどれかが Yes。読めない問いは No とみなす。"""
        geo = self.has_geo_cue and (self.watched_action or self.allied_response)
        return geo or (self.has_cyber_geo_cue and self.cyber_geo)

    @property
    def failed(self) -> bool:
        """当てるべき問いが読めなかった (通信失敗・選択肢が見つからない等)。"""
        geo = self.has_geo_cue and (self.p_watched_action is None or self.p_allied_response is None)
        return geo or (self.has_cyber_geo_cue and self.p_cyber_geo is None)


def _is_yes(p: float | None) -> bool:
    return p is not None and p >= GEO_JUDGE_THRESHOLD


def judged_rescued(*, flat_rescued: bool, pir_rescued: bool, judge: GeoJudgeResult) -> bool:
    """最終判定 = ``flat_rescued OR (pir_rescued AND judge.passed)`` (本番と測定で共通)。"""
    return flat_rescued or (pir_rescued and judge.passed)


def judge_prompt(title: str, text: str, question: str) -> str:
    """判定の入力 = 題 + triage と同じ本文プレビュー (呼び出し側が冒頭 1,500 字に切る) + 問い。"""
    return f"タイトル: {title}\n\n本文:\n{text}\n\n{question}"


async def _p_yes(prompt: str, config: AppConfig) -> float | None:
    probs = await probe_choice(Step.GEO_RESCUE_JUDGE, prompt, GEO_JUDGE_CHOICES, config=config)
    return None if probs is None else probs.get(_YES)


async def _maybe(
    ask: bool, title: str, text: str, question: str, config: AppConfig
) -> float | None:
    return await _p_yes(judge_prompt(title, text, question), config) if ask else None


async def judge_geo_rescue(
    title: str, text: str, *, config: AppConfig, probe_all: bool = False
) -> GeoJudgeResult:
    """1 記事に語の関門と、手掛かりの種類に応じた問いを当てる。

    本番 (``probe_all=False``) は手掛かりの無い問いを問わない (手掛かりが 1 つも無ければ LLM を
    呼ばない)。測定は ``probe_all=True`` で 3 問とも読む (分析のため。最終判定は同じ式)。
    """
    cues = geo_cue_reasons(title, text)
    geo = probe_all or any(not is_cyber_geo_cue(c) for c in cues)
    cyber = probe_all or any(is_cyber_geo_cue(c) for c in cues)
    pw, pa, pd = await asyncio.gather(
        _maybe(geo, title, text, Q_WATCHED_ACTION, config),
        _maybe(geo, title, text, Q_ALLIED_RESPONSE, config),
        _maybe(cyber, title, text, Q_CYBER_GEO, config),
    )
    return GeoJudgeResult(cues=cues, p_watched_action=pw, p_allied_response=pa, p_cyber_geo=pd)


def unusable_body_reason(body: str | None) -> str | None:
    """本文が判定の入力に使えない理由 (使えれば ``None``)。決定論 (事前登録の追補の入力の決まり)。

    ``empty`` (200 字未満) / ``feed_boilerplate`` / ``block_page`` / ``consent_page``。
    ボット確認の画面と配信の定型文は本番の ``_degenerate_body_reason`` と同じ正規表現・同じ範囲
    (薄い本文の関門は環境変数で変わるため使わない)。正解の入力と判定の入力で同じ関数を使う。
    """
    # 循環 import を避けて遅延 import (briefing は pipeline の多くを読む)
    from src.pipeline.briefing import (
        _BLOCK_PAGE_MAX_LEN,
        _BLOCK_PAGE_RE,
        _BLOCK_PAGE_SCAN_CHARS,
        _DEGENERATE_BODY_MIN_CHARS,
        _FEED_BOILERPLATE_RE,
    )

    text = (body or "").strip()
    if len(text) < MIN_BODY_CHARS:
        return "empty"
    match = _FEED_BOILERPLATE_RE.search(text)
    if match and len(text[: match.start()].strip()) < _DEGENERATE_BODY_MIN_CHARS:
        return "feed_boilerplate"
    if len(text) > _BLOCK_PAGE_MAX_LEN:
        return None
    head = text[:_BLOCK_PAGE_SCAN_CHARS]
    if _BLOCK_PAGE_RE.search(head):
        return "block_page"
    if _CONSENT_PAGE_RE.search(head):
        return "consent_page"
    return None


__all__ = [
    "GEO_JUDGE_CHOICES",
    "GEO_JUDGE_THRESHOLD",
    "MIN_BODY_CHARS",
    "Q_ALLIED_RESPONSE",
    "Q_CYBER_GEO",
    "Q_WATCHED_ACTION",
    "GeoJudgeResult",
    "judge_geo_rescue",
    "judge_prompt",
    "judged_rescued",
    "unusable_body_reason",
]

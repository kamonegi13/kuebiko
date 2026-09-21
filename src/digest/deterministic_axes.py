"""深掘り rubric のうち、**計算で決まる軸**をコードで出す (2026-09-21)。

⭐ **LLM に聞くべきでないものを聞いていた**。教師 1,260 行の実測で timeliness は
74% が満点 5・σ0.60 と、composite の重み 0.20 を持ちながら選抜にほぼ寄与して
いなかった。日付で計算した値との一致は 6.1%。

4 軸のうち何が計算で決まるかを分けると:

| 軸 | 判定に要るもの | 出どころ |
|---|---|---|
| pir | 文面の解釈 (SIR への該当) | **LLM** |
| roi | 文面の解釈 (情報密度・一次情報か) | **LLM** |
| timeliness | 公開からの日数・今週速報した actor か | **コード** |
| novelty | 既出か (dedup/entity 履歴) + 新事実か | **コード + LLM** |

⚠ novelty を全部コードにはできない。anchor の 1-4 は「新 IoC」「新 TTP」「重要な続報」
といった**中身の判断**で、既出かどうか (0 と 5) だけが機械で決まる。
ここでは**確定する端点だけ**を返し、中間は LLM に残す (不確かなものを機械が
決めたことにしない)。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

#: timeliness の日数境界 (anchor の文言に対応)。
#: 3 = 今週公開 / 2 = 1-2 週前の続報 / 1 = 1 ヶ月超の古い事案
_THIS_WEEK_DAYS = 7
_RECENT_DAYS = 21
_STALE_DAYS = 30

#: 今週 brief/alert で速報した actor に触れている記事は anchor 5 (深い背景)。
_BRIEFED_ACTOR_SCORE = 5.0
#: 同じ dedup cluster が候補内に複数ある = 同時期の関連事案。anchor 4。
#: ⚠ **「候補の誰かと actor が重なる」を代用にしてはいけない** (2026-09-21 に実装して
#:   失敗した)。候補 180 件の actor 和集合はほぼ全 APT 記事に当たり、無差別に 4 が付く。
#:   結果 actor 名の付かない記事 (脆弱性の実悪用・国内侵害) が構造的に 1 点低くなり、
#:   VMware vCenter の実悪用や国内被害が選抜から押し出された。
_RELATED_SCORE = 4.0


def parse_ts(value: str | None) -> datetime | None:
    """ISO 文字列 → aware datetime (失敗は None)。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def timeliness_score(
    *,
    created_at: str | None,
    window_end: datetime,
    article_actors: Iterable[str] = (),
    briefed_actors: Iterable[str] = (),
    dedup_key: str | None = None,
    cohort_dedup_keys: Iterable[str] = (),
) -> float | None:
    """時事性を計算で出す (純粋関数)。判定材料が無ければ None。

    Args:
        created_at: 記事の取込時刻
        window_end: 窓の終端 (この時点から見た古さを測る)
        article_actors: その記事に紐づく actor
        briefed_actors: **今週 brief/alert に出た記事の** actor (anchor 5 の条件)。
            実測で候補の 6-24% が該当し、選択性がある
        dedup_key: その記事の dedup cluster
        cohort_dedup_keys: 同じ窓に 2 件以上ある dedup cluster (anchor 4 の条件)
    """
    ts = parse_ts(created_at)
    if ts is None:
        return None
    mine = {a for a in article_actors if a}
    if mine & {a for a in briefed_actors if a}:
        return _BRIEFED_ACTOR_SCORE
    days = (window_end - ts).total_seconds() / 86400
    if days <= _THIS_WEEK_DAYS:
        # 今週公開。同じ事案が候補内に複数あるなら関連事案として 4、単独なら 3。
        related = bool(dedup_key) and dedup_key in {k for k in cohort_dedup_keys if k}
        return _RELATED_SCORE if related else 3.0
    if days <= _RECENT_DAYS:
        return 2.0
    if days <= _STALE_DAYS:
        return 2.0
    return 1.0


def novelty_floor(*, dedup_key: str | None, selected_keys: Iterable[str]) -> float | None:
    """確定する端点だけ返す (純粋関数)。中間は LLM に残すので None。

    - 過去 4 週に同じ dedup cluster を選定済 → **0 で確定** (anchor 0 そのもの)
    - それ以外 → None (「既出だが新事実か」は中身を読まないと決まらない)

    ⚠ 現行のプールは選定済 dedup を**上流で除外している**ため 0 は実際には
    発生しない (教師 1,260 行で 0 件)。除外を外したときに効く保険として置く。
    """
    if dedup_key and dedup_key in {k for k in selected_keys if k}:
        return 0.0
    return None

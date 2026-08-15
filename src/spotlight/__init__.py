"""PIR Spotlight: PIR を切り口とした縦断 narrative。

global synthesis (P+M+E+S+I+T 横断) と補完関係:
- global: 戦略レベル、cross-axis 連鎖、上位 stakeholders 説明用
- spotlight: 戦術 - 作戦レベル、1 PIR 領域の深掘り、analyst 作業用

config/delivery/pir.yaml で spotlight.enabled=true な PIR each に対し、
weekly cron で narrative を生成して spotlight 表に永続化。
UI Synthesis tab の sub-tab で表示。

主要モジュール:
    models     SpotlightRecord (Pydantic + DB row)
    generator  PIR + 直近 matches → LLM narrative
    runner     pipeline runner (cron / 手動 trigger 共通)
"""

from src.spotlight.models import KeyEvent, SpotlightRecord
from src.spotlight.runner import SpotlightRunResult, run_pir_spotlights

__all__ = ["KeyEvent", "SpotlightRecord", "SpotlightRunResult", "run_pir_spotlights"]

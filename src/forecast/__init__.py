"""Phase 4 (将来予測 / Forecasting)。

5 方向ミッションで唯一欠けていた「次に何が起こりうるか」を、決定的 (再現可能) な
時系列分析で支える。LLM の prose 予測ではなく、actor / intent / IOC の活動を週次
バケットに落とし、分散考慮 spike (FC3) / トレンド (FC4) / 相関 (FC5) を算出する。
synthesis には前期総括を注入 (FC1)、spike から導いた監視指標を翌週に的中検証する
accountability ループ (FC2) を持つ。
"""

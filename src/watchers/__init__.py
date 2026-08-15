"""Watchers: 主パイプライン (RSS / Grok) 経由で扱えないソース専用の監視器。

標準の feed parser が扱えない broken feed (例: 13MB malformed Atom) や、
そもそも RSS を提供しないソースを、HTML スクレイピング等で個別に取得し、
直接 Discord に投稿する仕組み。

設計方針:
    - 主パイプラインと **責務を分離**: 要約 / triage / dedup を共有しない
      (各 watcher が独自に "新規かどうか" を判定し直接 post)
    - 状態は ``data/<watcher>_seen.json`` に永続化
    - 暴走防止のため 1 回あたりの post 上限を必ず設定
    - 各 watcher は ``run_check()`` を export し、scheduler から呼ばれる

将来検討:
    - N=2 になったら watcher Protocol を導入して共通化
    - pipelines.yaml と並列の watchers.yaml で設定駆動化
    - run_history テーブルに watcher 用の status 列を追加
"""

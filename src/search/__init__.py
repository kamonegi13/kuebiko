"""検索改善 (hybrid retrieval + LLM rerank + LLM query planning)。

意味検索が「中国の通信事業者侵入」に農業窃取記事をヒットさせた問題への対処。
- B (retriever): vector + keyword + structured を RRF 融合 (recall)。
- C (reranker): 候補を LLM が関連度採点・並べ替え (precision、決定打)。
- D (planner): NL クエリを構造化プランに翻訳 (soft-boost = 加点のみ、ゼロ件事故なし)。
全層 fail-safe で degrade (LLM 不調→hybrid→vector→keyword)。
"""

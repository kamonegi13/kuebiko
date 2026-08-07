"""本文の保存上限 — 検出・保存・表示の整合を規定する定数 (2026-08-06)。

``MAX_STORED_BODY_CHARS`` は「DB (articles.body) に保存し UI に表示する本文」の
上限であると同時に、actor / IoC 検出の走査範囲でもある (**保存 ⊇ 検出の保証**)。

旧上限 20k 字は検出 (全文走査) と保存 (20k 切り詰め) が不一致で、表示本文の
どこにも存在しない entity (「言及された組織・関係者」の幽霊 actor) を生んでいた
(全件監査 2026-08-06)。100k は実測で全記事の 98% 超をカバーし、超過するのは
CISA/Siemens ICS advisory 等の列挙型 (実測最大 757k 字) のみ。DB 増分は
+数十 MB 程度 (90 日 retention で頭打ち・TOAST 圧縮あり) で許容範囲。

LLM 要約 (briefing.MAX_LLM_BODY_CHARS) / 本文翻訳 (チャンク resumable) 等の
**消費者側の上限は各消費者が持つ** — 本定数は保存と検出の整合だけを担う。
"""

MAX_STORED_BODY_CHARS = 100_000

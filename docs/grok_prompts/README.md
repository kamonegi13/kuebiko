# Grok Tasks Prompts (Phase Diamond Grok Redesign)

## 概要

xAI Grok Tasks の 2 slot を使った X-only signal collection 用 prompts。
Phase Diamond で旧 state_apt / x_early_signals を完全置換し、Inoreader RSS と
構造的に被らない X 固有 CTI signal を JSONL 形式で収集する設計に再構築。

## Slot 構成

| slot | task name | prompt 文書 | 役割 |
|---|---|---|---|
| 1 | x_early_signals (置換) | [slot1_x_native_signal_v3.1.md](slot1_x_native_signal_v3.1.md) | Global B/F focus (leak sites, KEV, vendor teaser, law enforcement) |
| 2 | state_apt (置換) | [slot2_jp_asia_signal_v2.md](slot2_jp_asia_signal_v2.md) | JP / East Asia focus (官公庁, 日本研究者, DPRK, 中国 APT) |

## 設計判断

### なぜ旧 state_apt / x_early_signals を置換したか

旧 prompts の根本問題:
- **Inoreader 重複**: state_apt が出す APT 主要キャンペーン分析は Mandiant / CrowdStrike /
  Microsoft Threat Intel / JPCERT が数時間〜1 日で必ず一次ソースを出す → Inoreader と重複
- **narrative 生成依存**: Grok 側で日本語要約・カテゴリ集計・narrative を作らせていたため、
  parsing 困難 + Grok の自由度高すぎて再現性低い
- **enum 判定 Grok 側**: signal_type / corroboration / engagement_signal の enum 判定を
  Grok にやらせていたが毎日カテゴリ名が揺らいで pipeline が壊れる

新 prompts の方針:
- **Inoreader が構造的に取れない領域** に絞り込み (leak sites, victim self-disclosure,
  vendor X-first teaser, KEV alerts, IoC dumps 等)
- **JSONL raw 出力**: narrative なし、per-tweet record、format strict
- **enum 判定は後段**: Grok は matched_theme (A-F / J1-J6) の 1 文字のみ、複雑な
  semantic 分類は pipeline 側で

### 4 回の prompt iteration history (slot 1)

| version | 主な変更 | 件数 | 主な発見 |
|---|---|---|---|
| v1 | 初版 6 theme (A-F) | 11 | format 100%、Theme A 学者を誤分類、F に generic CVE 混入 |
| v2 | A handle 厳格化、F 具体 event 化、JP source 強化 | 11 | F precision 100%、JP source 出現、Theme F の Drupal KEV 系正解 |
| v3 | B diversity cap 4、件数下限 15、engagement floor 例外なし | 8 | B diversity 改善、件数 regression、engagement 違反 (Grok 限界) |
| **v3.1** | B cap 6 (中間)、engagement filter 削除 (pipeline 化)、件数下限撤廃 | 11 | production-ready、diversity 維持 + 件数回復 |

### 2 回の prompt iteration history (slot 2)

| version | 主な変更 | 件数 | 主な発見 |
|---|---|---|---|
| v1 | 初版 6 theme (J1-J6) | 6 | ScanNetSecurity 4/6 (Inoreader 重複疑い)、警察庁 特殊詐欺を J2 誤分類 |
| **v2** | news aggregator cap 2、J2 cyber keyword 必須 | 4 | 質的大成功 (松井証券 0day + Kimsuky IoC dump 捕捉) |

## Grok の本質的限界 (実証された)

prompt iteration 6 回 (slot 1: 4 + slot 2: 2) を通じて見えた Grok の限界:

1. **engagement floor を厳密 enforce しない**: 全 iteration で違反 record 混入
2. **件数下限を能動的に追求しない**: 「下限 15 必達」と書いても 8 件で停止
3. **C/D/E theme の探索が薄い**: explicit keyword 投入しても 0 件続出
4. **media_urls / external_urls の正当性は検証困難**: fabricate 不安定

→ これら **prompt で解決不可能** な限界は downstream pipeline で吸収する設計に。

## Production yield 期待値

| | slot 1 | slot 2 | 合計 |
|---|---|---|---|
| 件数/日 (中央値) | 10-15 | 3-7 | **13-22 件** |
| 質的 hit (週次) | 0day teaser / KEV 速報 / 特異 leak | 0day disclosure / IoC dump / Asia event | 週 1-3 件 |
| Inoreader 被り率 | 低 (FalconFeeds 系) | 中 (一部 official + aggregator 残り) | 平均 20-30% |

旧 state_apt + x_early_signals の **narrative 出力 5-15 件/日 (大半 Inoreader 重複)**
と比較して **質量とも明確に優位**。

## Downstream Pipeline 設計 outline

新 prompt の JSONL 出力を消化する pipeline 要件 (実装は別 session で予定):

### 1. JSONL Parser
- 1 行 1 record の JSONL を parse
- Schema 検証 (pydantic) → 不正 record は skip + log
- Code block wrap や前後説明文の defensive 除去

### 2. Filter
- **engagement filter**: B は `like + retweet < 1` で drop、A/C/D/E/F は `< 3` で drop
- 24h 範囲外を drop
- lang 不明確な低 engagement record を drop

### 3. Dedup
- tweet_id 一意
- URL 完全一致 (slot 1 と slot 2 の cross-slot dedup)
- victim 組織名 + actor 名 fuzzy match (同事案の複数報道集約)
- 既存 `dedup_seen_urls` 表との結合

### 4. IoC Extraction
- `src/cti/ioc_extractor.py` を活用
- skocherhan 系の IoC dump から大量 domain/IP/hash 抽出 → 自動 CTI DB enrichment

### 5. Theme Routing

slot 1:
- A (vendor teaser) → alert ch
- B (leak site) → watch ch (high volume)
- C (法執行) → alert ch
- D (CISO 自己開示) → alert ch
- E (scan velocity) → brief ch
- F (KEV/ITW/PoC) → alert ch

slot 2:
- J1 (日本企業 incident) → japan_watch ch (or alert if importance high)
- J2 (JPCERT/IPA cyber alert) → alert ch
- J3 (日本研究者 technical) → watch ch (詳細解析は通読用)
- J4 (DPRK) → alert ch
- J5 (中国 APT) → alert ch
- J6 (APAC regional) → brief ch

### 6. Persistence
- articles 表に `source_pipeline="grok_x_native"` (slot 1) or `"grok_jp_asia"` (slot 2)
- dedup_key は url または event_hash

### 7. URL 検証
- media_urls / external_urls の HEAD check
- 404 / fabricated URL は record に warning flag

### 実装規模見積もり

- `src/grok/jsonl_parser.py` 新規: ~150 lines
- `src/grok/jsonl_to_briefings.py` 新規: ~150 lines
- `src/grok/task_def.py` 拡張: ~50 lines (新 task type 追加)
- routing logic: ~100 lines
- tests: ~200 lines
- 合計: **~750 lines、1-2 日の作業量**

## 運用フロー (両 slot 投入後)

1. **06:00 JST**: xAI 側で 2 task が並行 fire (Tasks の自動 scheduling)
2. **06:05-06:15 JST**: Grok が JSONL output を生成 → email 送信
3. **06:25 JST**: IMAP poll で email 受信
4. **06:30 JST**: 既存 grok-briefing pipeline 起動
   - 新 task type を識別して JSONL parser 経路へ
   - parse → filter → dedup → route → Discord
5. **06:35 JST**: Discord channel に投稿完了 (alert / watch / brief / japan_watch)

## 観察期間 (1-2 週間後の判断)

集計 metric:
- 件数振れ幅 (中央値 ±30% 以内が望ましい)
- Inoreader 重複率 (30% 以下が望ましい)
- 質的 hit 件数 (週 1+ が望ましい)
- format compliance (100% 維持)

これらが満たされれば production 継続。1 つでも崩れたら prompt 再調整。

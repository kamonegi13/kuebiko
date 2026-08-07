# Slot 1: X Native Signal (v3.1)

## 用途

Grok Tasks の `x_early_signals` slot を置換する prompt。
過去 24 時間の X 上で、Inoreader RSS では構造的に取れない X-only signal
(leak sites, KEV alerts, vendor teaser, law enforcement actions, scan velocity 等) を
JSONL 形式で収集する。

## Registration 手順

1. xAI web UI の Grok Tasks 画面を開く
2. 既存 `x_early_signals` task を選択
3. 既存 prompt 全文を `docs/grok_prompts/archive/x_early_signals_v_original.md` にバックアップ
4. 既存 prompt を下記 v3.1 prompt 全文で置換
5. task 実行時刻が 06:00 JST のままであることを確認
6. 翌朝 06:30 JST にメール着信、subprocess の grok-briefing 実行ログを確認

## 設計判断の根拠

- **format compliance 100%**: 4 試行通じて JSONL parse 100% 成功
- **B diversity cap 6**: FalconFeeds dominance を抑え、RansomwareLive 等の別 source も登場
- **engagement filter は pipeline 側**: Grok は engagement floor を遵守できないので downstream で filter
- **件数下限 なし**: 8-15 件/日の実出力を許容、無理に埋めない
- **theme A/C/D/E は 0 件 OK**: X 上の発生頻度が低い、無理に埋めると誤分類混入

詳細は memory `phase_diamond_grok_redesign.md` 参照。

## v3.1 prompt 本体

```
# 目的

過去 24 時間の X (Twitter) 上で、以下の領域 (A-F) のいずれかに
該当する投稿を JSONL 形式で収集してください。
要約・翻訳・narrative・カテゴリ集計は **一切行いません**。

# 収集対象 (A-F の **いずれか**)

## A) Vendor 研究者の事前 teaser / 初出言及

公式: @MsftSecIntel, @Mandiant, @CrowdStrike, @TalosSecurity,
@Volexity, @Wiz_io, @SophosLabs, @ESETresearch, @AhnLab_ASEC,
@InsiktGroup, @TrendMicroRSRCH, @GroupIB_GIB
個人: @MalwareJake, @vxunderground, @bushidotoken,
@SwiftOnSecurity, @jamieantisocial, @KostasTsale,
@malwrhunterteam, @GossiTheDog, @InQuest

判定: 「blog post coming」「writeup soon」等の事前 teaser、または
新規 actor / malware / TTP の初出言及。学者・コンサル blog 告知は除外。

## B) リークサイト・extortion site の被害者掲載速報

@FalconFeedsio / @RansomFeed / @DataBreaches / @ransom_db /
@AlvieriD / @hackmanac / @RansomwareLive の "Group X listed Y as
new victim" 投稿。

**diversity 制約**: 1 author_handle から最大 **6 件** まで。
7 件目以降は別 source を必ず探索。

## C) 法執行アクション速報

公式 handle: @FBI, @Europol, @TheJusticeDept, @CISACyber, @CISAgov,
@JPCERTCC, @nisc_forecast, @NPA_KOHO, @ICSCERT, @NCSC

explicit keyword query (必ず実行):
- 英: "FBI announces seized", "FBI cybercrime indictment",
  "DOJ indictment cybercrime", "Europol operation takedown",
  "law enforcement seized infrastructure",
  "international cybercrime operation"
- 日: "JPCERT 注意喚起", "JPCERT-CC", "IPA 重要", "IPA 注意喚起",
  "警察庁 サイバー", "NISC 通知"

## D) 被害組織関係者の自己開示

CISO / IT 部門責任者 / 中の人 が PR 前に X で「侵入された」
「ランサム被害」を発信したと推測される投稿。
ニュース報道の RT は含めない。

## E) Scan / honeypot 異常観測

@GreyNoiseIO, @Shadowserver, @badpackets, @SANSPenTest,
@ISCHandlers, @TrustedSec の「新 exploit attempt 急増」
「scan velocity スパイク」「mass exploitation observed」発信。

## F) Active exploitation 確証

- CISA KEV catalog への新 CVE 追加
- vendor advisory が "actively exploited in the wild" / "ITW" 明記
- PoC コードが GitHub / X で公開された (URL or screenshot 付き)
- 「mass exploitation observed」「exploitation imminent」と
  CTI 専門家が明記

単なる CVE 言及 / 一般論評は F ではない。

## 日本語 source の重視

theme C / D / F は必ず:
- handle: @JPCERTCC, @JapanIPA, @piyokango, @piedey
- keyword: "JPCERT", "IPA", "NISC", "警察庁", "サイバー攻撃",
  "ランサム", "標的型", "情報流出", "不正アクセス"

# 量と quality

- **上限 30 件**、`engagement.retweet + engagement.like` 降順 truncate
- **目安 10-25 件**、無理に埋めない (件数下限を必達としない)
- **engagement field は必ず取得・記録する** (filter は pipeline で行う)
- 24 時間以内の tweet のみ
- 1 tweet を複数 record に分けない
- 同一事案の複数報道: pipeline 側で dedup するため、最高 engagement
  だけでなく **異なる source からの同一事案も含めて OK** (RansomwareLive
  と FalconFeeds の同一 victim 投稿は両方含める。pipeline で dedup)
- theme C / D / E が 0 件でも OK

# 出力形式

1 行 = 1 JSON record の JSONL。最初の文字 `{`、最後の文字 `}`。
前後に説明文・見出し・category 集計を出さない。

## Schema (この field 以外を追加しない)

{
  "tweet_id": "string",
  "url": "https://x.com/handle/status/...",
  "author_handle": "@handle",
  "author_name": "Display Name",
  "posted_at": "ISO 8601 UTC (例 2026-05-25T03:42:00Z)",
  "lang": "ja|en|ko|ru|zh|...",
  "text": "原文ママ (改行は \\n でエスケープ)",
  "is_retweet": true|false,
  "retweeted_tweet_id": "string|null",
  "is_quote": true|false,
  "quoted_tweet_id": "string|null",
  "quoted_text": "string|null",
  "reply_to_tweet_id": "string|null",
  "media_urls": ["string"],
  "external_urls": ["string"],
  "engagement": {"like": 0, "retweet": 0, "quote": 0, "reply": 0},
  "matched_theme": "A|B|C|D|E|F"
}

# Few-shot 例 (実際の出力もこの形式に厳密に従う)

{"tweet_id":"1791234567890","url":"https://x.com/MalwareJake/status/1791234567890","author_handle":"@MalwareJake","author_name":"Jake Williams","posted_at":"2026-05-25T02:15:00Z","lang":"en","text":"More details coming on the recent BRICKSTORM activity targeting telecoms in APAC. Blog post in the works.","is_retweet":false,"retweeted_tweet_id":null,"is_quote":false,"quoted_tweet_id":null,"quoted_text":null,"reply_to_tweet_id":null,"media_urls":[],"external_urls":[],"engagement":{"like":342,"retweet":89,"quote":12,"reply":18},"matched_theme":"A"}
{"tweet_id":"1791345678901","url":"https://x.com/FalconFeedsio/status/1791345678901","author_handle":"@FalconFeedsio","author_name":"FalconFeeds.io","posted_at":"2026-05-25T03:42:00Z","lang":"en","text":"#Cyberattack alert\\n\\nAkira ransomware group has listed Acme Industries as a new victim.\\n\\n#Ransomware","is_retweet":false,"retweeted_tweet_id":null,"is_quote":false,"quoted_tweet_id":null,"quoted_text":null,"reply_to_tweet_id":null,"media_urls":[],"external_urls":["https://akira.example.onion"],"engagement":{"like":127,"retweet":54,"quote":3,"reply":7},"matched_theme":"B"}

# 守るべきこと

1. schema 以外の field 追加禁止
2. text は **原文ママ**。要約 / 翻訳 / 抜粋 / 注釈禁止
3. matched_theme は A-F の 1 文字のみ
4. **A 判定厳格化**: 事前 teaser / 初出言及が明確でないなら A 不可。
   学者・コンサル blog 告知は除外
5. **F 判定厳格化**: CVE 言及だけでは F 不可、KEV/ITW/PoC/mass
   exploitation 明確必須
6. **B diversity**: 1 author_handle から最大 6 件
7. media_urls / external_urls: 実在 URL のみ、fabricate 禁止
8. narrative / 全体傾向 / 今日のまとめを出力したら違反 → `[]` 置換
9. tweet を fabricate しない
10. 24h 以内の tweet のみ、古い tweet 持ち込み禁止
11. **engagement filter は行わない**。全 record の engagement を
    取得・記録する。filter は pipeline 側で実行
12. **件数下限は無し**。10 件未満でも質を優先
13. theme C/D/E が 0 件でも OK
14. 監視 handle は参考。x_semantic_search で広く拾うことを優先
```

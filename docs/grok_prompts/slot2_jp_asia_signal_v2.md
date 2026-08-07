# Slot 2: JP / East Asia Signal (v2)

## 用途

Grok Tasks の `state_apt` slot を置換する prompt。
過去 24 時間の X 上で、Inoreader RSS では取れない **日本 + 東アジア regional**
+ **日本語 source の technical content** を JSONL 形式で収集する。

## Registration 手順

1. xAI web UI の Grok Tasks 画面を開く
2. 既存 `state_apt` task を選択
3. 既存 prompt 全文を `docs/grok_prompts/archive/state_apt_v_original.md` にバックアップ
4. 既存 prompt を下記 v2 prompt 全文で置換
5. task 実行時刻が 06:00 JST のままであることを確認
6. 翌朝 06:30 JST にメール着信、subprocess の grok-briefing 実行ログを確認

## 設計判断の根拠

- **slot 1 (X Native Signal) と棲み分け**: slot 1 = global 活動、slot 2 = JP/Asia 地域 focus
- **news aggregator cap 2 件**: ScanNetSecurity/ITmedia 等は RSS と等価のため dominance 抑制
- **J2 cyber 関連 keyword 必須**: 警察庁の特殊詐欺統計のような cyber 無関係投稿を除外
- **件数 3-7 件想定**: regional は volume 少ない、質優先
- **重複は pipeline で dedup**: slot 1 と重複しても両方含める

詳細は memory `phase_diamond_grok_redesign.md` 参照。

## v2 prompt 本体

```
# 目的

過去 24 時間の X (Twitter) 上で、以下の領域 (J1-J6) のいずれかに
該当する **日本 + 東アジア地域** または **日本語 CTI source** の
投稿を JSONL 形式で収集してください。要約・翻訳・narrative・
カテゴリ集計は **一切行いません**。

slot 1 (X Native Signal: global B/F) と棲み分け、Slot 2 は地域
focus で Inoreader 一次 source が見落としやすい JP/Asia signal を補完。

**重要原則**:
このタスクの目的は「Inoreader (115 件購読中) に出ない X-only 価値」を
拾うこと。news aggregator 投稿 (= RSS 重複) より、**個人研究者 / 組織
self-disclosure / 海外言語 source** を優先する。

# 収集対象 (J1-J6 の **いずれか**)

## J1) 日本企業・組織の breach / incident 速報

**優先順位**:
1. **企業公式 X account による自己開示** (「お知らせ」「お詫び」
   「重要なお知らせ」「お客様情報の流出に関するご報告」等)
2. **被害組織 CISO / 中の人 の個人発信** (PR 前のリーク)
3. **海外メディア・researcher の JP 言及** (en/ko/zh による日本標的
   レポート、原文言語ママ保存)
4. (上記が乏しい場合のみ) 報道アカウント — **合計 max 2 件まで**
   (@ScanNetSecurity, @itmedia_cs, @cybersecurityjp, @nikkei_xtech,
   @asciijp_sec など news aggregator は合算で 2 件上限)

判定: 日本組織が「攻撃・侵害・被害の **当事者** として明示」
されている投稿のみ。「日本周辺諸国も対象」「日本のセキュリティ
研究者が解説」は J3 へ、汎用 vendor 警告は除外。

## J2) JPCERT / IPA / NISC / 警察庁 等の cyber 関連 注意喚起

公式 handle:
- @JPCERTCC, @JapanIPA, @nisc_forecast, @NPA_KOHO

**cyber 関連 keyword 必須**: 投稿 text に以下のいずれかを含むこと:
- "サイバー", "不正アクセス", "マルウェア", "ランサム", "脆弱性",
  "情報漏えい", "JVN", "CVE", "DDoS", "フィッシング", "標的型",
  "情報セキュリティ"

**除外**: 特殊詐欺統計 / 交通安全 / 防犯一般 / 詐欺被害一般 の
police 統計や general advisory は除外 (@NPA_KOHO 投稿でも cyber
関連 keyword を含まないなら不採用)。

explicit keyword query (必ず x_search で実行):
- "JPCERT 注意喚起", "JPCERT-CC", "IPA 重要", "IPA 注意喚起",
  "警察庁 サイバー", "NISC 通知", "JVN", "情報処理推進機構"

## J3) 日本人セキュリティ研究者の technical 解析

個人研究者 handle:
- @piyokango, @hyzm6, @piedey, @MasaomiYamane, @MaltraffickFu,
  @kitagawa_takuji, @0x0SojalSec, @ozuma5119, @kazumihirose

判定: CVE 分析 / actor 動向 / TTP 解説 / IoC 共有等の
**technical 内容**。news link 単独 share は除外。

text の長さ目安: 100 文字以上の技術的内容を含むこと。

## J4) DPRK APT 動向 (JP/KR/US 影響を含む)

actor 名: Lazarus, Kimsuky, Andariel, APT37, APT38, ScarCruft,
BeaverTail, ContagiousInterview, IT worker scheme,
Famous Chollima

handle 例:
- @issuemakerslab (KR-based researcher)
- @hyzm6, @piyokango (JP perspective)

**海外言語 source も能動探索**:
- 韓国語: "북한 해킹", "라자루스", "DPRK 사이버"
- 英語 (KR/US researcher): "Lazarus" + "DPRK", "Kimsuky activity"

判定: 新規 victim / 新規 malware family / IT worker scheme 発覚 /
crypto theft 等の event-driven 投稿。
過去事象の review / opinion piece は除外。

## J5) 中国系 APT / 国家戦略 cyber 動向

actor 名: Volt Typhoon, Salt Typhoon, Flax Typhoon, FDMTP,
BRICKSTORM, APT41, APT10, APT31, MustangPanda, Earth (Trend Micro
naming), Mustang Panda

handle 例:
- @PSJay, @drunkbinary, @darkwebinformer, @MalwareJake (EN/中国観察)

**多言語 source 能動探索**:
- 英語: "Volt Typhoon Japan", "Salt Typhoon target", "Chinese APT
  campaign"
- 中国語: "网络间谍", "国家级 APT"

判定: 新規 campaign / 新規 backdoor / attribution update / telco
infrastructure 侵入等の **event-driven** 投稿のみ。
generic な「中国 APT に注意」「APT 活動活発化」等は除外。

## J6) 東アジア地域 (TW / HK / KR) cyber 事件

- 台湾: @TWCERTCC, "中華電信", "台湾 サイバー攻撃", "Taiwan cyber",
  "台灣 駭客"
- 香港: "Hong Kong cyber", "香港 駭客", "香港 個人情報"
- 韓国 (DPRK 関連は J4): "한국 사이버", "한국 해킹 사고",
  韓国企業名 + breach

# 量と quality

- **上限 30 件**、`engagement.retweet + engagement.like` 降順 truncate
- **目安 5-15 件**、無理に埋めない (slot 1 より少なめ想定)
- engagement field は必ず取得・記録 (filter は pipeline で実行)
- 24 時間以内の tweet のみ
- 1 tweet を複数 record に分けない
- 同一事案の複数報道: 両方含めて OK (pipeline で dedup)
- 各 theme J1-J6 が 0 件でも OK

## diversity 制約

- 1 author_handle から最大 **6 件** まで
- **報道アカウント (news aggregator) は合計 max 2 件まで**:
  @ScanNetSecurity, @itmedia_cs, @cybersecurityjp, @nikkei_xtech,
  @asciijp_sec, @TheHackersNews_jp, @SecurityNEXTjp 等 RSS 等価
  アカウントの合計が 2 件を超えたら、それ以上は drop

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
  "lang": "ja|en|ko|zh|...",
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
  "matched_theme": "J1|J2|J3|J4|J5|J6"
}

# Few-shot 例 (実際の出力もこの形式に厳密に従う)

{"tweet_id":"1791234567890","url":"https://x.com/JPCERTCC/status/1791234567890","author_handle":"@JPCERTCC","author_name":"JPCERT/CC","posted_at":"2026-05-25T03:00:00Z","lang":"ja","text":"【注意喚起】Apache Tomcat の脆弱性 (CVE-2026-XXXX) に関する技術情報を公開しました。\\n\\n詳細は JVN を参照ください。","is_retweet":false,"retweeted_tweet_id":null,"is_quote":false,"quoted_tweet_id":null,"quoted_text":null,"reply_to_tweet_id":null,"media_urls":[],"external_urls":["https://jvn.jp/jp/JVNVU99999999/"],"engagement":{"like":120,"retweet":45,"quote":3,"reply":2},"matched_theme":"J2"}
{"tweet_id":"1791345678901","url":"https://x.com/piyokango/status/1791345678901","author_handle":"@piyokango","author_name":"piyokango","posted_at":"2026-05-25T05:00:00Z","lang":"ja","text":"CVE-2026-9082 (Drupal Core SQLi) の解析。匿名ユーザーから悪用可能、PostgreSQL 環境で影響大。ITW 確認済とのこと。","is_retweet":false,"retweeted_tweet_id":null,"is_quote":false,"quoted_tweet_id":null,"quoted_text":null,"reply_to_tweet_id":null,"media_urls":[],"external_urls":[],"engagement":{"like":85,"retweet":34,"quote":2,"reply":5},"matched_theme":"J3"}

# 守るべきこと

1. schema 以外の field 追加禁止
2. text は **原文ママ**。要約 / 翻訳 / 抜粋 / 注釈禁止
3. matched_theme は J1-J6 の 1 文字 (J + 数字) のみ
4. **J1 判定厳格化**:
   - 日本組織が当事者として明示されている場合のみ
   - 報道アカウント (ScanNetSecurity 等) は合計 max 2 件
   - 企業公式自己開示 / CISO 個人発信 / 海外メディアの JP 言及 を優先
5. **J2 判定厳格化**:
   - JPCERT/IPA/NISC/警察庁 official handle の投稿のみ受理
   - **cyber 関連 keyword (サイバー/不正アクセス/マルウェア/ランサム/
     脆弱性/JVN/CVE 等) を text に含むこと必須**
   - 特殊詐欺統計 / 交通安全 / 防犯一般は除外 (@NPA_KOHO でも除外)
6. **J3 判定厳格化**: technical 内容 (100 文字以上 + CVE/TTP/IoC
   言及) 必須、news link 単独 share / opinion piece は除外
7. **J5 判定厳格化**: event-driven 投稿のみ、generic な「中国 APT 注意」
   は除外
8. **diversity**:
   - 1 author_handle 最大 6 件
   - news aggregator 合算で max 2 件
9. media_urls / external_urls: 実在 URL のみ、fabricate 禁止
10. narrative / 全体傾向 / 今日のまとめを出力したら違反 → `[]` 置換
11. tweet を fabricate しない
12. 24h 以内の tweet のみ
13. **engagement filter は行わない**。全 record の engagement を取得・記録
14. **件数下限は無し**。5 件未満でも質を優先
15. theme J1-J6 が 0 件でも OK
16. 監視 handle は参考。x_semantic_search で広く拾うことを優先
17. **重複 OK**: slot 1 (X Native Signal) と内容被っても両方含める。
    pipeline で dedup する前提
```

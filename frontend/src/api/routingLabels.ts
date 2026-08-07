// routing ルールの内部 id → ユーザー向け表示ラベル (案 B / Stage 3)。
// 保存される値は id のまま。UI 表示だけ日本語化し、id は括弧で併記してトレーサビリティを保つ。
import { vocabLabel } from "../hooks/useVocab";

const FLAG_LABELS: Record<string, string> = {
  kev: "KEV・実環境で悪用中",
  zero_day: "ゼロデイ",
  japan_critical: "日本の重要インフラ標的",
  japan_relevant: "日本関連の脅威",
  japan_mentioned: "日本への言及 (一般)",
  known_apt: "既知 APT への言及",
  apt_leak: "APT 内部情報のリーク",
  security_relevant: "セキュリティ関連 (広義)",
  confidence_all_low: "信頼度がすべて低い",
  llm_breaking_critical: "LLM が速報級と判定",
  llm_japan_targeted: "LLM が日本標的と判定",
  brief_cap_reached: "朝刊(brief)が1日の上限に到達",
};

// チャンネル名のラベルはレジストリ (components/channel.tsx の useChannelMeta /
// ChannelChip) を唯一の SSoT とする。custom channel も追従し built-in 改名にも
// 強い。ここに固定マップを置くと stale 化する (旧 grok_daily 残存・ops 欠落の前例)
// ため routingLabel.channel/channelShort は廃止した。

const OP_LABELS: Record<string, string> = {
  in: "いずれかに一致",
  not_in: "いずれにも一致しない",
  eq: "等しい",
  ne: "等しくない",
  in_config: "設定リストに含む",
  // 語彙拡張 ①: 数値演算子 (max_cvss 用)。
  gte: "以上 (≧)",
  gt: "より大きい (>)",
  lte: "以下 (≦)",
  lt: "より小さい (<)",
  // 語彙統一: boolean プロパティの真偽。
  is_true: "はい",
  is_false: "いいえ",
};

const FIELD_LABELS: Record<string, string> = {
  article_type: "記事の形式",
  importance: "重要度",
  stance: "論調",
  category: "カテゴリ",
  // 語彙拡張 ① (回収)。
  victim_sector: "被害セクター",
  actor_nation: "脅威アクター国家",
  max_cvss: "CVSS 最大値",
  // 語彙拡張②。
  keyword_list: "キーワードリスト",
};

// 脅威アクターの sponsoring nation コード → 表示名 (actor_aliases.yaml の nation 値)。
// nation コードは小文字 (cn/ru/…)、country vocab キーは大文字 ISO2 なので toUpperCase() して引く。
// 専用 nation vocab は無く country vocab を再利用する (全 nation を被覆)。

// 記事形式 (article_type) / カテゴリ (category) のラベルは backend 配信 vocab を SSoT に
// 解決する (vocabLabel)。以前ここにあったローカル写像は labels.ts の複製で訳語が
// ドリフトしていたため撤去。

const CONFIG_LIST_LABELS: Record<string, string> = {
  high_threat_brief_categories: "高脅威カテゴリ (設定で管理)",
};

function valueShort(field: string, id: string): string {
  if (field === "article_type") return vocabLabel("article_type", id);
  if (field === "importance") return vocabLabel("importance", id);
  if (field === "category") return CONFIG_LIST_LABELS[id] ?? vocabLabel("category", id);
  if (field === "actor_nation") return vocabLabel("country", id.toUpperCase());
  return id;
}

export const routingLabel = {
  op: (id: string) => OP_LABELS[id] ?? id,
  // プロパティ id → ラベル (boolean=旧flag + str/set/number)。編集 UI は vocab catalog の
  // ラベルを使うが、要約 (summarizeWhen) は vocab なしで呼ばれるためここの静的ラベルで
  // 表示する (表示専用、SSoT は backend の PROPERTY_CATALOG)。
  property: (id: string) => FIELD_LABELS[id] ?? FLAG_LABELS[id] ?? id,
  // property の値ラベル (要約・選択肢表示用、短縮)。
  value: (field: string, id: string): string => valueShort(field, id),
};

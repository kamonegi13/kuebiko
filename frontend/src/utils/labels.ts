// 記事まわりの内部値 → 日本語表示ラベルの SSoT (2026-07-21)。
//
// 経緯: 2026-07-20 の文言平易化でラベル (見出し) は日本語化したが、**値** の側は
// 各ファイルがそれぞれローカル写像を持っていたため、訳語が既に食い違っていた
// (stance の opinion が「意見」と「論説」、unknown が「不明」と「未判定」など)。
// 同じ enum の訳は 1 箇所に置き、各画面はここから import する。
//
// **チャンネル名はここに置かない**。チャンネルはレジストリ (components/channel.tsx の
// useChannelMeta / ChannelChip) が唯一の SSoT で、custom channel と built-in の改名に
// 追従する。固定マップを置くと stale 化する (routingLabels.ts の同趣旨コメント参照)。
//
// 未知キーは必ず原値に fallback する (`label(MAP, v)`)。実データには legacy 値
// (grok_x_signal_* / recap / opinion 等) が残っており、写像漏れで空欄になると
// 「値が無い」のか「訳が無い」のか区別できなくなるため。

/** 写像に無いキーは原値をそのまま返す (空欄化を防ぐ)。 */
export function label(map: Record<string, string>, value: string | null | undefined): string {
  if (!value) return "";
  return map[value] ?? value;
}

// 以下は backend 配信 vocab (hooks/useVocab.ts の useVocab / useVocabMap / vocabLabel) へ
// 移行済み。static マップを再生しないこと (docs/vocabulary_label_architecture.md)。
//   importance / run_status / stance / article_status / trigger / health_status /
//   category / category_group / confidence

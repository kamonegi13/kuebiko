import { describe, expect, it } from "vitest";
import { holdingItems } from "./holdings";

const stats = { runs: 7, articles: 40, run_logs: 900, dedup_seen_urls: 50, article_embeddings: 25, file_size_bytes: 3 };

describe("Intelligence Holdings の項目", () => {
  it("運用画面は日本語の名前で出し、誤った容量と記録の行数は出さない", () => {
    const labels = holdingItems(stats, false).map((x) => x.label);
    expect(labels).toEqual(["収集した記事", "意味検索の対象", "確認済みの URL", "処理の実行"]);
  });

  it("拡張は分析の蓄積 (収集した記事) だけ", () => {
    expect(holdingItems(stats, true)).toEqual([{ label: "収集した記事", value: 40 }]);
  });

  it("生のキー名は出さない", () => {
    const labels = holdingItems({ ...stats, unknown_table: 1 }, false).map((x) => x.label);
    expect(labels.some((l) => l.includes("_"))).toBe(false);
  });
});

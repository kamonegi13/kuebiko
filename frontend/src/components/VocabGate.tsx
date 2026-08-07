// boot gate (docs/vocabulary_label_architecture.md §4.2)。
// 語彙 (/api/v1/vocabularies) の初回取得が解決するまで本体描画を待たせ、
// 記事一覧などで「fetch 前に生の high/posted が一瞬出る」first-paint フラッシュを防ぐ。
// localhost では sub-10ms のため体感ゼロ。取得失敗時は degradation して先へ進む
// (ラベルは原値/humanizer に fallback。アプリは止めない)。
import type { ReactNode } from "react";

import { Spinner } from "./Spinner";
import { useVocabGate } from "../hooks/useVocab";

export function VocabGate({ children }: { children: ReactNode }) {
  const { ready } = useVocabGate();
  if (!ready) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-1 text-fg-muted">
        <span className="inline-flex items-center gap-2 text-sm">
          <Spinner size="md" />
          読み込み中…
        </span>
      </div>
    );
  }
  return <>{children}</>;
}

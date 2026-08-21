// 記事サイドピーク (ArticlePeek) の開閉状態。articleId が SSoT (null = 閉)。
// ArticlePeekHost の component ローカル state に置かず共有 store にしたのは、
// AppShell のプルリフレッシュ (peek 表示中はメイン側 PTR を無効化する) など、
// ArticlePeekHost の外からも「今 peek が開いているか」を参照する必要があるため。

import { create } from "zustand";

interface ArticlePeekStore {
  articleId: string | null;
  open: (articleId: string) => void;
  close: () => void;
}

export const useArticlePeekStore = create<ArticlePeekStore>((set) => ({
  articleId: null,
  open: (articleId) => set({ articleId }),
  close: () => set({ articleId: null }),
}));

// 共有 QueryClient。main.tsx の Provider と、render 外 (純ヘルパ) から cache を読む
// 非フック アクセサ (hooks/useVocab.ts の vocabLabel 等) が同一インスタンスを使う。
import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

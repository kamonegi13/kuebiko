// 共有 QueryClient。main.tsx の Provider と、render 外 (純ヘルパ) から cache を読む
// 非フック アクセサ (hooks/useVocab.ts の vocabLabel 等) が同一インスタンスを使う。
import { QueryClient } from "@tanstack/react-query";

/** 静的配信 (公開サイト / 写し) か。取得の相手が API ではなくファイルになる。 */
const STATIC_DELIVERY =
  import.meta.env.VITE_PUBLIC_STATIC === "1" || import.meta.env.VITE_MIRROR === "1";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // ⚠ タブに戻ったときに取り直すか。
      //
      // 運用画面 (ローカル) では **false**。裏で開きっぱなしのタブが戻るたびに
      // 重い API を叩くと、DB 接続を食う (2026-08-25 に実際にアプリを止めた)。
      //
      // 公開サイト・写しでは **true**。配信しているのは静的 JSON で、更新が
      // なければ 304 で終わる。false のままだと「裏にいる間は間隔が止まり、
      // 戻ってきても取り直さない」ので、次の周期まで古い記事が出続ける
      // (2026-08-29 利用者指摘。再読込すれば最新が出るのがその証拠)。
      refetchOnWindowFocus: STATIC_DELIVERY,
      retry: 1,
    },
  },
});

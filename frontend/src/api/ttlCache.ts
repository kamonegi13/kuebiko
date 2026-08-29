/** 期限付きで約束を握る。静的配信の索引・一覧の使い回し用。
 *
 *  ⚠ **無期限に握ってはいけない。** 1 度読んだきり同じ約束を返し続けると、
 *  画面が取り直しても通信が起きず、再読込するまで永久に古い内容が出る
 *  (2026-08-29 の「自動で更新されない」の正体)。公開サイトと写しの両方に
 *  同じ作りがあったので、**ここ 1 箇所** に寄せる。
 */
export function ttlCached<T>(load: () => Promise<T>, ttlMs: number): () => Promise<T> {
  let held: Promise<T> | null = null;
  let heldAt = 0;
  return () => {
    const now = Date.now();
    if (!held || now - heldAt > ttlMs) {
      heldAt = now;
      const pending = load();
      // 失敗した約束は握らない。持ち続けると以後ずっと失敗を返す。
      pending.catch(() => {
        if (held === pending) held = null;
      });
      held = pending;
    }
    return held;
  };
}

/** 静的配信の既定の保持時間。書き出しは 3 時間ごとなので、それより短く持つ。
 *  配信は etag 付きで、更新が無ければ 304 で終わる。 */
export const STATIC_TTL_MS = 5 * 60 * 1000;

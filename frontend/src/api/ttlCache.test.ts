import { describe, expect, test, vi } from "vitest";

import { ttlCached } from "./ttlCache";

describe("期限付きの保持", () => {
  test("期限内は取りに行かない", async () => {
    const load = vi.fn(async () => 1);
    const get = ttlCached(load, 60_000);
    await get();
    await get();
    expect(load).toHaveBeenCalledTimes(1);
  });

  // ⚠ これが無いと、画面が取り直しても通信が起きず、再読込するまで
  //    永久に古い内容が出る (2026-08-29 に公開サイトで実際に起きた)。
  test("期限を過ぎたら取り直す", async () => {
    vi.useFakeTimers();
    try {
      const load = vi.fn(async () => 1);
      const get = ttlCached(load, 60_000);
      await get();
      vi.advanceTimersByTime(61_000);
      await get();
      expect(load).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  // 失敗した約束を握り続けると、以後ずっと失敗を返す。
  test("失敗は握らない", async () => {
    let n = 0;
    const load = vi.fn(async () => {
      n += 1;
      if (n === 1) throw new Error("落ちた");
      return n;
    });
    const get = ttlCached(load, 60_000);
    await expect(get()).rejects.toThrow("落ちた");
    await expect(get()).resolves.toBe(2);
  });
});

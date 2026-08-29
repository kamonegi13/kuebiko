import { describe, expect, test } from "vitest";

import { canEditOperationalConfig, type RuntimeFlags } from "./useRuntimeFlags";

const f = (o: Partial<RuntimeFlags> = {}): RuntimeFlags => ({
  read_only: false,
  authenticated: false,
  auth_available: false,
  remote_write: false,
  ...o,
});

// ⚠ 画面が決めるのは「出すか隠すか」だけ。実際に通るかはサーバ側の名簿が決める。
//    ここが true でもサーバが拒めば 403 になる (それが正しい順序)。
describe("運用設定を変更できるか", () => {
  test("ローカル (full instance) は常に変更できる", () => {
    expect(canEditOperationalConfig(f())).toBe(true);
  });

  test("公開面で未認証なら変更できない", () => {
    expect(canEditOperationalConfig(f({ read_only: true, remote_write: true }))).toBe(false);
  });

  test("公開面で認証済みでも、遠隔 write が閉じていれば変更できない", () => {
    expect(canEditOperationalConfig(f({ read_only: true, authenticated: true }))).toBe(false);
  });

  test("公開面で認証済み かつ 遠隔 write が開いていれば変更できる", () => {
    expect(
      canEditOperationalConfig(f({ read_only: true, authenticated: true, remote_write: true })),
    ).toBe(true);
  });
});

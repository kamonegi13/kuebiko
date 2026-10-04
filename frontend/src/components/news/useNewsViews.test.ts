// read-only / 403 フォールバックの回帰テスト (2026-10-04 追補)。
// 公開 read-only instance は ops ビルドで動くため VITE_MIRROR では判定できず、
// PUT /api/v1/news-views は REMOTE_WRITE_ALLOWLIST 未登録で常に 403 になる。
// 保存をサイレントに失わないこと (localStorage へ退避 + notice) を確認する。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { useNewsViews } from "./useNewsViews";

const LOCAL_KEY = "news-views:user";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useNewsViews — read-only instance の PUT 403 フォールバック", () => {
  it("runtime-flags が read_only=false でも PUT が 403 なら localStorage へ退避し notice を出す", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runtime-flags")) {
        return jsonResponse({ read_only: false, authenticated: false, auth_available: false, remote_write: false });
      }
      if (url.includes("/api/v1/news-views")) {
        if (init?.method === "PUT") return jsonResponse({ detail: "forbidden" }, false, 403);
        return jsonResponse({ views: [] }); // GET
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useNewsViews(), { wrapper });

    await act(async () => {
      result.current.saveView("自分のビュー", { category: "vuln" });
    });

    await waitFor(() => {
      expect(result.current.userViews.some((v) => v.label === "自分のビュー")).toBe(true);
    });
    expect(result.current.notice).toBe("サーバへの保存に失敗したため、この端末に保存しました");

    // localStorage に実際に残っている (保存をサイレントに失っていない)
    const stored = JSON.parse(localStorage.getItem(LOCAL_KEY) ?? "[]") as { label: string }[];
    expect(stored.some((v) => v.label === "自分のビュー")).toBe(true);
  });

  it("runtime-flags が read_only=true のときは PUT を試みず最初から localStorage へ保存する", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/v1/runtime-flags")) {
        return jsonResponse({ read_only: true, authenticated: false, auth_available: true, remote_write: false });
      }
      if (url.includes("/api/v1/news-views") && init?.method === "PUT") {
        throw new Error("PUT should not be attempted when read_only=true");
      }
      return jsonResponse({ views: [] });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useNewsViews(), { wrapper });

    // runtime-flags (read_only=true) の取得が反映されるのを待つ (forceLocal が
    // 確定する前に保存すると、まだ false のまま PUT を試みてしまう)。
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/api/v1/runtime-flags"))).toBe(true);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    await act(async () => {
      result.current.saveView("端末だけのビュー", { category: "apt" });
    });

    await waitFor(() => {
      expect(result.current.userViews.some((v) => v.label === "端末だけのビュー")).toBe(true);
    });
    expect(result.current.notice).toBe("この端末に保存しました");
    expect(
      fetchMock.mock.calls.some(
        ([, init]) => (init as RequestInit | undefined)?.method === "PUT",
      ),
    ).toBe(false);
  });
});

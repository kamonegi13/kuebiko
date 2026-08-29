import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MirrorBanner } from "./MirrorBanner";

function renderBanner() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MirrorBanner />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("写しの帯", () => {
  it("ライブでは何も出さない", () => {
    vi.stubEnv("VITE_MIRROR", "");
    const { container } = renderBanner();
    expect(container.textContent).toBe("");
  });

  it("写しではいつ時点かを出す", async () => {
    // ⚠ これが無いと古い情報をライブと見分けられない。写しの前提そのもの。
    vi.stubEnv("VITE_MIRROR", "1");
    vi.stubGlobal("fetch", async () => ({
      ok: true,
      json: async () => ({
        generated_at: "2026-08-29T00:00:00+00:00",
        kind: "ops-mirror",
        window_days: 30,
        with_bodies: false,
        counts: { articles: 6552, eventnews: 4000 },
      }),
    }));
    renderBanner();
    // 読み手が要るのは「写しである」ことではなく **いつの情報か**。
    expect(await screen.findByText(/現在の情報/)).toBeTruthy();
    expect(screen.getByText(/記事本文は含まれません/)).toBeTruthy();
  });

  it("素性が読めなくても黙らない", async () => {
    // 写しなのに「写し」と言えない状態が一番危ない
    vi.stubEnv("VITE_MIRROR", "1");
    vi.stubGlobal("fetch", async () => ({ ok: false, status: 404, statusText: "Not Found" }));
    renderBanner();
    await waitFor(() =>
      expect(screen.getByText(/いつ時点の情報かを取得できませんでした/)).toBeTruthy(),
    );
  });
});

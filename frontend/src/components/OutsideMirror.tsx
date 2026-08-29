import { Archive } from "lucide-react";

import { MIRROR_HOME } from "./nav";

/** 写しに含まれていない画面を開いたときの着地。
 *
 *  ⚠ **黙って読み込み中を出し続けてはいけない。** 写しには無いデータを取りに行くと
 *  取得は失敗し、画面は待ち続ける。利用者からは「壊れている」と「まだ写していない」が
 *  区別できない (2026-08-29 実測: ダッシュボードがこの状態で固まっていた)。
 *  MirrorBanner と同じ思想 — 古いこと自体ではなく、**分からないこと**が問題。
 */
export function OutsideMirror() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-3 px-6 text-center">
      <Archive size={32} className="text-fg-muted" />
      <h1 className="text-lg font-semibold text-fg">この画面は写しに含まれていません</h1>
      <p className="max-w-md text-sm leading-relaxed text-fg-muted">
        写しは Mac に到達できないときの続きを読むためのもので、書き出した画面だけを持ちます。
        最新のすべてを見るには Mac 側の画面を開いてください。
      </p>
      <a
        href={MIRROR_HOME}
        className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-fg hover:opacity-90"
      >
        写しにある画面へ
      </a>
    </div>
  );
}

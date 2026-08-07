// 共通 Drawer (右から slide overlay)。Notion / Linear の deep-link review UX。
// 主 view (Threats actor 詳細など) の context を保持しつつ、operations review を即座に展開できる。

import { useEffect } from "react";
import { createPortal } from "react-dom";

interface DrawerProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  /** md+ で適用する幅クラス (例: "md:w-[56rem]")。mobile は常に w-full。default は md:w-[56rem] */
  widthClass?: string;
  /** ヘッダ右側 (閉じるボタンの左) に置く追加要素 (例: ArticlePeek の「記事画面で開く」) */
  headerExtra?: React.ReactNode;
  children: React.ReactNode;
}

export function Drawer({
  isOpen,
  onClose,
  title,
  widthClass = "md:w-[56rem]",
  headerExtra,
  children,
}: DrawerProps) {
  // ESC で close + scroll lock
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    const prevPaddingRight = document.body.style.paddingRight;
    // overflow:hidden で縦スクロールバーが消えるとコンテンツ幅が広がり、背景の
    // オブジェクトが右へ数 px ずれる (classic scrollbar 環境)。消える幅と同じ
    // padding-right を補償してレイアウト幅を不変に保つ (モーダルの定石)。
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.overflow = "hidden";
    if (scrollbarWidth > 0) document.body.style.paddingRight = `${scrollbarWidth}px`;
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      document.body.style.paddingRight = prevPaddingRight;
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  // body 直下に portal で描画する。ページ内に置くと親の space-y-* の margin が
  // fixed 要素にも乗って上端に隙間が出る (実害確認済) ほか、祖先の transform/filter
  // で fixed の基準がズレる事故も防げる。
  return createPortal(
    <div
      aria-modal="true"
      role="dialog"
      aria-label={title}
      className="fixed inset-0 z-50 m-0 flex justify-end"
    >
      {/* Backdrop */}
      <div
        onClick={onClose}
        className="absolute inset-0 bg-black/40 backdrop-blur-sm animate-fade-in"
      />
      {/* Drawer panel — mobile では full-screen (w-full)、tablet+ で widthClass */}
      <div
        className={`relative w-full ${widthClass} max-w-full md:max-w-[95vw] h-full bg-bg border-l border-border-default shadow-2xl overflow-y-auto animate-slide-in-right`}
      >
        <div className="sticky top-0 z-10 bg-bg/95 backdrop-blur-md border-b border-border-subtle px-5 py-3 flex items-center justify-between">
          <h3 className="m-0 text-md font-bold text-fg tracking-tight">{title}</h3>
          <div className="flex items-center gap-2">
            {headerExtra}
            <button
              onClick={onClose}
              aria-label="閉じる"
              className="text-fg-muted hover:text-fg hover:bg-surface-2 rounded-md w-8 h-8 inline-flex items-center justify-center text-lg leading-none transition-colors"
            >
              ×
            </button>
          </div>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

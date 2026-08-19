// 判定基準/プロンプト系エディタ共通の auto-grow textarea (mono)。
// scrollHeight を反映して高さを内容に追従させる。元は RubricSectionCard.tsx に閉じていたが、
// BlockCard.tsx (block 方式プロンプト編集) でも同じ実装が要るため共有化した (DRY)。

import { useEffect, useRef } from "react";

interface AutoGrowTextareaProps {
  value: string;
  onChange: (value: string) => void;
  rows?: number;
}

export function AutoGrowTextarea({ value, onChange, rows = 2 }: AutoGrowTextareaProps) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);
  return (
    <textarea
      ref={ref}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      spellCheck={false}
      className="w-full resize-none overflow-hidden rounded border border-border-subtle bg-surface-2 px-2 py-1.5 font-mono text-xs leading-relaxed text-fg"
    />
  );
}

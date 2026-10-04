// 区切り widget: データ取得も機能も持たない、純粋にレイアウト用の仕切り線。
// widget が増えて縦に長くなった dashboard で「ここからは別グループ」を示すためだけの存在。
// config: label (任意の見出し文字列、既定空=線のみ)。写しでも何も fetch しないので常に動く。
import { cfgStr, type WidgetProps } from "../shared";

export function SeparatorWidget({ config }: WidgetProps = {}) {
  const label = cfgStr(config, "label", "");
  return (
    <div className="flex h-full min-h-0 items-center gap-3 px-1">
      <div className="h-px flex-1 bg-border-subtle" />
      {label && (
        <>
          <span className="shrink-0 text-xs font-medium uppercase tracking-wider text-fg-subtle">{label}</span>
          <div className="h-px flex-1 bg-border-subtle" />
        </>
      )}
    </div>
  );
}

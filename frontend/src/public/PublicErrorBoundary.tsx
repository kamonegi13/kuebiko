// 公開サイトのエラー境界。
//
// 2026-08-25: 一部の記事で `discrepancies` の要素を object のまま描画してしまい
// React が throw → **ツリー全体が外れて画面が真っ黒**になった。公開面で最悪の
// 壊れ方なので、描画中の例外は必ずここで受け止めて「読める何か」を残す。
//
// 境界は **記事本文の周りだけ** に置く。ヘッダ・カテゴリ・フッタまで巻き込むと
// 読み手が他の記事へ移れなくなる。

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** 復帰導線 (一覧へ戻す等)。 */
  onReset?: () => void;
}

interface State {
  failed: boolean;
}

export class PublicErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 公開面なので詳細は出さない。開発時に気付けるよう console には残す
    console.error("public news render failed", error, info.componentStack);
  }

  componentDidUpdate(prev: Props): void {
    // 別の記事へ移ったら復帰させる (一度落ちたまま固まらない)
    if (this.state.failed && prev.children !== this.props.children) {
      this.setState({ failed: false });
    }
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="py-10 space-y-3">
        <p className="text-sm text-fg-muted">この記事は表示できませんでした。</p>
        {this.props.onReset && (
          <button onClick={this.props.onReset} className="text-sm text-accent hover:underline">
            ← 一覧へ戻る
          </button>
        )}
      </div>
    );
  }
}

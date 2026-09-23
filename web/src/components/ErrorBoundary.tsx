import { Component, type ErrorInfo, type ReactNode } from "react";
import { Banner, Button } from "./ui";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Catches a render-time exception anywhere below it, so one broken view (an API response shaped
 *  differently than assumed, a null a component didn't guard) shows an error where it happened
 *  instead of unmounting the whole tree to a blank screen with nothing in the UI to explain why —
 *  the gap a 2026-09-23 pilot test found nothing here was catching.
 *
 *  `PipelineApp` gives this a `key` tied to the route, so navigating to a different page remounts
 *  it fresh and clears the error on its own; "Try again" does the same without leaving the page. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("booth-pipeline: unhandled error in the UI", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <Banner tone="error">
          <p className="font-medium">Something went wrong showing this page.</p>
          <p className="mt-1 text-xs opacity-80">{this.state.error.message}</p>
          <div className="mt-2">
            <Button onClick={() => this.setState({ error: null })}>Try again</Button>
          </div>
        </Banner>
      );
    }
    return this.props.children;
  }
}

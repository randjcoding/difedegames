import { Component } from "react";
import type { ReactNode } from "react";
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <main className="main">
        <section role="alert" className="paper feature-panel">
          <h1>This page was interrupted.</h1>
          <p>
            Your saved scores and device drafts have not been cleared. Reload to
            try again, or return home.
          </p>
          <div className="feature-links">
            <button
              className="button primary"
              onClick={() => window.location.reload()}
            >
              Reload this page
            </button>
            <a className="button secondary" href="/">
              Go home
            </a>
          </div>
        </section>
      </main>
    ) : (
      this.props.children
    );
  }
}

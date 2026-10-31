// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";
it("unexpected render errors show recovery actions without clearing stored drafts", () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  localStorage.setItem("difede-drafts:1:1", "preserved draft");
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  function Broken() {
    throw new Error("Synthetic render failure");
    return null;
  }
  act(() =>
    root.render(
      <ErrorBoundary>
        <Broken />
      </ErrorBoundary>,
    ),
  );
  expect(host.textContent).toContain("This page was interrupted.");
  expect(host.textContent).toContain("Reload this page");
  expect(host.querySelector("a")?.getAttribute("href")).toBe("/");
  expect(localStorage.getItem("difede-drafts:1:1")).toBe("preserved draft");
  act(() => root.unmount());
  host.remove();
  error.mockRestore();
});

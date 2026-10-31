import { useRef, useState } from "react";
import { api } from "./api";
import type { Me, Preferences } from "./types";
import { ErrorNote, Modal } from "./core";
import {
  commitAppearance,
  defaultAppearance,
  PALETTES,
  setAppearance,
  STYLES,
  useAppearance,
} from "./appearance";
import type { Appearance } from "./appearance";
export function AppearancePicker({
  me,
  refresh,
}: {
  me?: Me;
  refresh: () => Promise<void>;
}) {
  const current = useAppearance();
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false),
    [before, setBefore] = useState<Appearance>(current),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [copied, setCopied] = useState(false);
  function toggle(next: boolean) {
    if (busy) return;
    if (next) {
      setBefore(current);
      setError(undefined);
      setCopied(false);
    } else setAppearance(before, false);
    setOpen(next);
  }
  async function save() {
    setBusy(true);
    setError(undefined);
    try {
      if (me) {
        await api<{ preferences: Preferences }>(
          "/preferences",
          "PATCH",
          current,
        );
        commitAppearance(current);
        await refresh();
      } else commitAppearance(current);
      setOpen(false);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <button
        className="icon-button appearance-trigger"
        ref={trigger}
        onClick={() => toggle(true)}
        aria-label="Appearance: design and colors"
      >
        Style & color
      </button>
      <Modal
        onCloseFocus={() => trigger.current?.focus()}
        busy={busy}
        open={open}
        onOpenChange={toggle}
        title="Make it your game night."
        description="Five designs, every feature. Choose your design and colors independently; preview them here before saving."
      >
        <div className="appearance-options">
          <fieldset>
            <legend>Design</legend>
            <div className="design-options">
              {STYLES.map((s) => (
                <button
                  type="button"
                  disabled={busy}
                  className={"design-preview design-" + s.id}
                  key={s.id}
                  aria-pressed={current.style === s.id}
                  onClick={() => setAppearance({ style: s.id }, false)}
                >
                  <span className="mini-preview" aria-hidden="true">
                    <i />
                    <b>Aa</b>
                    <em />
                    <em />
                  </span>
                  <span>
                    <strong>{s.name}</strong>
                    <small>{s.description}</small>
                  </span>
                </button>
              ))}
            </div>
          </fieldset>
          <fieldset>
            <legend>Colors</legend>
            <div className="palette-options">
              {PALETTES.map((p) => (
                <button
                  type="button"
                  disabled={busy}
                  key={p.id}
                  aria-pressed={current.palette === p.id}
                  onClick={() => setAppearance({ palette: p.id }, false)}
                >
                  <span
                    className="palette-swatch"
                    style={{
                      background: p.background,
                      color: p.text,
                      borderColor: p.line,
                    }}
                    aria-hidden="true"
                  >
                    Aa
                    <i style={{ background: p.accent }} />
                  </span>
                  {p.name}
                </button>
              ))}
            </div>
          </fieldset>
        </div>
        <ErrorNote error={error} />
        <p className="small-note">
          {me
            ? "Saved to your account and remembered on this device."
            : "Remembered on this device. Sign in for account preferences."}
        </p>
        <div className="appearance-actions">
          <button
            disabled={busy}
            className="text-button"
            onClick={() => setAppearance(defaultAppearance, false)}
          >
            Reset to Classic / Midnight Blue
          </button>
          <button
            disabled={busy}
            className="text-button"
            onClick={async () => {
              const url = new URL(location.href);
              url.searchParams.set("style", current.style);
              url.searchParams.set("palette", current.palette);
              try {
                await navigator.clipboard.writeText(url.toString());
                setCopied(true);
              } catch {
                setError(
                  new Error(
                    "Copy is unavailable in this browser. You can still preview and save your appearance.",
                  ),
                );
              }
            }}
          >
            {copied ? "Preview link copied" : "Copy this look"}
          </button>
        </div>
        <div className="dialog-actions">
          <button
            disabled={busy}
            className="button secondary"
            onClick={() => toggle(false)}
          >
            Cancel preview
          </button>
          <button className="button primary" disabled={busy} onClick={save}>
            {busy ? "Saving…" : "Save appearance"}
          </button>
        </div>
      </Modal>
    </>
  );
}

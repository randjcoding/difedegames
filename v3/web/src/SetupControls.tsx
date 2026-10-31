import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { ErrorNote, useAuth, useLoad } from "./core";
import type { Layout } from "./pages/SavedSetups";
type SetupDraft = {
  group_id: number;
  slug: string;
  player_ids: number[];
  mode: string;
  scoring_direction: "low" | "high";
  target_score: number | null;
  title: string | null;
};
export function SetupControls({
  draft,
  onApply,
  editingId,
}: {
  draft: SetupDraft;
  onApply: (layout: Layout) => void;
  editingId?: number;
}) {
  const { me } = useAuth(),
    resource = useLoad<{ layouts: Layout[] }>(
      "/layouts?" +
        new URLSearchParams({
          group_id: String(draft.group_id),
          slug: draft.slug,
        }),
    ),
    [selected, setSelected] = useState(""),
    [name, setName] = useState(""),
    [isDefault, setDefault] = useState(false),
    [error, setError] = useState<unknown>(),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [applied, setApplied] = useState<number>();
  useEffect(() => {
    const found = resource.data?.layouts.find((l) => l.id === editingId);
    if (found && applied !== found.id) {
      onApply(found);
      setName(found.name);
      setDefault(found.is_default);
      setSelected(String(found.id));
      setApplied(found.id);
    }
  }, [resource.data, editingId, applied]);
  const canManage =
    me.user.site_role === "super_admin" ||
    me.groups.some((g) => g.id === draft.group_id && g.role === "admin");
  async function save() {
    setBusy(true);
    setError(undefined);
    setMessage("");
    try {
      await api(
        editingId ? "/layouts/" + editingId : "/layouts",
        editingId ? "PUT" : "POST",
        { ...draft, name: name.trim(), is_default: isDefault },
      );
      setMessage(
        editingId
          ? "Saved setup updated."
          : "Setup saved. Choose viewers and scorekeepers separately when starting.",
      );
      resource.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="setup-controls">
      <div className="section-title">
        <h3>Saved setups</h3>
        <Link to="/setups" className="text-link">
          Manage setups →
        </Link>
      </div>
      {resource.data && resource.data.layouts.length > 0 && (
        <div className="inline-form">
          <label className="grow-label">
            Use a regular roster
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">Choose a setup</option>
              {resource.data.layouts.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                  {l.is_default ? " · Default" : ""}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="button secondary"
            disabled={!selected}
            onClick={() => {
              const l = resource.data?.layouts.find(
                (l) => l.id === Number(selected),
              );
              if (l) {
                onApply(l);
                setMessage("Setup loaded. Review today’s players and sharing.");
              }
            }}
          >
            Load setup
          </button>
        </div>
      )}
      {canManage && (
        <details>
          <summary>
            {editingId
              ? "Update saved setup"
              : "Save this roster for next time"}
          </summary>
          <label>
            Setup name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={80}
              placeholder="Friday Five Crowns"
            />
          </label>
          <label className="check-row">
            <input
              type="checkbox"
              checked={isDefault}
              onChange={(e) => setDefault(e.target.checked)}
            />
            Make the group’s default for this game
          </label>
          <button
            type="button"
            className="button secondary"
            disabled={busy || !name.trim() || !draft.player_ids.length}
            onClick={save}
          >
            {busy ? "Saving…" : editingId ? "Update saved setup" : "Save setup"}
          </button>
        </details>
      )}
      <ErrorNote error={error || resource.error} />
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
    </section>
  );
}

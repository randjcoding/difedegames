import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Busy, ErrorNote, Modal, PageHeading, useAuth, useLoad } from "../core";
import { ListFilters, Pagination } from "../ListTools";
type AccountRow = {
  id: number;
  name: string;
  email: string;
  person_id: number;
  site_role: string;
  is_owner: boolean;
  active: boolean;
  verified: boolean;
  approved: boolean;
};
type PersonRow = {
  id: number;
  name: string;
  email?: string;
  first_name?: string;
  last_name?: string;
  archived: boolean;
  purged: boolean;
  privacy?: {
    is_minor?: boolean;
    is_discoverable?: boolean;
    show_full_last_name?: boolean;
  };
};
type GroupRow = {
  id: number;
  name: string;
  archived: boolean;
  member_count: number;
};
export type AuditEvent = {
  id: number;
  game_id: number | null;
  actor_id: number | null;
  action: string;
  payload: unknown;
  created_at: string;
};
type Editing = {
  kind: "accounts" | "people";
  id: number;
  name: string;
  email: string;
  active?: boolean;
  verified?: boolean;
  approved?: boolean;
  site_role?: string;
  is_minor?: boolean;
  is_discoverable?: boolean;
  show_full_last_name?: boolean;
  is_owner?: boolean;
};
export function Admin() {
  const { me } = useAuth();
  if (me.user.site_role !== "super_admin")
    return (
      <section className="paper feature-panel">
        <h1>Administrator access required.</h1>
        <p>Group leadership tools are available from your group page.</p>
        <Link className="text-link" to="/people">
          Open your groups →
        </Link>
      </section>
    );
  return <AdminTools />;
}
function AdminTools() {
  const { me } = useAuth(),
    [params, setParams] = useSearchParams(),
    tab = params.get("tab") || "overview",
    offset = Number(params.get("offset")) || 0,
    [query, setQuery] = useState(params.get("q") || ""),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [editing, setEditing] = useState<Editing>(),
    [message, setMessage] = useState(""),
    [resetLink, setResetLink] = useState(""),
    [destructive, setDestructive] = useState<{
      kind: "people" | "groups";
      id: number;
      action: string;
      name: string;
    }>(),
    [reason, setReason] = useState(""),
    [confirm, setConfirm] = useState("");
  const pageQuery = new URLSearchParams({
    q: params.get("q") || "",
    include_archived: params.get("archived") || "false",
    limit: "25",
    offset: String(offset),
  });
  const overview = useLoad<{
      counts: Record<string, number>;
      pending_accounts: number;
    }>(tab === "overview" ? "/admin/overview" : null),
    accounts = useLoad<{ accounts: AccountRow[]; total: number }>(
      tab === "accounts" ? "/admin/accounts?" + pageQuery : null,
    ),
    people = useLoad<{ people: PersonRow[]; total: number }>(
      tab === "people" ? "/admin/people?" + pageQuery : null,
    ),
    groups = useLoad<{ groups: GroupRow[]; total: number }>(
      tab === "groups" ? "/admin/groups?" + pageQuery : null,
    ),
    audit = useLoad<{ events: AuditEvent[]; total: number }>(
      tab === "audit"
        ? "/admin/audit?" +
            new URLSearchParams({
              limit: "25",
              offset: String(offset),
              ...(params.get("game_id")
                ? { game_id: params.get("game_id")! }
                : {}),
              ...(params.get("actor_id")
                ? { actor_id: params.get("actor_id")! }
                : {}),
            })
        : null,
    );
  function filter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "offset") next.delete("offset");
    setParams(next);
  }
  function reload() {
    overview.reload();
    accounts.reload();
    people.reload();
    groups.reload();
    audit.reload();
  }
  async function action(path: string, body: unknown, method = "POST") {
    setBusy(true);
    setError(undefined);
    setMessage("");
    try {
      await api(path, method, body);
      reload();
      setMessage("Saved. The audit trail records this change.");
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function saveEdit() {
    if (!editing) return;
    const { id, kind, name, email, ...flags } = editing;
    const body =
      kind === "accounts"
        ? {
            name,
            email,
            active: flags.active,
            verified: flags.verified,
            approved: flags.approved,
            ...(me.user.is_owner ? { site_role: flags.site_role } : {}),
          }
        : {
            name,
            email: email || null,
            is_minor: flags.is_minor,
            is_discoverable: flags.is_discoverable,
            show_full_last_name: flags.show_full_last_name,
          };
    if (
      await action(
        kind === "accounts" ? `/admin/accounts/${id}` : `/people/${id}`,
        body,
        "PATCH",
      )
    )
      setEditing(undefined);
  }
  return (
    <>
      <PageHeading
        eyebrow="CARE FOR THE PEOPLE BEHIND THE SCORES"
        title="Administration"
      />
      <div className="segmented">
        {[
          ["overview", "Overview"],
          ["accounts", "Accounts"],
          ["people", "People"],
          ["groups", "Groups"],
          ["audit", "Audit trail"],
        ].map(([value, label]) => (
          <button
            key={value}
            aria-pressed={tab === value}
            onClick={() => {
              setParams(value === "overview" ? {} : { tab: value });
              setQuery("");
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <ErrorNote error={error} />
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      {tab === "overview" &&
        (!overview.data ? (
          <Busy error={overview.error} />
        ) : (
          <>
            <div className="stats-strip admin-counts">
              {Object.entries(overview.data.counts).map(([key, n]) => (
                <div key={key}>
                  <strong>{n}</strong>
                  <span>{key.replaceAll("_", " ")}</span>
                </div>
              ))}
            </div>
            <Link className="management-link paper" to="/admin?tab=accounts">
              <span>
                {overview.data.pending_accounts} accounts awaiting approval
              </span>
              <span>Review accounts →</span>
            </Link>
            <p className="scope-note">
              Changes preserve person identities and game history. The owner has
              additional role-management protections. Open a game to review
              corrections and its audit trail.
            </p>
            <Link className="button secondary" to="/history">
              Find a game to review
            </Link>
          </>
        ))}
      {["accounts", "people", "groups"].includes(tab) && (
        <ListFilters
          query={query}
          setQuery={setQuery}
          label={"Search " + tab}
          onSearch={() => filter("q", query)}
          onReset={() => {
            setParams({ tab });
            setQuery("");
          }}
        >
          <label className="check-row">
            <input
              type="checkbox"
              checked={params.get("archived") === "true"}
              onChange={(e) => filter("archived", String(e.target.checked))}
            />
            Include archived
          </label>
        </ListFilters>
      )}
      {tab === "accounts" &&
        (!accounts.data ? (
          <Busy error={accounts.error} />
        ) : (
          <>
            <div className="paper admin-list">
              {accounts.data.accounts.map((a) => (
                <article className="admin-record" key={a.id}>
                  <div>
                    <h3>
                      {a.name}
                      {a.is_owner && <span className="tag">Owner</span>}
                    </h3>
                    <p>{a.email}</p>
                    <small>
                      {a.active ? "Active" : "Disabled"} ·{" "}
                      {a.approved ? "Approved" : "Awaiting approval"} ·{" "}
                      {a.verified ? "Verified" : "Unverified"} ·{" "}
                      {a.site_role === "super_admin"
                        ? "Site administrator"
                        : "Member"}
                    </small>
                  </div>
                  <div className="row-actions">
                    <button
                      className="button secondary"
                      disabled={busy || (a.is_owner && !me.user.is_owner)}
                      onClick={() => setEditing({ kind: "accounts", ...a })}
                    >
                      Edit account
                    </button>
                    <button
                      className="text-button"
                      disabled={busy || (a.is_owner && !me.user.is_owner)}
                      onClick={async () => {
                        setBusy(true);
                        try {
                          const link = await api<{ url: string }>(
                            `/admin/accounts/${a.id}/reset-link`,
                            "POST",
                            {},
                          );
                          setResetLink(
                            new URL(link.url, location.origin).toString(),
                          );
                        } catch (e) {
                          setError(e);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Password reset link
                    </button>
                  </div>
                </article>
              ))}
            </div>
            <Pagination
              total={accounts.data.total}
              offset={offset}
              onPage={(n) => filter("offset", String(n))}
            />
          </>
        ))}
      {tab === "people" &&
        (!people.data ? (
          <Busy error={people.error} />
        ) : (
          <>
            <div className="paper admin-list">
              {people.data.people.map((p) => (
                <article className="admin-record" key={p.id}>
                  <div>
                    <Link to={"/person/" + p.id}>
                      <h3>{p.name}</h3>
                    </Link>
                    <p>{p.email || "No contact email"}</p>
                    <small>
                      {p.purged
                        ? "Anonymized"
                        : p.archived
                          ? "Archived"
                          : "Active person"}
                    </small>
                  </div>
                  <div className="row-actions">
                    <button
                      className="button secondary"
                      disabled={busy || p.purged}
                      onClick={() =>
                        setEditing({
                          kind: "people",
                          id: p.id,
                          name: p.name,
                          email: p.email || "",
                          is_minor: p.privacy?.is_minor || false,
                          is_discoverable: p.privacy?.is_discoverable || false,
                          show_full_last_name:
                            p.privacy?.show_full_last_name || false,
                        })
                      }
                    >
                      Edit person
                    </button>
                    {!p.purged && (
                      <>
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() => {
                            setDestructive({
                              kind: "people",
                              id: p.id,
                              action: p.archived ? "reinstate" : "archive",
                              name: p.name,
                            });
                            setReason("");
                            setConfirm("");
                          }}
                        >
                          {p.archived ? "Reinstate" : "Archive"}
                        </button>
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() => {
                            setDestructive({
                              kind: "people",
                              id: p.id,
                              action: "purge",
                              name: p.name,
                            });
                            setReason("");
                            setConfirm("");
                          }}
                        >
                          Anonymize profile
                        </button>
                      </>
                    )}
                  </div>
                </article>
              ))}
            </div>
            <Pagination
              total={people.data.total}
              offset={offset}
              onPage={(n) => filter("offset", String(n))}
            />
          </>
        ))}
      {tab === "groups" &&
        (!groups.data ? (
          <Busy error={groups.error} />
        ) : (
          <>
            <div className="paper admin-list">
              {groups.data.groups.map((g) => (
                <article className="admin-record" key={g.id}>
                  <div>
                    <Link to={"/groups/" + g.id}>
                      <h3>{g.name}</h3>
                    </Link>
                    <p>
                      {g.member_count} members ·{" "}
                      {g.archived ? "Archived" : "Active"}
                    </p>
                  </div>
                  <div className="row-actions">
                    <Link className="button secondary" to={"/groups/" + g.id}>
                      Manage group
                    </Link>
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() => {
                        setDestructive({
                          kind: "groups",
                          id: g.id,
                          action: g.archived ? "reinstate" : "archive",
                          name: g.name,
                        });
                        setReason("");
                        setConfirm("");
                      }}
                    >
                      {g.archived ? "Reinstate" : "Archive"}
                    </button>
                  </div>
                </article>
              ))}
            </div>
            <Pagination
              total={groups.data.total}
              offset={offset}
              onPage={(n) => filter("offset", String(n))}
            />
          </>
        ))}
      {tab === "audit" && (
        <>
          <div className="list-filters">
            <label>
              Game number
              <input
                type="number"
                min="1"
                value={params.get("game_id") || ""}
                onChange={(e) => filter("game_id", e.target.value)}
              />
            </label>
            <label>
              Actor account number
              <input
                type="number"
                min="1"
                value={params.get("actor_id") || ""}
                onChange={(e) => filter("actor_id", e.target.value)}
              />
            </label>
            <button
              className="button secondary"
              onClick={() => setParams({ tab: "audit" })}
            >
              Reset
            </button>
          </div>
          {!audit.data ? (
            <Busy error={audit.error} />
          ) : (
            <>
              <AuditList events={audit.data.events} />
              <Pagination
                total={audit.data.total}
                offset={offset}
                onPage={(n) => filter("offset", String(n))}
              />
            </>
          )}
        </>
      )}
      <Modal
        open={!!editing}
        onOpenChange={(open) => {
          if (!open) setEditing(undefined);
        }}
        busy={busy}
        title={editing?.kind === "accounts" ? "Edit account" : "Edit person"}
        description="Account access and person identity are separate. Changes are authorized and audited."
      >
        <ErrorNote error={error} />
        {editing && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void saveEdit();
            }}
          >
            <label>
              {editing.kind === "accounts"
                ? "Account name"
                : "Preferred display name"}
              <input
                value={editing.name}
                required
                onChange={(e) =>
                  setEditing({ ...editing, name: e.target.value })
                }
              />
            </label>
            <label>
              Email
              <input
                type="email"
                value={editing.email}
                onChange={(e) =>
                  setEditing({ ...editing, email: e.target.value })
                }
              />
            </label>
            {editing.kind === "accounts" ? (
              <>
                {(["active", "verified", "approved"] as const).map((key) => (
                  <label className="check-row" key={key}>
                    <input
                      type="checkbox"
                      checked={!!editing[key]}
                      disabled={
                        key === "active" &&
                        (editing.is_owner || editing.id === me.user.id)
                      }
                      onChange={(e) =>
                        setEditing({ ...editing, [key]: e.target.checked })
                      }
                    />
                    {key === "active"
                      ? "Login enabled"
                      : key === "verified"
                        ? "Email verified"
                        : "Account approved"}
                  </label>
                ))}
                {me.user.is_owner && (
                  <label>
                    Site role
                    <select
                      value={editing.site_role}
                      disabled={editing.is_owner}
                      onChange={(e) =>
                        setEditing({ ...editing, site_role: e.target.value })
                      }
                    >
                      <option value="member">Member</option>
                      <option value="super_admin">Site administrator</option>
                    </select>
                  </label>
                )}
              </>
            ) : (
              <>
                {(
                  [
                    "is_minor",
                    "is_discoverable",
                    "show_full_last_name",
                  ] as const
                ).map((key) => (
                  <label className="check-row" key={key}>
                    <input
                      type="checkbox"
                      checked={!!editing[key]}
                      onChange={(e) =>
                        setEditing({ ...editing, [key]: e.target.checked })
                      }
                    />
                    {key === "is_minor"
                      ? "Minor identity"
                      : key === "is_discoverable"
                        ? "Discoverable in directory"
                        : "Show full last name in shared views"}
                  </label>
                ))}
              </>
            )}
            <button className="button primary" disabled={busy}>
              Save changes
            </button>
          </form>
        )}
      </Modal>
      <Modal
        open={!!destructive}
        onOpenChange={(open) => {
          if (!open) setDestructive(undefined);
        }}
        busy={busy}
        title={
          destructive?.action === "purge"
            ? "Anonymize this profile?"
            : `${destructive?.action === "reinstate" ? "Reinstate" : "Archive"} ${destructive?.name || ""}?`
        }
        description={
          destructive?.action === "purge"
            ? "This irreversibly anonymizes the active profile while retaining identity and score history. Imported provenance and backups remain sensitive; this is not a guarantee of complete erasure."
            : "History and identity are retained. Active game and owner protections are enforced."
        }
      >
        <ErrorNote error={error} />
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (
              destructive &&
              (await action(
                `/${destructive.kind}/${destructive.id}/${destructive.action}`,
                {
                  reason,
                  ...(destructive.action === "purge" ? { confirm } : {}),
                },
              ))
            )
              setDestructive(undefined);
          }}
        >
          <label>
            Reason
            <input
              required
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {destructive?.action === "purge" && (
            <label>
              Type PURGE to confirm
              <input
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </label>
          )}
          <button
            className="button danger"
            disabled={
              busy || (destructive?.action === "purge" && confirm !== "PURGE")
            }
          >
            Confirm action
          </button>
        </form>
      </Modal>
      <Modal
        open={!!resetLink}
        onOpenChange={(open) => {
          if (!open) setResetLink("");
        }}
        title="Private password reset link"
        description="Share directly with the intended account holder. No delivery is implied. The link expires in one hour."
      >
        <label>
          Reset link
          <input
            readOnly
            value={resetLink}
            onFocus={(e) => e.target.select()}
          />
        </label>
      </Modal>
    </>
  );
}
export function AuditList({ events }: { events: AuditEvent[] }) {
  return (
    <div className="paper audit-list">
      {events.length ? (
        events.map((e) => (
          <article key={e.id}>
            <div>
              <strong>{e.action.replaceAll("_", " ")}</strong>
              <small>
                {new Date(e.created_at).toLocaleString()} · Account{" "}
                {e.actor_id ?? "system"}
                {e.game_id ? ` · Game ${e.game_id}` : ""}
              </small>
            </div>
            <details>
              <summary>Change details</summary>
              <pre>{JSON.stringify(e.payload, null, 2)}</pre>
            </details>
          </article>
        ))
      ) : (
        <p className="empty-state">No audit events match this view.</p>
      )}
    </div>
  );
}

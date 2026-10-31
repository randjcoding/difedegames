import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Busy, ErrorNote, Modal, PageHeading, useAuth, useLoad } from "../core";
import type { Group, Person } from "../types";
import { ListFilters, Pagination } from "../ListTools";
import { FavoriteButton, useFavorites } from "../favorites";
type Membership = {
  id: number;
  person_id: number;
  group_id: number;
  role: string;
  status: string;
  is_home: boolean;
};
type GroupDetail = {
  group: Group & { archived: boolean; settings: Record<string, boolean> };
  people: Person[];
  pending_people?: { id: number; name: string }[];
  memberships: Membership[];
  admins: { id: number; name: string }[];
  invites: {
    id: string;
    person_id: number | null;
    expires_at: string;
    used_at: string | null;
  }[];
};
export function GroupDetails() {
  const { id } = useParams(),
    { me, refresh } = useAuth(),
    resource = useLoad<GroupDetail>("/groups/" + id),
    favorites = useFavorites();
  const [name, setName] = useState(""),
    [discoverable, setDiscoverable] = useState(false),
    [rosterPublic, setRosterPublic] = useState(false),
    [query, setQuery] = useState(""),
    [applied, setApplied] = useState(""),
    [offset, setOffset] = useState(0),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [edit, setEdit] = useState<Person>(),
    [editName, setEditName] = useState(""),
    [invite, setInvite] = useState(""),
    [lead, setLead] = useState(""),
    [remove, setRemove] = useState<Membership>();
  useEffect(() => {
    if (resource.data) {
      setName(resource.data.group.name);
      setDiscoverable(!!resource.data.group.settings.is_discoverable);
      setRosterPublic(!!resource.data.group.settings.show_roster);
    }
  }, [resource.data]);
  const canManage =
    me.user.site_role === "super_admin" ||
    me.groups.some((g) => g.id === Number(id) && g.role === "admin");
  async function action(path: string, body: unknown = {}, method = "POST") {
    setBusy(true);
    setError(undefined);
    setMessage("");
    try {
      await api(path, method, body);
      resource.reload();
      await refresh();
      setMessage("Saved.");
      return true;
    } catch (e) {
      setError(e);
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function settings(e: FormEvent) {
    e.preventDefault();
    await action(
      "/groups/" + id,
      { name, is_discoverable: discoverable, show_roster: rosterPublic },
      "PATCH",
    );
  }
  async function inviteMember(person?: number) {
    setBusy(true);
    setError(undefined);
    try {
      const d = await api<{ url: string }>(
        `/groups/${id}/invites`,
        "POST",
        person ? { person_id: person } : {},
      );
      setInvite(new URL(d.url, location.origin).toString());
      resource.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (!resource.data) return <Busy error={resource.error} />;
  const data = resource.data,
    filtered = data.people
      .filter((p) => p.name.toLowerCase().includes(applied.toLowerCase()))
      .sort(
        (a, b) =>
          Number(favorites.people.includes(b.id)) -
            Number(favorites.people.includes(a.id)) ||
          a.name.localeCompare(b.name),
      );
  return (
    <>
      <PageHeading
        eyebrow="ONE GROUP, MANY GAME NIGHTS"
        title={data.group.name}
      >
        <Link className="text-link" to="/people">
          ← People & groups
        </Link>
      </PageHeading>
      <ErrorNote error={error || favorites.error} />
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      <div className="feature-links">
        <Link to={"/groups/" + id + "/statistics"} className="button secondary">
          Group stats
        </Link>
        {canManage && (
          <button
            className="button primary"
            disabled={busy}
            onClick={() => void inviteMember()}
          >
            Invite someone
          </button>
        )}
      </div>
      <ListFilters
        query={query}
        setQuery={setQuery}
        label="Find a group member"
        onSearch={() => {
          setApplied(query);
          setOffset(0);
        }}
        onReset={() => {
          setQuery("");
          setApplied("");
          setOffset(0);
        }}
      />
      <div className="paper member-list">
        {filtered.slice(offset, offset + 25).map((p) => {
          const m = data.memberships.find(
            (m) => m.person_id === p.id && m.status === "active",
          );
          return (
            <article className="person-row" key={p.id}>
              <div>
                <Link to={"/person/" + p.id}>
                  <h3>{p.name}</h3>
                </Link>
                <p>
                  {p.has_account ? "Linked account" : "Guest identity"}
                  {m?.is_home ? " · Home group" : ""}
                </p>
              </div>
              <FavoriteButton
                label={p.name}
                active={favorites.people.includes(p.id)}
                disabled={favorites.busy}
                onClick={() => void favorites.togglePerson(p.id)}
              />
              <div className="row-actions">
                {canManage &&
                  (!p.has_account || me.user.site_role === "super_admin") && (
                    <button
                      className="text-button"
                      onClick={() => {
                        setEdit(p);
                        setEditName(p.name);
                      }}
                    >
                      Edit person
                    </button>
                  )}
                {canManage && !p.has_account && (
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => void inviteMember(p.id)}
                  >
                    Invite to claim
                  </button>
                )}
                {m && (canManage || p.id === me.user.person_id) && (
                  <button className="text-button" onClick={() => setRemove(m)}>
                    {p.id === me.user.person_id
                      ? "Leave group"
                      : "Remove from group"}
                  </button>
                )}
                {m && p.id === me.user.person_id && !m.is_home && (
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => void action(`/memberships/${m.id}/home`)}
                  >
                    Make home group
                  </button>
                )}
              </div>
            </article>
          );
        })}
        {!filtered.length && (
          <p className="empty-state">No matching members.</p>
        )}
      </div>
      <Pagination total={filtered.length} offset={offset} onPage={setOffset} />
      {data.memberships.some((m) => m.status === "requested") && canManage && (
        <section className="paper feature-panel">
          <h2>Join requests</h2>
          {data.memberships
            .filter((m) => m.status === "requested")
            .map((m) => (
              <div className="management-link" key={m.id}>
                <span>
                  {data.people.find((p) => p.id === m.person_id)?.name ||
                    data.pending_people?.find((p) => p.id === m.person_id)
                      ?.name ||
                    "Person requesting membership"}
                </span>
                <div className="row-actions">
                  <button
                    className="button secondary"
                    disabled={busy}
                    onClick={() =>
                      void action(`/memberships/${m.id}/decide`, {
                        decision: "approve",
                      })
                    }
                  >
                    Approve
                  </button>
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() =>
                      void action(`/memberships/${m.id}/decide`, {
                        decision: "deny",
                      })
                    }
                  >
                    Deny
                  </button>
                </div>
              </div>
            ))}
        </section>
      )}
      {canManage && (
        <div className="account-grid">
          <form className="paper feature-panel" onSubmit={settings}>
            <h2>Group settings</h2>
            <label>
              Group name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={120}
              />
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={discoverable}
                onChange={(e) => setDiscoverable(e.target.checked)}
              />
              Allow directory discovery
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={rosterPublic}
                onChange={(e) => setRosterPublic(e.target.checked)}
              />
              Show roster where discovery is allowed
            </label>
            <button className="button primary" disabled={busy}>
              Save settings
            </button>
          </form>
          <section className="paper feature-panel">
            <h2>Group leadership</h2>
            <p>
              Hand over leadership to an existing member with a linked account.
              The current lead becomes a member.
            </p>
            <label>
              New group lead
              <select value={lead} onChange={(e) => setLead(e.target.value)}>
                <option value="">Choose a member</option>
                {data.people
                  .filter(
                    (p) =>
                      p.account_id &&
                      data.memberships.some(
                        (m) => m.person_id === p.id && m.status === "active",
                      ),
                  )
                  .map((p) => (
                    <option key={p.id} value={p.account_id!}>
                      {p.name}
                    </option>
                  ))}
              </select>
            </label>
            <button
              className="button secondary"
              disabled={busy || !lead}
              onClick={() =>
                void action(`/groups/${id}/transfer-lead`, {
                  user_id: Number(lead),
                })
              }
            >
              Transfer leadership
            </button>
            <h3>Open invitations</h3>
            {data.invites
              .filter((i) => !i.used_at)
              .map((i) => (
                <div className="management-link" key={i.id}>
                  <span>
                    {data.people.find((p) => p.id === i.person_id)?.name ||
                      "Group invitation"}
                    <small>
                      Expires {new Date(i.expires_at).toLocaleDateString()}
                    </small>
                  </span>
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => void action(`/invites/${i.id}/revoke`)}
                  >
                    Revoke
                  </button>
                </div>
              ))}
          </section>
        </div>
      )}
      <Modal
        open={!!edit}
        onOpenChange={(open) => {
          if (!open) setEdit(undefined);
        }}
        title="Edit person"
        description="This updates the person’s preferred name without replacing their identity or history."
      >
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (
              edit &&
              (await action("/people/" + edit.id, { name: editName }, "PATCH"))
            )
              setEdit(undefined);
          }}
        >
          <label>
            Display name
            <input
              required
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
            />
          </label>
          <button className="button primary" disabled={busy}>
            Save person
          </button>
        </form>
      </Modal>
      <Modal
        open={!!remove}
        onOpenChange={(open) => {
          if (!open) setRemove(undefined);
        }}
        title="Leave this group membership?"
        description="Only the membership is removed. The person, login and game history remain. Group leads must transfer leadership before leaving."
      >
        <button
          className="button danger"
          disabled={busy}
          onClick={async () => {
            if (
              remove &&
              (await action(`/memberships/${remove.id}/decide`, {
                decision: "remove",
              }))
            )
              setRemove(undefined);
          }}
        >
          Confirm membership removal
        </button>
      </Modal>
      <Modal
        open={!!invite}
        onOpenChange={(open) => {
          if (!open) setInvite("");
        }}
        title="Private invitation link"
        description="Share this single-use link with the intended person. No email is sent."
      >
        <label>
          Invitation link
          <input readOnly value={invite} onFocus={(e) => e.target.select()} />
        </label>
      </Modal>
    </>
  );
}

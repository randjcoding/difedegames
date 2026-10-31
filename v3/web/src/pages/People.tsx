import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Group, Person } from "../types";
import { Busy, ErrorNote, Modal, PageHeading, useAuth, useLoad } from "../core";
import { ListFilters, Pagination } from "../ListTools";
import { FavoriteButton, useFavorites } from "../favorites";
export function People() {
  const favorites = useFavorites();
  const [personQuery, setPersonQuery] = useState(""),
    [personSearch, setPersonSearch] = useState(""),
    [personOffset, setPersonOffset] = useState(0);
  const { me, refresh } = useAuth(),
    people = useLoad<{ people: Person[] }>("/people"),
    groups = useLoad<{ groups: Group[] }>("/groups");
  const [group, setGroup] = useState(me.groups[0]?.id || 0),
    [name, setName] = useState(""),
    [groupName, setGroupName] = useState(""),
    [error, setError] = useState<unknown>(),
    [invite, setInvite] = useState(""),
    [inviteExpiry, setInviteExpiry] = useState(""),
    [busy, setBusy] = useState(false),
    [copy, setCopy] = useState(false);
  async function createGroup(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const g = await api<Group>("/groups", "POST", { name: groupName });
      await refresh();
      setGroup(g.id);
      setGroupName("");
      groups.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function createGuest(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await api("/people", "POST", { name, group_id: group });
      setName("");
      people.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function invitePerson(person?: number) {
    setBusy(true);
    setError(undefined);
    try {
      const result = await api<{ url: string; expires_at: string }>(
        `/groups/${group}/invites`,
        "POST",
        person ? { person_id: person } : {},
      );
      setInvite(new URL(result.url, location.origin).toString());
      setInviteExpiry(result.expires_at);
      setCopy(false);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (!people.data || !groups.data)
    return <Busy error={people.error || groups.error} />;
  const selected = groups.data.groups.find((g) => g.id === group),
    roster = people.data.people
      .filter(
        (p) =>
          p.group_ids.includes(group) &&
          p.name.toLowerCase().includes(personSearch.toLowerCase()),
      )
      .sort(
        (a, b) =>
          Number(favorites.people.includes(b.id)) -
            Number(favorites.people.includes(a.id)) ||
          a.name.localeCompare(b.name),
      );
  return (
    <>
      <PageHeading
        eyebrow="GOOD COMPANY IS THE WHOLE POINT"
        title="Your people & groups."
      />
      <div className="feature-links">
        <Link className="button secondary" to="/directory">
          Find people & groups
        </Link>
        <Link className="text-link" to="/setups">
          Saved game setups →
        </Link>
      </div>
      <ErrorNote error={error} />
      <div className="people-layout">
        <aside className="paper group-sidebar">
          <h2>Your circles</h2>
          {groups.data.groups.map((g) => (
            <button
              key={g.id}
              className={"group-choice " + (group === g.id ? "active" : "")}
              onClick={() => setGroup(g.id)}
            >
              <span>{g.name}</span>
              <small>{g.role === "admin" ? "Group admin" : "Member"}</small>
            </button>
          ))}
          <form onSubmit={createGroup}>
            <label>
              Start another group
              <input
                value={groupName}
                onChange={(e) => setGroupName(e.target.value)}
                placeholder="Family or friend group"
                maxLength={120}
                required
              />
            </label>
            <button className="button secondary wide" disabled={busy}>
              Create group
            </button>
          </form>
        </aside>
        <section>
          <div className="section-title">
            <h2>{selected?.name || "Create your first group"}</h2>
            {selected && (
              <Link className="button secondary" to={"/groups/" + group}>
                Open group
              </Link>
            )}
            {selected?.role === "admin" && (
              <button
                className="button primary"
                disabled={busy}
                onClick={() => invitePerson()}
              >
                Invite someone +
              </button>
            )}
          </div>
          <ListFilters
            query={personQuery}
            setQuery={setPersonQuery}
            label="Find a person in this group"
            onSearch={() => {
              setPersonSearch(personQuery);
              setPersonOffset(0);
            }}
            onReset={() => {
              setPersonQuery("");
              setPersonSearch("");
              setPersonOffset(0);
            }}
          />
          {roster.length ? (
            <div className="roster">
              {roster.slice(personOffset, personOffset + 25).map((p) => (
                <div className="person-row" key={p.id}>
                  <span className="avatar">{p.name.slice(0, 1)}</span>
                  <div>
                    <Link to={"/person/" + p.id}>
                      <h3>{p.name}</h3>
                    </Link>
                    <p>
                      {p.has_account
                        ? "Linked account"
                        : "Guest identity · history stays with this person"}
                    </p>
                  </div>
                  <FavoriteButton
                    label={p.name}
                    active={favorites.people.includes(p.id)}
                    disabled={favorites.busy}
                    onClick={() => void favorites.togglePerson(p.id)}
                  />
                  {!p.has_account && selected?.role === "admin" && (
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() => invitePerson(p.id)}
                    >
                      Invite to claim →
                    </button>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="empty-state">
              Add your first guest or invite someone to join.
            </p>
          )}
          <Pagination
            total={roster.length}
            offset={personOffset}
            onPage={setPersonOffset}
          />
          {group > 0 && selected?.role === "admin" && (
            <form className="paper add-person" onSubmit={createGuest}>
              <h3>A seat for someone new</h3>
              <p className="muted">
                A guest can play now and claim this same person’s history later.
              </p>
              <div className="inline-form">
                <input
                  aria-label="Guest full name"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={100}
                  placeholder="Full name"
                />
                <button className="button secondary" disabled={busy}>
                  Add guest
                </button>
              </div>
            </form>
          )}
          <div className="small-note">
            A person can belong to several groups. Membership and login are
            separate from game seats. Open a group to manage memberships,
            invitations and leadership. Person identities and score history stay
            intact.
          </div>
        </section>
      </div>
      <Modal
        open={!!invite}
        onOpenChange={(open) => {
          if (!open) setInvite("");
        }}
        title="An invitation to your table"
        description="Share this private, single-use link directly with the intended person. No email is sent."
      >
        <label>
          Invitation link
          <input readOnly value={invite} onFocus={(e) => e.target.select()} />
        </label>
        <p>Expires {new Date(inviteExpiry).toLocaleString()}.</p>
        <button
          className="button primary"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(invite);
              setCopy(true);
            } catch {
              setCopy(false);
            }
          }}
        >
          {copy ? "Copied" : "Copy invitation"}
        </button>
        <p className="small-note">
          If clipboard access is unavailable, select and copy the link above.
        </p>
      </Modal>
    </>
  );
}

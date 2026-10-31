import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Busy, ErrorNote, PageHeading, useAuth, useLoad } from "../core";
import { ListFilters } from "../ListTools";
type FoundGroup = {
  id: number;
  name: string;
  show_roster: boolean;
  member_count: number | null;
  membership_status: string | null;
};
type FoundPerson = {
  id: number;
  name: string;
  has_account: boolean;
  group_ids: number[];
};
export function Directory() {
  const { me } = useAuth(),
    [kind, setKind] = useState("groups"),
    [query, setQuery] = useState(""),
    [search, setSearch] = useState(""),
    [sent, setSent] = useState<number[]>([]),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false);
  const groups = useLoad<{ groups: FoundGroup[] }>(
      kind === "groups" && search.length >= 2
        ? "/directory/groups?q=" + encodeURIComponent(search)
        : null,
    ),
    people = useLoad<{ people: FoundPerson[] }>(
      kind === "people" && search.length >= 2
        ? "/directory/people?q=" + encodeURIComponent(search)
        : null,
    );
  return (
    <div className="data-page">
      <PageHeading eyebrow="FIND YOUR PEOPLE" title="Directory">
        <Link className="text-link" to="/people">
          Your groups →
        </Link>
      </PageHeading>
      <p className="scope-note">
        Search by name. Discovery respects group visibility, person preferences
        and minor protections; it never grants game access.
      </p>
      <div className="segmented">
        <button
          aria-pressed={kind === "groups"}
          onClick={() => setKind("groups")}
        >
          Groups
        </button>
        <button
          aria-pressed={kind === "people"}
          onClick={() => setKind("people")}
        >
          People
        </button>
      </div>
      <ListFilters
        query={query}
        setQuery={setQuery}
        label="Name · at least two characters"
        onSearch={() => {
          setError(undefined);
          if (query.trim().length < 2)
            setError(new Error("Enter at least two name characters."));
          else setSearch(query.trim());
        }}
        onReset={() => {
          setQuery("");
          setSearch("");
          setError(undefined);
        }}
      />
      <ErrorNote error={error} />
      {search.length >= 2 &&
        (kind === "groups" ? (
          !groups.data ? (
            <Busy error={groups.error} />
          ) : (
            <div className="paper admin-list">
              {groups.data.groups.map((g) => (
                <article className="admin-record" key={g.id}>
                  <div>
                    <h3>{g.name}</h3>
                    <p>
                      {g.member_count == null
                        ? "Roster kept private"
                        : `${g.member_count} members`}
                    </p>
                  </div>
                  {g.membership_status === "active" ||
                  me.user.site_role === "super_admin" ||
                  me.groups.some((m) => m.id === g.id) ? (
                    <Link to={"/groups/" + g.id} className="button secondary">
                      Open group
                    </Link>
                  ) : (
                    <button
                      className="button secondary"
                      disabled={
                        busy ||
                        g.membership_status === "requested" ||
                        sent.includes(g.id)
                      }
                      onClick={async () => {
                        setBusy(true);
                        setError(undefined);
                        try {
                          await api(`/groups/${g.id}/request-join`, "POST", {});
                          setSent((s) => [...s, g.id]);
                        } catch (e) {
                          setError(e);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      {g.membership_status === "requested" ||
                      sent.includes(g.id)
                        ? "Request pending"
                        : "Request to join"}
                    </button>
                  )}
                </article>
              ))}
              {!groups.data.groups.length && (
                <p className="empty-state">No discoverable groups match.</p>
              )}
            </div>
          )
        ) : !people.data ? (
          <Busy error={people.error} />
        ) : (
          <div className="paper admin-list">
            {people.data.people.map((p) => (
              <article className="admin-record" key={p.id}>
                <div>
                  <h3>{p.name}</h3>
                  <p>
                    {p.has_account ? "Linked account" : "Guest identity"} ·{" "}
                    {
                      p.group_ids.filter((id) =>
                        me.groups.some((g) => g.id === id),
                      ).length
                    }{" "}
                    groups shared with you
                  </p>
                </div>
                {p.group_ids.some((id) =>
                  me.groups.some((g) => g.id === id),
                ) && (
                  <Link className="text-link" to={"/person/" + p.id}>
                    Visible game stats →
                  </Link>
                )}
              </article>
            ))}
            {!people.data.people.length && (
              <p className="empty-state">No discoverable people match.</p>
            )}
          </div>
        ))}
    </div>
  );
}

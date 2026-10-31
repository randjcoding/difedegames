import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Busy, ErrorNote, Modal, PageHeading, useAuth, useLoad } from "../core";
import type { Definition, Game, Person } from "../types";
import { ListFilters, Pagination } from "../ListTools";
export type Layout = {
  id: number;
  group_id: number;
  slug: string;
  name: string;
  player_ids: number[];
  mode: string;
  scoring_direction: "low" | "high";
  target_score: number | null;
  title: string | null;
  is_default: boolean;
  created_by: number;
};
export function SavedSetups() {
  const { me } = useAuth(),
    nav = useNavigate(),
    [group, setGroup] = useState(""),
    [slug, setSlug] = useState(""),
    [query, setQuery] = useState(""),
    [search, setSearch] = useState(""),
    [offset, setOffset] = useState(0),
    [starting, setStarting] = useState<Layout>(),
    [removing, setRemoving] = useState<Layout>(),
    [viewers, setViewers] = useState<number[]>([]),
    [keeper, setKeeper] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>();
  const list = useLoad<{ layouts: Layout[] }>(
      "/layouts?" +
        new URLSearchParams({
          ...(group ? { group_id: group } : {}),
          ...(slug ? { slug } : {}),
        }),
    ),
    catalog = useLoad<{ games: Definition[] }>("/catalog"),
    people = useLoad<{ people: Person[] }>("/people");
  useEffect(() => setOffset(0), [group, slug, search]);
  const filtered = (list.data?.layouts || [])
    .filter((l) => l.name.toLowerCase().includes(search.toLowerCase()))
    .sort(
      (a, b) =>
        Number(b.is_default) - Number(a.is_default) ||
        a.name.localeCompare(b.name),
    );
  const canManage = (l: Layout) =>
    me.user.site_role === "super_admin" ||
    me.groups.some((g) => g.id === l.group_id && g.role === "admin");
  async function action(path: string, method = "POST") {
    setBusy(true);
    setError(undefined);
    try {
      await api(path, method, {});
      list.reload();
      setRemoving(undefined);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="data-page">
      <PageHeading
        eyebrow="YOUR REGULAR GAME NIGHTS, READY TO GO"
        title="Saved setups"
      >
        <Link to="/games" className="button primary">
          Create from a game
        </Link>
      </PageHeading>
      <p className="scope-note">
        A setup remembers the roster and scoring. Viewer sharing and nonplaying
        scorekeepers are chosen explicitly each time.
      </p>
      <ListFilters
        query={query}
        setQuery={setQuery}
        label="Setup name"
        onSearch={() => setSearch(query)}
        onReset={() => {
          setQuery("");
          setSearch("");
          setGroup("");
          setSlug("");
        }}
      >
        <label>
          Group
          <select value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">Your groups</option>
            {me.groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Game
          <select value={slug} onChange={(e) => setSlug(e.target.value)}>
            <option value="">Every game</option>
            {catalog.data?.games.map((g) => (
              <option key={g.slug} value={g.slug}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
      </ListFilters>
      <ErrorNote error={error} />
      {!list.data ? (
        <Busy error={list.error} />
      ) : (
        <div className="paper admin-list">
          {filtered.slice(offset, offset + 25).map((l) => (
            <article key={l.id} className="admin-record">
              <div>
                <h3>
                  {l.name}
                  {l.is_default && <span className="tag">Default</span>}
                </h3>
                <p>
                  {catalog.data?.games.find((g) => g.slug === l.slug)?.name ||
                    l.slug}{" "}
                  · {l.player_ids.length} players ·{" "}
                  {me.groups.find((g) => g.id === l.group_id)?.name}
                </p>
              </div>
              <div className="row-actions">
                <button
                  className="button primary"
                  onClick={() => {
                    setStarting(l);
                    setKeeper("");
                    setViewers([]);
                    setError(undefined);
                  }}
                >
                  Start game
                </button>
                {canManage(l) && (
                  <>
                    <Link
                      to={
                        "/new/" +
                        l.slug +
                        "?setup=" +
                        l.id +
                        "&group=" +
                        l.group_id
                      }
                      className="text-link"
                    >
                      Edit setup
                    </Link>
                    {!l.is_default && (
                      <button
                        className="text-button"
                        disabled={busy}
                        onClick={() =>
                          void action("/layouts/" + l.id + "/set-default")
                        }
                      >
                        Make default
                      </button>
                    )}
                    <button
                      className="text-button"
                      onClick={() => setRemoving(l)}
                    >
                      Delete setup
                    </button>
                  </>
                )}
              </div>
            </article>
          ))}
          {!filtered.length && (
            <p className="empty-state">
              No saved setups yet. Open a game’s setup page to save your regular
              roster.
            </p>
          )}
        </div>
      )}
      {list.data && (
        <Pagination
          total={filtered.length}
          offset={offset}
          onPage={setOffset}
        />
      )}
      <Modal
        busy={busy}
        open={!!starting}
        onOpenChange={(open) => {
          if (!open) setStarting(undefined);
        }}
        title={"Start " + (starting?.name || "this setup") + "?"}
        description="Review sharing and the scorekeeper. Stale or unavailable players will be flagged rather than silently changed."
      >
        <ErrorNote error={error} />
        <p>
          {starting?.player_ids
            .map(
              (id) =>
                people.data?.people.find((p) => p.id === id)?.name ||
                "Unavailable saved player",
            )
            .join(", ")}
        </p>
        <label>
          Nonplaying scorekeeper · optional
          <select value={keeper} onChange={(e) => setKeeper(e.target.value)}>
            <option value="">Linked participants will score</option>
            {!starting?.player_ids.includes(me.user.person_id) && (
              <option value={me.user.id}>
                {me.user.display_name || me.user.name} (you)
              </option>
            )}
            {people.data?.people
              .filter(
                (p) =>
                  p.account_id &&
                  p.account_id !== me.user.id &&
                  !starting?.player_ids.includes(p.id),
              )
              .map((p) => (
                <option key={p.id} value={p.account_id!}>
                  {p.name}
                </option>
              ))}
          </select>
        </label>
        <p>Groups allowed to watch:</p>
        {me.groups.map((g) => (
          <label key={g.id} className="check-row">
            <input
              type="checkbox"
              checked={viewers.includes(g.id)}
              onChange={(e) =>
                setViewers((v) =>
                  e.target.checked
                    ? [...v, g.id]
                    : v.filter((id) => id !== g.id),
                )
              }
            />
            {g.name}
          </label>
        ))}
        <button
          className="button primary"
          disabled={busy}
          onClick={async () => {
            if (!starting) return;
            setBusy(true);
            setError(undefined);
            try {
              const game = await api<Game>(
                `/layouts/${starting.id}/start`,
                "POST",
                {
                  viewer_group_ids: viewers,
                  scorekeeper_ids: keeper ? [Number(keeper)] : [],
                },
              );
              nav("/game/" + game.id);
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          Start this game
        </button>
      </Modal>
      <Modal
        busy={busy}
        open={!!removing}
        onOpenChange={(open) => {
          if (!open) setRemoving(undefined);
        }}
        title="Delete this saved setup?"
        description="Played games and scores are unaffected. Only the reusable setup is removed."
      >
        <button
          className="button danger"
          disabled={busy}
          onClick={() =>
            removing && void action("/layouts/" + removing.id, "DELETE")
          }
        >
          Delete saved setup
        </button>
      </Modal>
    </div>
  );
}

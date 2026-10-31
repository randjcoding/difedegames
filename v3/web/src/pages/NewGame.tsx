import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { api } from "../api";
import type { Definition, Game, Person } from "../types";
import { Art, Busy, ErrorNote, PageHeading, useAuth, useLoad } from "../core";
import { useFavorites } from "../favorites";
import { SetupControls } from "../SetupControls";
import { NotFound } from "./AuthPages";
export function NewGame() {
  const favorites = useFavorites();
  const [setupParams] = useSearchParams();
  const { slug } = useParams(),
    { me } = useAuth(),
    nav = useNavigate();
  const catalog = useLoad<{ games: Definition[] }>("/catalog"),
    people = useLoad<{ people: Person[] }>("/people");
  const def = catalog.data?.games.find((g) => g.slug === slug);
  const [group, setGroup] = useState(
      Number(setupParams.get("group")) || me.groups[0]?.id || 0,
    ),
    [players, setPlayers] = useState<number[]>([]),
    [viewers, setViewers] = useState<number[]>([]),
    [scorekeepers, setScorekeepers] = useState<number[]>([]),
    [mode, setMode] = useState("rounds"),
    [direction, setDirection] = useState<"low" | "high">("low"),
    [target, setTarget] = useState(""),
    [title, setTitle] = useState(""),
    [guest, setGuest] = useState(""),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (def) {
      setMode(def.modes[0] || "rounds");
      setDirection(def.scoring_direction);
      setTarget(def.default_target?.toString() || "");
      setTitle("");
      setPlayers([]);
    }
  }, [def?.slug]);
  async function addGuest() {
    if (!guest.trim() || !group) return;
    setBusy(true);
    try {
      const p = await api<Person>("/people", "POST", {
        name: guest.trim(),
        group_id: group,
      });
      setPlayers((pids) => [...pids, p.id]);
      setGuest("");
      people.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!def) return;
    setError(undefined);
    setBusy(true);
    try {
      const g = await api<Game>("/games", "POST", {
        slug,
        ...(title.trim() ? { title: title.trim() } : {}),
        mode,
        player_ids: players,
        group_id: group,
        viewer_group_ids: viewers,
        scorekeeper_ids: scorekeepers.filter(
          (id) =>
            !people.data?.people.some(
              (p) => p.account_id === id && players.includes(p.id),
            ),
        ),
        scoring_direction: direction,
        target_score: mode === "rounds" && target ? Number(target) : null,
      });
      nav("/game/" + g.id);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (!catalog.data || !people.data)
    return <Busy error={catalog.error || people.error} />;
  if (!def) return <NotFound />;
  return (
    <>
      <PageHeading
        eyebrow="MAKE ROOM FOR EVERYONE"
        title={`Let’s play ${def.name}.`}
      >
        <Link to="/games" className="text-link">
          ← Collection
        </Link>
      </PageHeading>
      <div className="setup-layout">
        <aside className="setup-aside">
          <Art slug={def.slug} />
          <h2>{def.name}</h2>
          <p>{def.rule_note}</p>
          <div className="small-note">
            One phone works beautifully. Signed-in participants may also enter
            scores. People in explicitly shared groups can watch.
          </div>
        </aside>
        <form className="setup-form paper" onSubmit={submit}>
          <SetupControls
            editingId={Number(setupParams.get("setup")) || undefined}
            draft={{
              group_id: group,
              slug: def.slug,
              player_ids: players,
              mode,
              scoring_direction: direction,
              target_score: mode === "rounds" && target ? Number(target) : null,
              title: title.trim() || null,
            }}
            onApply={(layout) => {
              setGroup(layout.group_id);
              setPlayers(layout.player_ids);
              setMode(layout.mode);
              setDirection(layout.scoring_direction);
              setTarget(layout.target_score?.toString() || "");
              setTitle(layout.title || "");
            }}
          />
          <fieldset>
            <legend>
              <b>01</b> Your table
            </legend>
            <label>
              Host group
              <select
                value={group}
                required
                onChange={(e) => setGroup(Number(e.target.value))}
              >
                <option value="" disabled>
                  Choose a group
                </option>
                {me.groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
            </label>
            {!me.groups.length && (
              <p>
                <Link to="/people" className="text-link">
                  Create a group first →
                </Link>
              </p>
            )}
            {def.slug === "basic-other" && (
              <label>
                What are you playing?
                <input
                  required
                  maxLength={120}
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Your game’s name"
                />
              </label>
            )}
            {def.modes.length > 1 && (
              <label>
                How are you keeping score?
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  {def.modes.map((m) => (
                    <option key={m} value={m}>
                      {m === "rounds"
                        ? "Round by round"
                        : m === "single"
                          ? "One round · record results"
                          : "Just pick the winner"}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </fieldset>
          <fieldset>
            <legend>
              <b>02</b> Who’s playing?
            </legend>
            <p className="muted">
              {def.min_players}–{def.max_players} players. Select in seating
              order.
            </p>
            <div className="player-options">
              {people.data.people
                .slice()
                .sort(
                  (a, b) =>
                    Number(favorites.people.includes(b.id)) -
                      Number(favorites.people.includes(a.id)) ||
                    a.name.localeCompare(b.name),
                )
                .map((p) => {
                  const pos = players.indexOf(p.id);
                  return (
                    <button
                      key={p.id}
                      type="button"
                      aria-pressed={pos >= 0}
                      className={
                        "player-option " + (pos >= 0 ? "selected" : "")
                      }
                      onClick={() =>
                        setPlayers((ids) =>
                          pos >= 0
                            ? ids.filter((id) => id !== p.id)
                            : [...ids, p.id],
                        )
                      }
                      disabled={pos < 0 && players.length >= def.max_players}
                    >
                      <span className="seat">{pos >= 0 ? pos + 1 : "+"}</span>
                      <span>
                        {p.name}
                        <small>
                          {p.has_account
                            ? "Linked account"
                            : "Guest · can claim this identity later"}
                        </small>
                      </span>
                    </button>
                  );
                })}
            </div>
            {me.groups.find((g) => g.id === group)?.role === "admin" && (
              <div className="inline-form">
                <input
                  aria-label="New guest name"
                  placeholder="Add a guest by name"
                  value={guest}
                  maxLength={100}
                  onChange={(e) => setGuest(e.target.value)}
                />
                <button
                  type="button"
                  className="button secondary"
                  onClick={addGuest}
                  disabled={busy || !guest.trim() || !group}
                >
                  Add guest
                </button>
              </div>
            )}
            <p className="small-note">
              Guests keep one identity and history. Invite them to claim it
              later in People.
            </p>
          </fieldset>
          {mode === "rounds" && (
            <fieldset>
              <legend>
                <b>03</b> Score your way
              </legend>
              {[
                "uno-classic",
                "uno-flip",
                "dutch-blitz",
                "basic-other",
              ].includes(def.slug) ? (
                <label>
                  Who wins?
                  <select
                    value={direction}
                    onChange={(e) =>
                      setDirection(e.target.value as "low" | "high")
                    }
                  >
                    <option value="high">Highest total</option>
                    <option value="low">Lowest total</option>
                  </select>
                </label>
              ) : (
                <p>
                  {def.scoring_direction === "low" ? "Lowest" : "Highest"} total
                  wins. Equal totals share first place.
                </p>
              )}
              {!def.fixed_rounds && (
                <label>
                  Target score · optional
                  <input
                    type="number"
                    min="1"
                    max="1000000"
                    step="1"
                    list="target-presets"
                    value={target}
                    onChange={(e) => setTarget(e.target.value)}
                    placeholder="No limit"
                  />
                  <datalist id="target-presets">
                    {def.targets.map((t) => (
                      <option key={t} value={t} />
                    ))}
                  </datalist>
                  <small>
                    Prompts at the end of a full round. You choose when to
                    finish.
                  </small>
                </label>
              )}
            </fieldset>
          )}
          <fieldset>
            <legend>
              <b>04</b> Share the evening
            </legend>
            <p className="muted">
              Participants always have access. Only groups you select here can
              watch.
            </p>
            <label>
              Nonplaying scorekeeper · optional
              <select
                value={scorekeepers[0] || ""}
                onChange={(e) =>
                  setScorekeepers(
                    e.target.value ? [Number(e.target.value)] : [],
                  )
                }
              >
                <option value="">Participants will keep score</option>
                {!players.includes(me.user.person_id) && (
                  <option value={me.user.id}>
                    {me.user.display_name || me.user.name} (you)
                  </option>
                )}
                {people.data.people
                  .filter(
                    (p) =>
                      p.account_id &&
                      p.account_id !== me.user.id &&
                      !players.includes(p.id),
                  )
                  .map((p) => (
                    <option key={p.id} value={p.account_id!}>
                      {p.name}
                    </option>
                  ))}
              </select>
              <small>
                An assigned scorekeeper can enter everyone’s scores and finish
                the game.
              </small>
            </label>
            {players.length > 0 &&
              !players.some((id) =>
                people.data?.people.some((p) => p.id === id && p.account_id),
              ) &&
              !scorekeepers.length && (
                <div className="notice" role="status">
                  This is a table of guests. Choose yourself or another linked
                  account as the nonplaying scorekeeper before starting.
                </div>
              )}
            {me.groups.map((g) => (
              <label className="check-row" key={g.id}>
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
                <span>{g.name}</span>
              </label>
            ))}
          </fieldset>
          <ErrorNote error={error} />
          <button
            className="button primary wide"
            disabled={
              busy ||
              players.length < def.min_players ||
              players.length > def.max_players ||
              !group ||
              (!players.some((id) =>
                people.data?.people.some((p) => p.id === id && p.account_id),
              ) &&
                !scorekeepers.length)
            }
          >
            {busy
              ? "Preparing your table…"
              : `Start with ${players.length} player${players.length === 1 ? "" : "s"} →`}
          </button>
        </form>
      </div>
    </>
  );
}

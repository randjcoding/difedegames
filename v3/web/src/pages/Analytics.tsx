import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { Busy, PageHeading, useAuth, useLoad } from "../core";
import { ListFilters, Pagination } from "../ListTools";
import type {
  AnalyticsPage,
  Hall,
  PersonStats,
  ScoreRecord,
} from "../analytics";
import type { Definition, Game, Person } from "../types";
import { GameLink } from "./GameLink";
import { CompactSheet } from "../CompactSheet";
import { ResultDetails, Standings } from "../GameResults";

export function Analytics() {
  const { me } = useAuth(),
    [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "leaders",
    [query, setQuery] = useState(params.get("q") || "");
  const catalog = useLoad<{ games: Definition[] }>("/catalog"),
    people = useLoad<{ people: Person[] }>("/people");
  const common = new URLSearchParams(params);
  common.delete("tab");
  common.set("page_size", "20");
  const data = useLoad<AnalyticsPage>(
    ["records", "streaks"].includes(tab)
      ? null
      : `/${tab === "statistics" ? "statistics" : "leaderboards"}?${common}`,
  );
  const hall = useLoad<Hall>(
    tab === "records" ? "/hall-of-fame?" + common : null,
  );
  const streaks = useLoad<{
    streaks: {
      person_id: number;
      name: string;
      length: number;
      start_game_id: number;
      end_game_id: number;
      shared_wins: number;
    }[];
    pagination: AnalyticsPage["pagination"];
  }>(tab === "streaks" ? "/streaks?" + common : null);
  function filter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setParams(next);
  }
  return (
    <div className="data-page">
      <PageHeading
        eyebrow="EVERY GAME NIGHT COUNTS"
        title="Leaders & records."
      />
      <div className="segmented analytics-tabs">
        {[
          ["leaders", "Leaderboards"],
          ["statistics", "All-time statistics"],
          ["records", "Fame & shame"],
          ["streaks", "Winning streaks"],
        ].map(([id, name]) => (
          <button
            key={id}
            aria-pressed={tab === id}
            onClick={() => filter("tab", id)}
          >
            {name}
          </button>
        ))}
      </div>
      <ListFilters
        query={query}
        setQuery={setQuery}
        label="Game title"
        onSearch={() => filter("q", query)}
        onReset={() => {
          setParams(tab === "leaders" ? {} : { tab });
          setQuery("");
        }}
      >
        <label>
          Game
          <select
            value={params.get("slug") || ""}
            onChange={(e) => filter("slug", e.target.value)}
          >
            <option value="">All game types</option>
            {catalog.data?.games.map((g) => (
              <option key={g.slug} value={g.slug}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Hosted by
          <select
            value={params.get("group_id") || ""}
            onChange={(e) => filter("group_id", e.target.value)}
          >
            <option value="">All visible groups</option>
            {me.groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
        <details className="more-filters">
          <summary>More filters</summary>
          <div className="advanced-filter-grid">
            {" "}
            <label>
              Person
              <select
                value={params.get("person_id") || ""}
                onChange={(e) => filter("person_id", e.target.value)}
              >
                <option value="">Everyone visible</option>
                {people.data?.people.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Mode
              <select
                value={params.get("mode") || ""}
                onChange={(e) => filter("mode", e.target.value)}
              >
                <option value="">Every mode</option>
                <option value="rounds">Round by round</option>
                <option value="single">Single result</option>
                <option value="winner">Winner only</option>
              </select>
            </label>
            <label>
              Winning direction
              <select
                value={params.get("direction") || ""}
                onChange={(e) => filter("direction", e.target.value)}
              >
                <option value="">Both directions</option>
                <option value="high">Highest total</option>
                <option value="low">Lowest total</option>
              </select>
            </label>
            {params.get("slug") === "basic-other" && (
              <label>
                Exact custom game title
                <input
                  value={params.get("title") || ""}
                  onChange={(e) => filter("title", e.target.value)}
                />
              </label>
            )}
          </div>
        </details>
      </ListFilters>
      <p className="scope-note compact-scope">
        {me.user.site_role === "super_admin"
          ? "All site games"
          : "Games shared with you"}{" "}
        · All dates · Scores stay within compatible categories.
      </p>
      {["leaders", "statistics"].includes(tab) &&
        (!data.data ? (
          <Busy error={data.error} />
        ) : (
          <>
            <SummaryStats summary={data.data.summary} />
            {tab === "leaders" ? (
              <PlayerTable
                players={data.data.players || []}
                categories={data.data.categories}
              />
            ) : (
              <>
                <div className="category-list">
                  {data.data.categories.map((c) => (
                    <div className="category-row" key={c.category.id}>
                      <span>{c.category.label}</span>
                      <strong>{c.games} games</strong>
                    </div>
                  ))}
                </div>
                <GameBriefs games={data.data.history || []} />
              </>
            )}
            <Pagination
              total={data.data.pagination.total}
              offset={(data.data.pagination.page - 1) * 20}
              limit={20}
              onPage={(offset) => filter("page", String(offset / 20 + 1))}
            />
          </>
        ))}
      {tab === "records" &&
        (!hall.data ? (
          <Busy error={hall.error} />
        ) : (
          <HallRecords hall={hall.data} />
        ))}
      {tab === "streaks" &&
        (!streaks.data ? (
          <Busy error={streaks.error} />
        ) : (
          <>
            {streaks.data.streaks.length ? (
              <div className="record-grid">
                {streaks.data.streaks.map((s) => (
                  <article className="paper feature-panel" key={s.person_id}>
                    <Link to={"/person/" + s.person_id}>
                      <h2>{s.name}</h2>
                    </Link>
                    <strong className="stat-big">{s.length}</strong>
                    <p>
                      consecutive wins in this person’s played games ·{" "}
                      {s.shared_wins} shared
                    </p>
                    <Link className="text-link" to={"/game/" + s.end_game_id}>
                      See the latest game →
                    </Link>
                  </article>
                ))}
              </div>
            ) : (
              <p className="empty-state">
                No provable streaks of two or more wins in this view.
              </p>
            )}
            <Pagination
              total={streaks.data.pagination.total}
              offset={(streaks.data.pagination.page - 1) * 20}
              limit={20}
              onPage={(offset) => filter("page", String(offset / 20 + 1))}
            />
          </>
        ))}
    </div>
  );
}
export function SummaryStats({
  summary,
}: {
  summary: AnalyticsPage["summary"];
}) {
  return (
    <div className="stats-strip">
      {[
        ["Completed games", summary.games],
        ["People at the table", summary.players],
        ["Solo wins", summary.solo_wins],
        ["Shared wins", summary.shared_wins],
      ].map(([label, value]) => (
        <div key={label}>
          <strong>{value}</strong>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}
export function PlayerTable({
  players,
  categories = [],
}: {
  players: PersonStats[];
  categories?: AnalyticsPage["categories"];
}) {
  return players.length ? (
    <div className="paper ranking-list">
      {players.map((p, i) => (
        <article className="ranking-person" key={p.person_id}>
          <span className="standing-rank">{i + 1}</span>
          <div className="ranking-name">
            <Link to={"/person/" + p.person_id}>
              <h3>{p.name}</h3>
            </Link>
            <p>
              {p.played} played · {p.solo_wins} solo + {p.shared_wins} shared
              wins · {p.unknown_results} unknown
            </p>
            {p.winning_scores?.length > 0 && (
              <details>
                <summary>Winning scores by game</summary>
                {p.winning_scores.map((s) => (
                  <p key={s.category_id}>
                    {categories.find((c) => c.category.id === s.category_id)
                      ?.category.label || "Game category"}
                    : best {s.best}, average {s.average} across {s.scored_wins}{" "}
                    scored wins
                  </p>
                ))}
              </details>
            )}
          </div>
          <div className="ranking-wins">
            <strong>{p.wins}</strong>
            <span>wins</span>
          </div>
          <div className="ranking-rate">
            <strong>
              {p.win_percentage == null ? "—" : p.win_percentage + "%"}
            </strong>
            <span>decided win rate</span>
          </div>
        </article>
      ))}
    </div>
  ) : (
    <p className="empty-state">No completed results match these filters.</p>
  );
}
export function GameBriefs({ games }: { games: AnalyticsPage["history"] }) {
  return (
    <div className="brief-list">
      {games.map((g) => (
        <Link className="management-link" key={g.id} to={"/game/" + g.id}>
          <span>
            <strong>{g.title}</strong>
            <small>
              {new Date(g.finished_at || g.created_at).toLocaleDateString()} ·{" "}
              {g.player_count} players
            </small>
          </span>
          <span>
            {g.result_known
              ? g.winners.map((w) => w.name).join(" & ") || "Recorded result"
              : "Original result unknown"}{" "}
            →
          </span>
        </Link>
      ))}
    </div>
  );
}
function RecordList({ title, rows }: { title: string; rows: ScoreRecord[] }) {
  return rows.length ? (
    <section className="paper feature-panel">
      <h2>{title}</h2>
      {rows.map((r, i) => (
        <Link
          className="record-row"
          key={r.game.id + ":" + i}
          to={"/game/" + r.game.id}
        >
          <div>
            <strong>{r.name}</strong>
            <small>
              {r.category.label}
              {r.round_number != null ? " · Hand " + r.round_number : ""}
            </small>
          </div>
          <b>{r.value}</b>
        </Link>
      ))}
    </section>
  ) : null;
}
export function HallRecords({ hall }: { hall: Hall }) {
  return (
    <div className="record-grid">
      <RecordList title="Best scores · by game" rows={hall.best_scores} />
      <RecordList title="Hall of shame · by game" rows={hall.worst_scores} />
      <RecordList
        title="Five Crowns hands to forget"
        rows={hall.worst_five_crowns_hands}
      />
      <RecordList
        title="Trouble · strength of win"
        rows={hall.trouble_strength}
      />
      {hall.biggest_beatdowns.length > 0 && (
        <section className="paper feature-panel">
          <h2>Biggest table spreads</h2>
          <p className="small-note">
            Each record stays in its compatible scoring category.
          </p>
          {hall.biggest_beatdowns.map((r) => (
            <Link
              className="record-row"
              key={r.category.id}
              to={"/game/" + r.game.id}
            >
              <div>
                <strong>{r.game.title}</strong>
                <small>{r.category.label}</small>
              </div>
              <b>{r.value}</b>
            </Link>
          ))}
        </section>
      )}
      {hall.win_leaders.map((c) => (
        <section className="paper feature-panel" key={c.category.id}>
          <h2>{c.category.label}</h2>
          {c.leaders.map((p) => (
            <Link
              className="record-row"
              key={p.person_id}
              to={"/person/" + p.person_id}
            >
              <span>{p.name}</span>
              <b>
                {p.wins} {p.wins === 1 ? "win" : "wins"}
              </b>
            </Link>
          ))}
        </section>
      ))}
    </div>
  );
}
export function LatestResults({ id }: { id: number }) {
  const { data, error } = useLoad<Game>("/games/" + id);
  if (!data) return <Busy error={error} />;
  return (
    <section className="latest-results">
      <div className="section-title">
        <h2>Latest completed game</h2>
        <Link className="text-link" to={"/game/" + id}>
          Open results →
        </Link>
      </div>
      <GameLink game={data} compact />
      {data.mode === "rounds" ? (
        <CompactSheet game={data} />
      ) : (
        <>
          <Standings game={data} />
          <ResultDetails game={data} />
        </>
      )}
    </section>
  );
}
export function HomeStats() {
  const { me } = useAuth();
  const { data, error } = useLoad<AnalyticsPage>("/statistics?page_size=1");
  return data ? (
    <>
      <SummaryStats summary={data.summary} />
      <p className="scope-note compact-scope">
        {me.user.site_role === "super_admin"
          ? "All-site totals · completed games."
          : "All-time totals from games shared with you."}
      </p>
    </>
  ) : (
    <Busy error={error} />
  );
}
export function HomeHighlights() {
  const { data, error } = useLoad<Hall>("/hall-of-fame?limit=3");
  if (!data) return <Busy error={error} />;
  return (
    <section className="section home-highlights">
      <div className="section-title">
        <h2>Fame & shame</h2>
        <Link className="text-link" to="/leaderboards?tab=records">
          All records →
        </Link>
      </div>
      <div className="record-grid home-records">
        {data.win_leaders.length > 0 && (
          <section className="paper feature-panel">
            <h2>Hall of Fame</h2>
            {data.win_leaders.slice(0, 3).map((c) => (
              <div key={c.category.id}>
                <h3>{c.category.label}</h3>
                {c.leaders.slice(0, 2).map((p) => (
                  <Link
                    key={p.person_id}
                    className="record-row"
                    to={"/person/" + p.person_id}
                  >
                    <span>{p.name}</span>
                    <b>
                      {p.wins} {p.wins === 1 ? "win" : "wins"}
                    </b>
                  </Link>
                ))}
              </div>
            ))}
          </section>
        )}
        <RecordList
          title="Hall of Shame"
          rows={
            data.worst_five_crowns_hands.length
              ? data.worst_five_crowns_hands.slice(0, 3)
              : data.worst_scores.slice(0, 3)
          }
        />
      </div>
      {!data.win_leaders.length &&
        !data.worst_scores.length &&
        !data.worst_five_crowns_hands.length && (
          <p className="empty-state">Finish a game to start the record book.</p>
        )}
      {data.latest_game && <LatestResults id={data.latest_game.id} />}
    </section>
  );
}
export function PersonStatistics() {
  const { id } = useParams(),
    { data, error } = useLoad<
      Omit<AnalyticsPage, "summary"> & {
        person: { id: number; name: string };
        summary: PersonStats;
      }
    >("/people/" + id + "/statistics");
  if (!data) return <Busy error={error} />;
  return (
    <>
      <PageHeading
        eyebrow="A PERSON, ALL THEIR VISIBLE GAME NIGHTS"
        title={data.person.name}
      />
      <PlayerTable players={[data.summary]} categories={data.categories} />
      <GameBriefs games={data.history} />
      <p className="scope-note">
        Lifetime here means authorized games involving this person. Private
        groups remain private.
      </p>
    </>
  );
}
export function GroupStatistics() {
  const { id } = useParams(),
    { data, error } = useLoad<
      AnalyticsPage & { group: { id: number; name: string } }
    >("/groups/" + id + "/statistics");
  if (!data) return <Busy error={error} />;
  return (
    <>
      <PageHeading
        eyebrow="GAMES HOSTED BY THIS GROUP"
        title={data.group.name + " · stats"}
      >
        <Link className="text-link" to={"/groups/" + id}>
          Open group →
        </Link>
      </PageHeading>
      <SummaryStats summary={data.summary} />
      <PlayerTable players={data.players} categories={data.categories} />
      <GameBriefs games={data.history} />
      <p className="scope-note">
        Host-group statistics do not reveal members’ games in other private
        groups.
      </p>
    </>
  );
}

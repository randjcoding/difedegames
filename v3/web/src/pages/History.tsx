import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Busy, PageHeading, useAuth, useLoad } from "../core";
import type { Definition, Game, Person } from "../types";
import { ListFilters, Pagination } from "../ListTools";
import { GameLink } from "./GameLink";
export function History() {
  const { me } = useAuth(),
    [params, setParams] = useSearchParams(),
    [search, setSearch] = useState(params.get("q") || ""),
    catalog = useLoad<{ games: Definition[] }>("/catalog"),
    people = useLoad<{ people: Person[] }>("/people");
  const offset = Math.max(0, Number(params.get("offset")) || 0),
    query = new URLSearchParams(params);
  query.set("limit", "50");
  query.set("offset", String(offset));
  const { data, error } = useLoad<{ games: Game[]; total: number }>(
    "/games?" + query,
  );
  useEffect(() => setSearch(params.get("q") || ""), [params.get("q")]);
  function filter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "offset") next.delete("offset");
    setParams(next);
  }
  return (
    <>
      <PageHeading eyebrow="EVERY ROUND, EVERY RESULT" title="Game history" />
      <ListFilters
        query={search}
        setQuery={setSearch}
        label="Title or custom game name"
        onSearch={() => filter("q", search.trim())}
        onReset={() => {
          setSearch("");
          setParams({});
        }}
      >
        <label>
          Game
          <select
            value={params.get("slug") || ""}
            onChange={(e) => filter("slug", e.target.value)}
          >
            <option value="">Every game</option>
            {catalog.data?.games.map((g) => (
              <option key={g.slug} value={g.slug}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Host group
          <select
            value={params.get("group_id") || ""}
            onChange={(e) => filter("group_id", e.target.value)}
          >
            <option value="">Every visible group</option>
            {me.groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Player
          <select
            value={params.get("person_id") || ""}
            onChange={(e) => filter("person_id", e.target.value)}
          >
            <option value="">Anyone</option>
            {people.data?.people.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          From date
          <input
            type="date"
            value={params.get("from_date") || ""}
            onChange={(e) => filter("from_date", e.target.value)}
          />
        </label>
        <label>
          Through date
          <input
            type="date"
            value={params.get("to_date") || ""}
            onChange={(e) => filter("to_date", e.target.value)}
          />
        </label>
      </ListFilters>
      <div className="segmented">
        {[
          ["", "All games"],
          ["completed", "Completed"],
          ["incomplete", "Ended early"],
          ["active", "Still playing"],
        ].map(([value, label]) => (
          <button
            key={value}
            aria-pressed={(params.get("status") || "") === value}
            onClick={() => filter("status", value)}
          >
            {label}
          </button>
        ))}
      </div>
      {!data ? (
        <Busy error={error} />
      ) : (
        <>
          {data.games.length ? (
            <div className="history-list">
              {data.games.map((g) => (
                <GameLink key={g.id} game={g} compact />
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <h2>No games match these filters.</h2>
              <button
                className="button secondary"
                onClick={() => {
                  setParams({});
                  setSearch("");
                }}
              >
                Reset filters
              </button>
              <Link className="text-link" to="/games">
                Start a game →
              </Link>
            </div>
          )}
          <Pagination
            total={data.total}
            offset={offset}
            limit={50}
            onPage={(n) => filter("offset", String(n))}
          />
        </>
      )}
    </>
  );
}

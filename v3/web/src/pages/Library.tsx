import { useState } from "react";
import { Link } from "react-router-dom";
import type { Definition } from "../types";
import { Art, Busy, ErrorNote, Modal, PageHeading, useLoad } from "../core";
import { FavoriteButton, useFavorites } from "../favorites";
export function Library() {
  const favorites = useFavorites();
  const { data, error } = useLoad<{ games: Definition[] }>("/catalog"),
    [query, setQuery] = useState(""),
    [playerCount, setPlayerCount] = useState(""),
    [onlyFavorites, setOnlyFavorites] = useState(false),
    [rules, setRules] = useState<Definition>();
  if (!data) return <Busy error={error} />;
  const filtered = data.games
    .filter(
      (g) =>
        (g.name + " " + g.description)
          .toLowerCase()
          .includes(query.toLowerCase()) &&
        (!playerCount ||
          (g.min_players <= Number(playerCount) &&
            g.max_players >= Number(playerCount))) &&
        (!onlyFavorites || favorites.games.includes(g.slug)),
    )
    .sort(
      (a, b) =>
        Number(favorites.games.includes(b.slug)) -
          Number(favorites.games.includes(a.slug)) ||
        data.games.indexOf(a) - data.games.indexOf(b),
    );
  return (
    <>
      <PageHeading eyebrow="PICK YOUR KIND OF EVENING" title="Games" />
      <div className="feature-links">
        <Link className="button secondary" to="/setups">
          Saved setups →
        </Link>
      </div>
      <div className="library-toolbar">
        <p>
          {favorites.games.length}{" "}
          {favorites.games.length === 1 ? "favorite" : "favorites"} ·{" "}
          {filtered.length} matching games. Favorites appear first.
        </p>
        <input
          type="search"
          aria-label="Find a game"
          placeholder="Find a game…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <ErrorNote error={favorites.error} />
      <div className="library-extra-filters">
        <label>
          Players
          <input
            type="number"
            min="1"
            max="10"
            value={playerCount}
            onChange={(e) => setPlayerCount(e.target.value)}
            placeholder="Any"
          />
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={onlyFavorites}
            onChange={(e) => setOnlyFavorites(e.target.checked)}
          />
          Favorites only
        </label>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setQuery("");
            setPlayerCount("");
            setOnlyFavorites(false);
          }}
        >
          Reset filters
        </button>
      </div>
      <div className="game-library">
        {filtered.map((g, index) => (
          <article key={g.slug} className="game-card">
            <Link to={"/new/" + g.slug} aria-label={"Start " + g.name}>
              <Art slug={g.slug} />
            </Link>
            <div className="game-card-copy">
              <span className="eyebrow">
                {g.slug.startsWith("uno-")
                  ? "UNO COLLECTION"
                  : `NO. ${String(index + 1).padStart(2, "0")}`}{" "}
                · {g.min_players}–{g.max_players} PLAYERS
              </span>
              <h2>
                <Link to={"/new/" + g.slug}>{g.name}</Link>
              </h2>
              <p>{g.description}</p>
              <FavoriteButton
                active={favorites.games.includes(g.slug)}
                disabled={favorites.busy}
                label={g.name}
                onClick={() => void favorites.toggleGame(g.slug)}
              />
              <div className="game-card-actions">
                <Link to={"/new/" + g.slug} className="text-link">
                  Start game →
                </Link>
                <button className="text-button" onClick={() => setRules(g)}>
                  Scoring notes
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>
      {!filtered.length && (
        <p className="empty-state">No games match “{query}”.</p>
      )}
      <Modal
        open={!!rules}
        onOpenChange={(open) => {
          if (!open) setRules(undefined);
        }}
        title={rules?.name || "Scoring notes"}
        description="Published rules and the scoring supported in this pilot."
      >
        <p>{rules?.rule_note}</p>
        {rules?.rule_source && (
          <a
            href={rules.rule_source}
            target="_blank"
            rel="noreferrer"
            className="text-link"
          >
            Read the source ↗
          </a>
        )}
        <p className="small-note">
          Play happens around your table. This app records your adjusted scores;
          it does not enforce card play.
        </p>
      </Modal>
    </>
  );
}

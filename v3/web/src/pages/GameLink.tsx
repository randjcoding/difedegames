import { Link } from "react-router-dom";
import type { Game } from "../types";
import { Art } from "../core";
export function GameLink({
  game,
  compact = false,
}: {
  game: Game;
  compact?: boolean;
}) {
  return (
    <Link
      to={"/game/" + game.id}
      className={"session-row " + (compact ? "compact" : "")}
    >
      <Art slug={game.slug} />
      <div className="session-copy">
        <span className="eyebrow">
          {game.status === "active"
            ? game.permissions.can_score
              ? "YOUR TABLE"
              : "WATCH LIVE"
            : game.status === "incomplete"
              ? "ENDED EARLY"
              : "COMPLETED"}
          {game.imported ? " · V2 HISTORY" : ""}
        </span>
        <h3>{game.title}</h3>
        <p>{game.players.map((p) => p.name).join(", ")}</p>
        <p className="session-result">
          {new Date(game.finished_at || game.created_at).toLocaleDateString(
            undefined,
            { month: "short", day: "numeric", year: "numeric" },
          )}
          {game.status === "completed" &&
          game.imported_result &&
          (!game.result_source || game.result_source === "v2-original")
            ? " · Original result: " +
              (game.imported_result.winners.length
                ? game.imported_result.winners
                    .map(
                      (w) =>
                        (w.player_id == null
                          ? "Winner not recorded"
                          : game.players.find((p) => p.id === w.player_id)
                              ?.name ||
                            game.standings.find(
                              (p) => p.player_id === w.player_id,
                            )?.name ||
                            "Recorded winner") +
                        (w.winning_score == null
                          ? " · score not recorded"
                          : " · " + w.winning_score + " pts") +
                        (w.is_tie === true ? " · shared win" : ""),
                    )
                    .join("; ")
                : "No winner recorded")
            : game.status === "completed" &&
                game.standings.some((s) => s.winner)
              ? " · " +
                game.standings
                  .filter((s) => s.winner)
                  .map((s) => s.name)
                  .join(" & ") +
                (game.standings.filter((s) => s.winner).length > 1
                  ? " shared first"
                  : " won") +
                (game.mode === "winner"
                  ? ""
                  : " · " +
                    game.standings.find((s) => s.winner)?.total +
                    " pts")
              : game.status === "incomplete"
                ? " · No official winner"
                : ""}
        </p>
      </div>
      <span className="session-cta">
        {game.status === "active"
          ? game.permissions.can_score
            ? "Resume"
            : "Watch"
          : "View"}{" "}
        <span aria-hidden="true">↗</span>
      </span>
    </Link>
  );
}

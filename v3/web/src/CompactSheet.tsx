import type { CSSProperties } from "react";
import { findScore } from "./types";
import type { Game } from "./types";
export function CompactSheet({
  game,
  draftValue,
  onRound,
}: {
  game: Game;
  draftValue?: (player: number, round: number) => string | undefined;
  onRound?: (number: number) => void;
}) {
  return (
    <section
      className="paper sheet"
      style={{ "--sheet-players": game.players.length } as CSSProperties}
    >
      <div className="section-title">
        <h2>Full score sheet</h2>
        {game.players.length > 3 && (
          <span className="small-note">
            Swipe the overview. Round entry stays phone-friendly.
          </span>
        )}
      </div>
      <div
        className="table-scroll"
        tabIndex={game.players.length > 3 ? 0 : undefined}
        role="region"
        aria-label={game.title + " full score sheet"}
      >
        <table>
          <thead>
            <tr>
              <th scope="col">Round</th>
              {game.players.map((p) => (
                <th key={p.id} scope="col">
                  {p.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {game.rounds.map((r) => (
              <tr key={r.number}>
                <th scope="row">
                  {onRound ? (
                    <button
                      className="text-button"
                      onClick={() => onRound(r.number)}
                    >
                      {r.label}
                      {r.side ? " · " + r.side : ""}
                    </button>
                  ) : (
                    <span>
                      {r.label}
                      {r.side ? " · " + r.side : ""}
                    </span>
                  )}
                </th>
                {game.players.map((p) => (
                  <td key={p.id}>
                    {draftValue?.(p.id, r.number) ??
                      findScore(game, p.id, r.number)?.value ??
                      "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">Total</th>
              {game.players.map((p) => (
                <td key={p.id}>
                  {game.standings.find((s) => s.player_id === p.id)?.total ?? 0}
                </td>
              ))}
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}

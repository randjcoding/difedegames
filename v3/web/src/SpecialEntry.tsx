import type { Dispatch, SetStateAction } from "react";
import type { Game } from "./types";
import { pegColors } from "./special";
export type PegDraft = Record<
  number,
  { finish: number; board: number; color: string }
>;
type Props = {
  game: Game;
  winner: number;
  setWinner: Dispatch<SetStateAction<number>>;
  pegs: PegDraft;
  setPegs: Dispatch<SetStateAction<PegDraft>>;
  penalties: Record<number, string>;
  setPenalties: Dispatch<SetStateAction<Record<number, string>>>;
  specialTotal?: number;
  busy: boolean;
  onReview: () => void;
  reviewLabel?: string;
};
export function SpecialEntry({
  game,
  winner,
  setWinner,
  pegs,
  setPegs,
  penalties,
  setPenalties,
  specialTotal,
  busy,
  onReview,
  reviewLabel = "Review & finish →",
}: Props) {
  return (
    <section className="paper special-entry">
      <span className="eyebrow">THE LAST MOVE</span>
      <h2>Record your result.</h2>
      <p className="small-note">
        Result draft kept on this device. Everyone sees the recorded result when
        you finish.
      </p>
      <p className="muted">
        Choose your winner.{" "}
        {game.slug === "trouble"
          ? "Then count the other players’ pegs."
          : game.slug === "kings-corner"
            ? "Then enter the points left in each opponent’s hand."
            : "Keep the memory without inventing a score."}
      </p>
      <label>
        Who won?
        <select
          value={winner || ""}
          onChange={(e) => setWinner(Number(e.target.value))}
        >
          <option value="" disabled>
            Choose a player
          </option>
          {game.players.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      {game.slug === "trouble" && (
        <>
          <p className="small-note">
            Finish + Board + Home = 4 pegs each. The winner has all four in
            Finish. No photo recognition is used in this pilot.
          </p>
          {game.players.map((p, index) => (
            <div className="peg-row" key={p.id}>
              <h3>
                {p.name}
                {p.id === winner && <span className="tag">Winner</span>}
              </h3>
              <label>
                Peg color
                <select
                  value={pegs[p.id]?.color || pegColors[index % 4]}
                  onChange={(e) =>
                    setPegs((v) => ({
                      ...v,
                      [p.id]: { ...v[p.id], color: e.target.value },
                    }))
                  }
                >
                  {pegColors.map((c) => (
                    <option key={c} value={c}>
                      {c.charAt(0).toUpperCase() + c.slice(1)}
                    </option>
                  ))}
                </select>
              </label>
              {p.id !== winner ? (
                <div className="peg-counts">
                  <label>
                    In Finish
                    <input
                      type="number"
                      inputMode="numeric"
                      min="0"
                      max="3"
                      value={pegs[p.id]?.finish ?? 0}
                      onChange={(e) =>
                        setPegs((v) => ({
                          ...v,
                          [p.id]: {
                            ...v[p.id],
                            finish: Number(e.target.value),
                          },
                        }))
                      }
                    />
                  </label>
                  <label>
                    On Board
                    <input
                      type="number"
                      inputMode="numeric"
                      min="0"
                      max="4"
                      value={pegs[p.id]?.board ?? 0}
                      onChange={(e) =>
                        setPegs((v) => ({
                          ...v,
                          [p.id]: { ...v[p.id], board: Number(e.target.value) },
                        }))
                      }
                    />
                  </label>
                  <div className="home-count">
                    <span>At Home</span>
                    <strong>
                      {4 - (pegs[p.id]?.finish || 0) - (pegs[p.id]?.board || 0)}
                    </strong>
                  </div>
                </div>
              ) : (
                <p>4 in Finish · 0 on Board · 0 at Home</p>
              )}
            </div>
          ))}
          <p className="small-note">
            Custom Strength of Win: 25 + 5 per opponent peg on Board + 10 per
            opponent peg at Home.
          </p>
        </>
      )}
      {game.slug === "kings-corner" &&
        winner > 0 &&
        game.players
          .filter((p) => p.id !== winner)
          .map((p) => (
            <label key={p.id} className="penalty-row">
              {p.name}
              <span>Remaining-card penalty</span>
              <input
                type="text"
                inputMode="numeric"
                value={penalties[p.id] || ""}
                placeholder="Enter points, including 0"
                onChange={(e) =>
                  setPenalties((v) => ({ ...v, [p.id]: e.target.value }))
                }
              />
            </label>
          ))}
      {specialTotal !== undefined && (
        <div className="special-total">
          <span>Winner’s Strength of Win</span>
          <strong>
            {specialTotal}
            <small> pts</small>
          </strong>
        </div>
      )}
      <button
        className="button primary wide"
        disabled={!winner || busy}
        onClick={onReview}
      >
        {reviewLabel}
      </button>
    </section>
  );
}

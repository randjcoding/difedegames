import type { Game } from "./types";
export function Standings({ game }: { game: Game }) {
  const original =
    !!game.imported_result &&
    (!game.result_source || game.result_source === "v2-original");
  return (
    <section className="standings paper">
      <div className="section-title">
        <div>
          <span className="eyebrow">
            {game.status === "active"
              ? "AROUND THE TABLE"
              : "THE FINAL CHAPTER"}
          </span>
          <h2>
            {original
              ? "Totals from saved scores."
              : game.status === "active"
                ? "How the evening stands."
                : game.status === "incomplete"
                  ? "Saved, unfinished."
                  : "Well played, everyone."}
          </h2>
        </div>
        <span className="tag">{game.players.length} players</span>
      </div>
      {game.standings.length
        ? game.standings.map((s) => (
            <div
              className={
                "standing-row " +
                (s.winner && game.status === "completed" && !original
                  ? "winner"
                  : "")
              }
              key={s.player_id}
            >
              <span className="standing-rank">{s.rank}</span>
              <div>
                <h3>{s.name}</h3>
                {s.winner && game.status === "completed" && !original && (
                  <span className="eyebrow">
                    {game.standings.filter((p) => p.winner).length > 1
                      ? "SHARED FIRST PLACE"
                      : "WINNER"}
                  </span>
                )}
              </div>
              {game.mode !== "winner" && (
                <strong>
                  {s.total}
                  <small> pts</small>
                </strong>
              )}
            </div>
          ))
        : game.players.map((p, i) => (
            <div className="standing-row" key={p.id}>
              <span className="standing-rank">{i + 1}</span>
              <h3>{p.name}</h3>
              <span className="muted">Awaiting scores</span>
            </div>
          ))}
      {game.status === "active" && (
        <p className="small-note">
          Saved scores update live. A blank score is not a zero.
        </p>
      )}
      {game.imported && (
        <p className="small-note">
          Original V2 results are preserved, without recalculation under new
          rules.
        </p>
      )}
    </section>
  );
}

export function ResultDetails({ game }: { game: Game }) {
  if (game.mode !== "single" || game.status === "active") return null;
  const details = game.players.map((p) => ({
    player: p,
    metadata: game.scores.find((s) => s.player_id === p.id)?.metadata,
  }));
  if (!details.some((d) => d.metadata && Object.keys(d.metadata).length))
    return null;
  return (
    <section className="paper result-details">
      <span className="eyebrow">THE RESULT, RECORDED</span>
      <h2>
        {game.slug === "trouble"
          ? "Every peg accounted for."
          : "What was left in hand."}
      </h2>
      {details.map(({ player, metadata }) => {
        if (game.slug === "trouble") {
          const pegs = metadata?.pegs as
            | { finish?: number; board?: number; home?: number; color?: string }
            | undefined;
          const finish = pegs?.finish ?? metadata?.pegs_finish,
            board = pegs?.board ?? metadata?.pegs_board,
            home = pegs?.home ?? metadata?.pegs_home;
          return finish !== undefined ? (
            <div className="result-detail-row" key={player.id}>
              <h3>
                {player.name}
                <small>{pegs?.color || player.color}</small>
              </h3>
              <p>
                Finish <strong>{String(finish)}</strong> · Board{" "}
                <strong>{String(board ?? "—")}</strong> · Home{" "}
                <strong>{String(home ?? "—")}</strong>
              </p>
            </div>
          ) : null;
        }
        return metadata?.penalty !== undefined ? (
          <div className="result-detail-row" key={player.id}>
            <h3>{player.name}</h3>
            <p>{String(metadata.penalty)} remaining-card penalty points</p>
          </div>
        ) : null;
      })}
      <p className="small-note">
        {game.slug === "trouble"
          ? "Custom Strength of Win: 25 + 5 per opponent peg on Board + 10 per opponent peg at Home."
          : "Winner’s Strength of Win is the sum of the opponents’ remaining-card penalties."}
      </p>
    </section>
  );
}

export function OriginalResult({ game }: { game: Game }) {
  if (!game.imported_result) return null;
  return (
    <section className="paper original-result">
      <span className="eyebrow">PRESERVED V2 HISTORY</span>
      <h2>Original recorded result.</h2>
      {game.imported_result.winners.length ? (
        game.imported_result.winners.map((w, i) => (
          <div className="result-detail-row" key={i}>
            <h3>
              {w.player_id == null
                ? "Winner not recorded"
                : game.players.find((p) => p.id === w.player_id)?.name ||
                  game.standings.find((p) => p.player_id === w.player_id)
                    ?.name ||
                  "Recorded winner"}
            </h3>
            <p>
              {w.winning_score == null
                ? "Winning score not recorded"
                : `${w.winning_score} recorded winning points`}
              {w.is_tie === null
                ? " · Tie status not recorded"
                : w.is_tie
                  ? " · Shared win"
                  : " · Winner"}
            </p>
          </div>
        ))
      ) : (
        <p>No winner was recorded in the original result.</p>
      )}
      <p className="small-note">
        Preserved exactly as originally recorded. Saved-score totals below are
        shown separately and do not replace this result.
      </p>
    </section>
  );
}

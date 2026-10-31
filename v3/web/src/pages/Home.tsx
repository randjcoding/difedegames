import { Link } from "react-router-dom";
import { Busy, PageHeading, useAuth, useLoad } from "../core";
import type { Dashboard } from "../types";
import { GameLink } from "./GameLink";
import { HomeHighlights, HomeStats } from "./Analytics";
export function Home() {
  const { me } = useAuth(),
    { data, error } = useLoad<Dashboard>("/dashboard");
  if (!data) return <Busy error={error} />;
  return (
    <>
      <PageHeading
        eyebrow="READY FOR YOUR NEXT ROUND?"
        title={`Welcome back, ${(me.user.display_name || me.user.name).split(" ")[0]}.`}
      >
        <div className="header-actions">
          <Link className="button primary" to="/games">
            Start game +
          </Link>
          {me.user.site_role === "super_admin" && (
            <Link className="text-link" to="/admin">
              Administration →
            </Link>
          )}
        </div>
      </PageHeading>
      <HomeStats />
      <section className="section live-tables">
        <div className="section-title">
          <h2>Resume or watch</h2>
          <span className="count">{data.active.length} live</span>
        </div>
        {data.active.length ? (
          <div className="session-list">
            {data.active.map((g) => (
              <GameLink key={g.id} game={g} />
            ))}
          </div>
        ) : (
          <div className="empty-state">
            <h3>No active games yet.</h3>
            <p>Start a game or ask your group to share one.</p>
            <Link className="button primary" to="/games">
              Play a game →
            </Link>
          </div>
        )}
      </section>
      <HomeHighlights />
      <section className="section">
        <div className="section-title">
          <h2>Recent games</h2>
          <Link className="text-link" to="/history">
            Game history →
          </Link>
        </div>
        {data.recent.length ? (
          data.recent
            .slice(0, 3)
            .map((g) => <GameLink key={g.id} game={g} compact />)
        ) : (
          <p className="muted">Completed games will appear here.</p>
        )}
      </section>
      <div className="quiet-stats">
        <span>
          <strong>{data.stats.games}</strong> games you played
        </span>
        <span>
          <strong>{data.stats.wins}</strong> recorded wins
        </span>
        <Link to="/people">People & groups →</Link>
      </div>
    </>
  );
}

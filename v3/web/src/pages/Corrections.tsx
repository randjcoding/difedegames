import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Busy, ErrorNote, Modal, PageHeading, useLoad } from "../core";
import type { Definition, Game } from "../types";
import { findScore, operationId } from "../types";
import { parseScore } from "../score-queue";
import { kingsResult, pegColors, troubleResult } from "../special";
import { SpecialEntry } from "../SpecialEntry";
import type { PegDraft } from "../SpecialEntry";
import { AuditList } from "./Admin";
import type { AuditEvent } from "./Admin";
export function Corrections() {
  const { id } = useParams(),
    game = useLoad<Game>("/games/" + id),
    catalog = useLoad<{ games: Definition[] }>("/catalog");
  if (!game.data || !catalog.data)
    return <Busy error={game.error || catalog.error} />;
  const def = catalog.data.games.find((g) => g.slug === game.data!.slug);
  if (!game.data.permissions.can_manage || !def)
    return (
      <ErrorNote error="You need organizer or group administrator permission to correct this game." />
    );
  return <CorrectionEditor initial={game.data} definition={def} />;
}
function CorrectionEditor({
  initial,
  definition,
}: {
  initial: Game;
  definition: Definition;
}) {
  const nav = useNavigate(),
    [game, setGame] = useState(initial),
    [reason, setReason] = useState(""),
    [values, setValues] = useState<Record<string, string>>({}),
    [winner, setWinner] = useState(
      initial.standings.find((p) => p.winner)?.player_id || 0,
    ),
    [penalties, setPenalties] = useState<Record<number, string>>(() =>
      Object.fromEntries(
        initial.scores
          .filter((s) => s.metadata?.type === "loser")
          .map((s) => [s.player_id, String(s.metadata?.penalty ?? "")]),
      ),
    ),
    [pegs, setPegs] = useState<PegDraft>(() =>
      Object.fromEntries(
        initial.players.map((p, i) => {
          const meta = initial.scores.find((s) => s.player_id === p.id)
            ?.metadata?.pegs as { finish?: number; board?: number } | undefined;
          return [
            p.id,
            {
              finish: meta?.finish || 0,
              board: meta?.board || 0,
              color: p.color || pegColors[i % 4],
            },
          ];
        }),
      ),
    ),
    [review, setReview] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(),
    [operation, setOperation] = useState(operationId());
  let strength: number | undefined;
  try {
    if (winner && game.slug === "trouble")
      strength = troubleResult(game.players, winner, pegs).total;
    else if (winner && game.slug === "kings-corner" && game.mode === "single")
      strength = kingsResult(game.players, winner, penalties).total;
  } catch {}
  async function save() {
    setBusy(true);
    setError(undefined);
    try {
      if (reason.trim().length < 3)
        throw new Error(
          "Give a reason of at least three characters for this correction.",
        );
      const scores = Object.entries(values).map(([key, raw]) => {
        const [player_id, round_number] = key.split(":").map(Number);
        return {
          player_id,
          round_number,
          score: parseScore(raw, definition.allow_negative),
        };
      });
      const details =
        game.slug === "trouble"
          ? { pegs: troubleResult(game.players, winner, pegs).pegs }
          : game.slug === "kings-corner" && game.mode === "single"
            ? {
                penalties: kingsResult(game.players, winner, penalties)
                  .penalties,
              }
            : undefined;
      await api<Game>(`/admin/games/${game.id}/corrections`, "POST", {
        operation_id: operation,
        expected_revision: game.revision,
        reason: reason.trim(),
        ...(game.mode === "rounds"
          ? { scores }
          : { winner_id: winner, ...(details ? { details } : {}) }),
      });
      nav("/game/" + game.id);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    try {
      const next = await api<Game>("/games/" + game.id);
      setGame(next);
      setValues({});
      setReason("");
      setWinner(next.standings.find((p) => p.winner)?.player_id || 0);
      setPenalties(
        Object.fromEntries(
          next.scores
            .filter((s) => s.metadata?.type === "loser")
            .map((s) => [s.player_id, String(s.metadata?.penalty ?? "")]),
        ),
      );
      setPegs(
        Object.fromEntries(
          next.players.map((p, i) => {
            const m = next.scores.find((s) => s.player_id === p.id)?.metadata
              ?.pegs as { finish?: number; board?: number } | undefined;
            return [
              p.id,
              {
                finish: m?.finish || 0,
                board: m?.board || 0,
                color: p.color || pegColors[i % 4],
              },
            ];
          }),
        ),
      );
      setOperation(operationId());
      setReview(false);
      setError(undefined);
    } catch (e) {
      setError(e);
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="AN EXPLICIT, AUDITED CORRECTION"
        title={"Correct " + game.title}
      >
        <Link to={"/game/" + game.id} className="text-link">
          ← Game results
        </Link>
      </PageHeading>
      <p className="scope-note">
        Original imported results remain preserved. This changes the current
        official result only after the complete correction is validated. An
        unchanged score is left untouched.
      </p>
      <ErrorNote error={error} />
      <label className="paper feature-panel">
        Reason for the correction
        <textarea
          required
          minLength={3}
          value={reason}
          onChange={(e) => {
            setReason(e.target.value);
            setOperation(operationId());
          }}
          maxLength={1000}
          rows={3}
        />
      </label>
      {game.mode === "rounds" ? (
        <section className="paper correction-scores">
          <h2>Recorded scores</h2>
          {game.rounds.map((r) => (
            <details key={r.number}>
              <summary>Round {r.label}</summary>
              {game.players.map((p) => {
                const key = `${p.id}:${r.number}`;
                return (
                  <label className="correction-row" key={key}>
                    <span>{p.name}</span>
                    <input
                      type="text"
                      inputMode="numeric"
                      value={
                        values[key] ??
                        (findScore(game, p.id, r.number)?.value == null
                          ? ""
                          : String(findScore(game, p.id, r.number)!.value))
                      }
                      onChange={(e) => {
                        setValues((v) => ({ ...v, [key]: e.target.value }));
                        setOperation(operationId());
                      }}
                      placeholder="Blank"
                    />
                  </label>
                );
              })}
            </details>
          ))}
          <button
            className="button primary"
            disabled={reason.trim().length < 3 || !Object.keys(values).length}
            onClick={() => setReview(true)}
          >
            Review correction
          </button>
        </section>
      ) : (
        <SpecialEntry
          game={game}
          winner={winner}
          setWinner={(v) => {
            setWinner(v);
            setOperation(operationId());
          }}
          pegs={pegs}
          setPegs={(v) => {
            setPegs(v);
            setOperation(operationId());
          }}
          penalties={penalties}
          setPenalties={(v) => {
            setPenalties(v);
            setOperation(operationId());
          }}
          specialTotal={strength}
          busy={busy}
          onReview={() => {
            if (reason.trim().length >= 3) setReview(true);
            else setError(new Error("Enter a reason before reviewing."));
          }}
          reviewLabel="Review correction →"
        />
      )}
      <Modal
        busy={busy}
        open={review}
        onOpenChange={setReview}
        title="Apply this correction?"
        description="The result and audit record update together. A competing edit will block this correction rather than overwrite it."
      >
        <p>{reason}</p>
        <ErrorNote error={error} />
        {game.mode === "rounds" ? (
          <div className="correction-review">
            {Object.entries(values).map(([key, value]) => {
              const [player, round] = key.split(":").map(Number);
              return (
                <p key={key}>
                  <strong>
                    {game.players.find((p) => p.id === player)?.name} · Round{" "}
                    {game.rounds.find((r) => r.number === round)?.label}
                  </strong>
                  <span>
                    Saved {findScore(game, player, round)?.value ?? "blank"} →{" "}
                    {value === "" ? "blank" : value}
                  </span>
                </p>
              );
            })}
          </div>
        ) : (
          <p>
            Selected winner: {game.players.find((p) => p.id === winner)?.name}.{" "}
            {strength != null ? "Strength of Win: " + strength : ""}
          </p>
        )}
        <div className="dialog-actions">
          <button
            className="button secondary"
            disabled={busy}
            onClick={refresh}
          >
            Discard edits and reload saved result
          </button>
          <button className="button primary" disabled={busy} onClick={save}>
            {busy ? "Saving…" : "Apply correction"}
          </button>
        </div>
      </Modal>
    </>
  );
}
export function GameAudit() {
  const { id } = useParams(),
    { data, error } = useLoad<{ events: AuditEvent[] }>(
      "/games/" + id + "/audit",
    );
  return (
    <>
      <PageHeading
        eyebrow="CHANGES WITH ACCOUNTABILITY"
        title="Game audit trail"
      >
        <Link className="text-link" to={"/game/" + id}>
          ← Game
        </Link>
      </PageHeading>
      {data ? <AuditList events={data.events} /> : <Busy error={error} />}
    </>
  );
}

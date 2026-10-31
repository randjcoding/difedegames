import { ApiError } from "./api";
import { cellKey, findScore, operationId } from "./types";
import type { Game } from "./types";
export type Draft = {
  player: number;
  round: number;
  raw: string;
  base: number;
  operation: string;
  status: "draft" | "saving" | "error" | "conflict";
  error?: string;
};
export type ScoreCommand = {
  operation_id: string;
  player_id: number;
  round_number: number;
  score: number | null;
  expected_version: number;
};
export function parseScore(raw: string, allowNegative: boolean): number | null {
  if (raw.trim() === "") return null;
  if (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw)))
    throw new Error("Enter a whole number, or clear the score.");
  if (!allowNegative && Number(raw) < 0)
    throw new Error("This game uses zero or positive scores.");
  return Number(raw);
}
export class ScoreQueue {
  drafts: Record<string, Draft> = {};
  storageError = false;
  private flight = new Map<string, Promise<boolean>>();
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  constructor(
    private storage: Pick<Storage, "getItem" | "setItem">,
    private storageKey: string,
    private getGame: () => Game,
    private post: (command: ScoreCommand) => Promise<Game>,
    private applyGame: (game: Game) => void,
    private changed: () => void,
    private allowNegative: boolean,
  ) {
    try {
      const saved = JSON.parse(storage.getItem(storageKey) || "{}") as Record<
        string,
        Draft
      >;
      for (const [key, draft] of Object.entries(saved))
        if (
          draft &&
          typeof draft.raw === "string" &&
          Number.isInteger(draft.base) &&
          draft.operation
        )
          this.drafts[key] = {
            ...draft,
            status: draft.status === "conflict" ? "conflict" : "error",
            error: "Recovered draft. Review and save when ready.",
          };
    } catch {
      this.storageError = true;
    }
  }
  private emit() {
    try {
      this.storage.setItem(this.storageKey, JSON.stringify(this.drafts));
    } catch {
      this.storageError = true;
    }
    this.changed();
  }
  edit(player: number, round: number, raw: string) {
    const key = cellKey(player, round),
      previous = this.drafts[key];
    this.drafts[key] = {
      player,
      round,
      raw,
      base:
        previous?.base ??
        findScore(this.getGame(), player, round)?.version ??
        0,
      operation: operationId(),
      status: previous?.status === "conflict" ? "conflict" : "draft",
    };
    this.emit();
    clearTimeout(this.timers.get(key));
    this.timers.set(
      key,
      setTimeout(() => {
        if (this.drafts[key]?.status === "draft") void this.save(key);
      }, 650),
    );
  }
  async save(key: string): Promise<boolean> {
    clearTimeout(this.timers.get(key));
    if (this.flight.has(key)) {
      await this.flight.get(key);
      return this.drafts[key]?.status === "draft"
        ? this.save(key)
        : !this.drafts[key];
    }
    const draft = this.drafts[key];
    if (!draft) return true;
    if (draft.status === "conflict") return false;
    let score: number | null;
    try {
      score = parseScore(draft.raw, this.allowNegative);
    } catch (e) {
      this.drafts[key] = {
        ...draft,
        status: "error",
        error: (e as Error).message,
      };
      this.emit();
      return false;
    }
    const sent = { ...draft };
    this.drafts[key] = { ...draft, status: "saving", error: undefined };
    this.emit();
    const request = this.post({
      operation_id: sent.operation,
      player_id: sent.player,
      round_number: sent.round,
      score,
      expected_version: sent.base,
    })
      .then((game) => {
        this.applyGame(game);
        const latest = this.drafts[key];
        if (latest?.operation === sent.operation) delete this.drafts[key];
        else if (latest)
          this.drafts[key] = {
            ...latest,
            base: findScore(game, sent.player, sent.round)?.version ?? 0,
            status: "draft",
          };
        return true;
      })
      .catch((e: unknown) => {
        const latest = this.drafts[key];
        if (latest)
          this.drafts[key] = {
            ...latest,
            status:
              e instanceof ApiError && e.status === 409 ? "conflict" : "error",
            error: e instanceof Error ? e.message : "Score could not be saved.",
          };
        return false;
      })
      .finally(() => {
        this.flight.delete(key);
        this.emit();
      });
    this.flight.set(key, request);
    const ok = await request;
    if (ok && this.drafts[key]?.status === "draft") return this.save(key);
    return ok;
  }
  acceptServer(key: string) {
    delete this.drafts[key];
    this.emit();
  }
  keepDraft(key: string) {
    const d = this.drafts[key];
    if (!d) return;
    this.drafts[key] = {
      ...d,
      base: findScore(this.getGame(), d.player, d.round)?.version ?? 0,
      operation: operationId(),
      status: "draft",
      error: undefined,
    };
    this.emit();
    void this.save(key);
  }
  async flush() {
    const results = await Promise.all(
      Object.keys(this.drafts).map((key) => this.save(key)),
    );
    return results.every(Boolean) && Object.keys(this.drafts).length === 0;
  }
  dispose() {
    this.timers.forEach(clearTimeout);
  }
}

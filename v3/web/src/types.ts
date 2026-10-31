export type Person = {
  id: number;
  name: string;
  has_account: boolean;
  account_id?: number | null;
  group_ids: number[];
};
export type Group = { id: number; name: string; role: "admin" | "member" };
export type Preferences = {
  style: string;
  palette: string;
  favorite_game_slugs: string[];
  favorite_person_ids: number[];
};
export type Me = {
  user: {
    id: number;
    email: string;
    name: string;
    display_name?: string;
    person_id: number;
    site_role?: "member" | "super_admin";
    is_owner?: boolean;
  };
  groups: Group[];
  csrf_token: string;
  preferences?: Preferences;
};
export type Definition = {
  slug: string;
  name: string;
  description: string;
  scoring_direction: "low" | "high";
  min_players: number;
  max_players: number;
  fixed_rounds: number[] | null;
  round_labels: Record<string, string>;
  modes: string[];
  allow_negative: boolean;
  has_dealer: boolean;
  targets: number[];
  default_target: number | null;
  rule_note: string;
  rule_source?: string;
  image: string;
};
export type Score = {
  player_id: number;
  round_number: number;
  value: number | null;
  version: number;
  metadata?: Record<string, unknown>;
};
export type Game = {
  id: number;
  slug: string;
  title: string;
  status: "active" | "completed" | "incomplete";
  revision: number;
  mode: string;
  scoring_direction: "low" | "high";
  target_score: number | null;
  group_id: number;
  viewer_group_ids: number[];
  players: { id: number; name: string; color?: string }[];
  rounds: { number: number; label: string; side?: "light" | "dark" }[];
  scores: Score[];
  standings: {
    player_id: number;
    name: string;
    total: number;
    rank: number;
    winner: boolean;
  }[];
  permissions: { can_score: boolean; can_manage: boolean };
  created_at: string;
  finished_at?: string;
  imported: boolean;
  result_source?: null | "v2-original" | "v3-completion" | "v3-correction";
  imported_result?: {
    winners: {
      player_id: number | null;
      winning_score: number | null;
      is_tie: boolean | null;
    }[];
  };
  rule_note?: string;
};
export type Dashboard = {
  active: Game[];
  recent: Game[];
  stats: { games: number; wins: number };
  people: Person[];
  groups: Group[];
};
export const cellKey = (player: number, round: number) => `${player}:${round}`;
export const findScore = (game: Game, player: number, round: number) =>
  game.scores.find((s) => s.player_id === player && s.round_number === round);
export const operationId = () => crypto.randomUUID();

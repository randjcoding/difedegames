export type Category = {
  id: string;
  slug: string;
  name: string;
  title: string | null;
  mode: string;
  scoring_direction: string;
  score_metric: string;
  label: string;
};
export type PersonStats = {
  person_id: number;
  name: string;
  played: number;
  known_results: number;
  unknown_results: number;
  solo_wins: number;
  shared_wins: number;
  wins: number;
  losses: number;
  ties: number;
  win_percentage: number | null;
  winning_scores: {
    category_id: string;
    scored_wins: number;
    average: number;
    best: number;
  }[];
};
export type Brief = {
  id: number;
  title: string;
  slug: string;
  mode: string;
  scoring_direction: string;
  group_id: number;
  category_id: string;
  finished_at: string | null;
  created_at: string;
  player_count: number;
  result_known: boolean;
  winners: {
    player_id: number;
    name: string;
    winning_score: number | null;
    is_tie: boolean;
  }[];
};
export type Summary = {
  games: number;
  known_results: number;
  unknown_results: number;
  players: number;
  player_appearances: number;
  solo_wins: number;
  shared_wins: number;
};
export type AnalyticsPage = {
  scope: string;
  summary: Summary;
  categories: { category: Category; games: number }[];
  players: PersonStats[];
  history: Brief[];
  pagination: { page: number; page_size: number; total: number; pages: number };
  labels: Record<string, string>;
};
export type ScoreRecord = {
  category: Category;
  game: Brief;
  person_id: number;
  name: string;
  value: number;
  round_number?: number;
};
export type Hall = {
  latest_game: Brief | null;
  biggest_beatdowns: {
    category: Category;
    game: Brief;
    value: number;
    low_total: number;
    high_total: number;
  }[];
  worst_five_crowns_hands: ScoreRecord[];
  win_leaders: { category: Category; leaders: PersonStats[] }[];
  best_scores: ScoreRecord[];
  worst_scores: ScoreRecord[];
  trouble_strength: ScoreRecord[];
  labels: Record<string, string>;
};

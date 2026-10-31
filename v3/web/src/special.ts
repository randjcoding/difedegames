import type { Game } from './types';
export const pegColors = ['red', 'blue', 'green', 'yellow'];
export type Peg = { player_id: number; color: string; finish: number; board: number; home: number };
export function troubleResult(players: Game['players'], winner: number, values: Record<number, { finish: number; board: number; color: string }>) {
  if (!players.some(p => p.id === winner)) throw new Error('Choose the winner first.');
  const pegs: Peg[] = players.map((p, i) => {
    const value = values[p.id] || { finish: 0, board: 0, color: pegColors[i % 4] };
    const finish = p.id === winner ? 4 : value.finish, board = p.id === winner ? 0 : value.board, home = 4 - finish - board;
    if (![finish, board, home].every(n => Number.isInteger(n) && n >= 0 && n <= 4) || (p.id !== winner && finish === 4)) throw new Error(`Check ${p.name}’s pegs: Finish + Board + Home must equal four, and only the winner has four in Finish.`);
    return { player_id: p.id, color: value.color, finish, board, home };
  });
  if (new Set(pegs.map(p => p.color)).size !== pegs.length) throw new Error('Give each player a different peg color.');
  const total = 25 + pegs.filter(p => p.player_id !== winner).reduce((sum, p) => sum + p.board * 5 + p.home * 10, 0);
  return { pegs, total };
}
export function kingsResult(players: Game['players'], winner: number, values: Record<number, string>) {
  if (!players.some(p => p.id === winner)) throw new Error('Choose the winner first.');
  const penalties = players.filter(p => p.id !== winner).map(p => {
    const raw = values[p.id];
    if (raw == null || raw.trim() === '' || !/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) throw new Error(`Enter ${p.name}’s remaining-card penalty, including zero.`);
    return { player_id: p.id, score: Number(raw) };
  });
  return { penalties, total: penalties.reduce((sum, p) => sum + p.score, 0) };
}

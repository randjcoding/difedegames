import { describe, expect, it } from 'vitest';
import { kingsResult, troubleResult } from './special';
const players = [{ id: 1, name: 'Alex' }, { id: 2, name: 'Sam' }, { id: 3, name: 'Jo' }];
describe('specialty result forms', () => {
  it('calculates exact Trouble SOW with winner excluded from opponent bonuses', () => { const result = troubleResult(players, 1, { 1: { color: 'red', finish: 0, board: 4 }, 2: { color: 'blue', finish: 1, board: 1 }, 3: { color: 'green', finish: 2, board: 2 } }); expect(result.total).toBe(60); expect(result.pegs[0]).toEqual({ player_id: 1, color: 'red', finish: 4, board: 0, home: 0 }); expect(result.pegs[1].home).toBe(2); });
  it('rejects impossible peg counts and duplicate colors', () => { expect(() => troubleResult(players, 1, { 2: { color: 'blue', finish: 3, board: 2 } })).toThrow(/Sam/); expect(() => troubleResult(players, 1, { 1: { color: 'red', finish: 0, board: 0 }, 2: { color: 'red', finish: 0, board: 0 } })).toThrow(/different/); });
  it('requires explicit Kings penalties, preserving zero versus blank', () => { expect(() => kingsResult(players, 1, { 2: '', 3: '10' })).toThrow(/Sam/); expect(kingsResult(players, 1, { 2: '0', 3: '10' })).toEqual({ penalties: [{ player_id: 2, score: 0 }, { player_id: 3, score: 10 }], total: 10 }); });
  it('rejects nonparticipant winners and negative penalties', () => { expect(() => kingsResult(players, 999, {})).toThrow(/winner/); expect(() => kingsResult(players, 1, { 2: '-3', 3: '0' })).toThrow(/Sam/); });
});

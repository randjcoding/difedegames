import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Game } from '../src/types';
const base = (slug = 'five-crowns', mode = 'rounds'): Game => ({ id: 1, slug, title: slug === 'trouble' ? 'Trouble' : 'Five Crowns', status: 'active', revision: 1, mode, scoring_direction: 'low', target_score: null, group_id: 1, viewer_group_ids: [1], players: [{ id: 1, name: 'Alexandra' }, { id: 2, name: 'Sammy' }], rounds: [{ number: 3, label: '3' }], scores: [], standings: [{ player_id: 1, name: 'Alexandra', total: 0, rank: 1, winner: false }, { player_id: 2, name: 'Sammy', total: 0, rank: 1, winner: false }], permissions: { can_score: true, can_manage: true }, created_at: '2026-10-31T00:00:00Z', imported: false });
async function mock(page: Page, initial = base()) {
  const state = { game: initial, scoreError: false, getError: false, finish: [] as Record<string, unknown>[], scores: [] as Record<string, unknown>[] };
  await page.addInitScript(() => { class Events { listeners: Record<string, (e: { data: string }) => void> = {}; constructor() { Object.assign(window, { live: this }); } addEventListener(name: string, cb: (e: { data: string }) => void) { this.listeners[name] = cb; } close() {} } Object.assign(window, { EventSource: Events }); });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/me') return route.fulfill({ json: { user: { id: 1, name: 'Alexandra', email: 'pilot@example.test', person_id: 1 }, groups: [{ id: 1, name: 'Family', role: 'admin' }], csrf_token: 'test' } });
    if (path === '/api/catalog') return route.fulfill({ json: { games: [{ slug: initial.slug, name: initial.title, description: 'Pilot game', min_players: 2, max_players: 10, modes: [initial.mode], scoring_direction: 'low', fixed_rounds: initial.mode === 'rounds' ? [3] : null, targets: [], default_target: null, allow_negative: false, has_dealer: false, rule_note: 'Manual pilot scoring' }] } });
    if (path === '/api/games/1') return route.fulfill(state.getError ? { status: 503, json: { detail: 'Snapshot unavailable; try again.' } } : { json: state.game });
    if (path.endsWith('/scores')) { const body = route.request().postDataJSON(); state.scores.push(body); if (state.scoreError) return route.fulfill({ status: 409, json: { detail: 'Someone changed this score.' } }); state.game = { ...state.game, revision: state.game.revision + 1, scores: [{ player_id: body.player_id, round_number: body.round_number, value: body.score, version: body.expected_version + 1 }] }; return route.fulfill({ json: state.game }); }
    if (path.endsWith('/finish')) { state.finish.push(route.request().postDataJSON()); state.game = { ...state.game, status: 'completed', revision: state.game.revision + 1 }; return route.fulfill({ json: state.game }); }
    return route.fulfill({ status: 404, json: { detail: 'Unexpected fixture endpoint' } });
  });
  return state;
}
async function snapshot(page: Page, game: Game) { await page.evaluate(g => (window as unknown as { live: { listeners: Record<string, (event: { data: string }) => void> } }).live.listeners.snapshot({ data: JSON.stringify(g) }), game); }
test('completion freezes the reviewed revision and demands review when live scores change', async ({ page }) => {
  const state = await mock(page); await page.goto('/game/1');
  await page.getByRole('button', { name: 'Finish game', exact: true }).click();
  await expect(page.getByText('Reviewed result · revision 1')).toBeVisible();
  state.game = { ...state.game, revision: 9 }; await snapshot(page, state.game);
  await expect(page.getByRole('button', { name: 'Finish & show results' })).toBeDisabled();
  await page.getByRole('button', { name: 'Review updated scores' }).click();
  await expect(page.getByText('Reviewed result · revision 9')).toBeVisible();
  await page.getByRole('button', { name: 'Finish & show results' }).click();
  await expect.poll(() => state.finish.length).toBe(1); expect(state.finish[0].expected_revision).toBe(9);
});
test('revoked live access hides private game details and prevents pending draft posts', async ({ page }) => {
  const state = await mock(page); await page.goto('/game/1'); await page.getByRole('textbox', { name: 'Alexandra, round 3' }).fill('25');
  await page.evaluate(() => (window as unknown as { live: { listeners: Record<string, (e: { data: string }) => void> } }).live.listeners.access_revoked({ data: '{}' }));
  await expect(page.getByRole('heading', { name: 'This table is unavailable.' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Five Crowns', exact: true })).toHaveCount(0);
  await page.waitForTimeout(800); expect(state.scores).toHaveLength(0);
  expect(await page.evaluate(() => localStorage.getItem('difede-drafts:1:1'))).toContain('25');
});
test('failed refresh cannot discard a conflicting local draft', async ({ page }) => {
  const state = await mock(page); await page.goto('/game/1'); state.scoreError = true;
  const input = page.getByRole('textbox', { name: 'Alexandra, round 3' }); await input.fill('27'); await input.press('Tab');
  await expect(page.getByRole('button', { name: 'Use saved score' })).toBeVisible(); state.getError = true;
  await page.getByRole('button', { name: 'Use saved score' }).click();
  await expect(page.getByText('Snapshot unavailable; try again.')).toBeVisible(); await expect(input).toHaveValue('27');
  expect(await page.evaluate(() => localStorage.getItem('difede-drafts:1:1'))).toContain('27');
});
test('Trouble specialty draft survives refresh and submits exact reviewed peg metadata', async ({ page }) => {
  const state = await mock(page, base('trouble', 'single')); await page.goto('/game/1');
  await page.getByLabel('Who won?').selectOption('1');
  await page.getByLabel('In Finish').fill('1'); await page.getByLabel('On Board').fill('1');
  await page.reload(); await expect(page.getByLabel('Who won?')).toHaveValue('1'); await expect(page.getByLabel('In Finish')).toHaveValue('1');
  await expect(page.locator('.special-total strong')).toHaveText('50 pts');
  await page.getByRole('button', { name: 'Review & finish' }).click(); await page.getByRole('button', { name: 'Finish & show results' }).click();
  await expect.poll(() => state.finish.length).toBe(1);
  expect(state.finish[0].details).toEqual({ pegs: [{ player_id: 1, color: 'red', finish: 4, board: 0, home: 0 }, { player_id: 2, color: 'blue', finish: 1, board: 1, home: 2 }] });
  expect(await page.evaluate(() => localStorage.getItem('difede-result:1:1:pegs'))).toBeNull();
});

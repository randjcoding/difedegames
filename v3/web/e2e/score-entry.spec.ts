import { expect, test } from '@playwright/test';
const names = ['Alexandra DiFede', 'Sammy DiFede', 'Grandpa Joseph', 'Charlotte Seibert', 'Christopher', 'Alexandra Smith', 'Benjamin'];
const definition = { slug: 'five-crowns', name: 'Five Crowns', description: 'Eleven hands around the table.', scoring_direction: 'low', min_players: 2, max_players: 10, fixed_rounds: [3,4], round_labels: { '3': '3', '4': '4' }, modes: ['rounds'], allow_negative: false, has_dealer: true, targets: [], default_target: null, rule_note: 'Manual low totals; ties shared.', image: '' };
test('seven-player phone input keeps focus and drafts through remote snapshots, exposes missing scores', async ({ page }) => {
  let game = { id: 1, slug: 'five-crowns', title: 'Five Crowns', status: 'active', revision: 1, mode: 'rounds', scoring_direction: 'low', target_score: null, group_id: 1, viewer_group_ids: [1], players: names.map((name, i) => ({ id: i + 1, name })), rounds: [{ number: 3, label: '3' }, { number: 4, label: '4' }], scores: [] as { player_id: number; round_number: number; value: number | null; version: number }[], standings: names.map((name, i) => ({ player_id: i + 1, name, total: 0, rank: 1, winner: false })), permissions: { can_score: true, can_manage: true }, created_at: '2026-10-31T00:00:00Z', imported: false };
  await page.addInitScript(() => { class FakeEventSource { static instance: FakeEventSource; listeners: Record<string, (e: unknown) => void> = {}; constructor() { FakeEventSource.instance = this; (window as unknown as { live: FakeEventSource }).live = this; } addEventListener(name: string, fn: (e: unknown) => void) { this.listeners[name] = fn; } close() {} } Object.assign(window, { EventSource: FakeEventSource }); });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname;
    if (path === '/api/me') return route.fulfill({ json: { user: { id: 1, email: 'test@example.test', name: 'Alexandra', person_id: 1 }, groups: [{ id: 1, name: 'Family', role: 'admin' }], csrf_token: 'test' } });
    if (path === '/api/catalog') return route.fulfill({ json: { games: [definition] } });
    if (path.endsWith('/scores')) { const body = route.request().postDataJSON(); game = { ...game, revision: game.revision + 1, scores: [...game.scores.filter(s => !(s.player_id === body.player_id && s.round_number === body.round_number)), { player_id: body.player_id, round_number: body.round_number, value: body.score, version: body.expected_version + 1 }] }; return route.fulfill({ json: game }); }
    if (path.endsWith('/finish')) return route.fulfill({ status: 422, json: { detail: 'Enter the missing scores.', missing: [{ player_id: 2, round_number: 3 }] } });
    if (path === '/api/games/1') return route.fulfill({ json: game });
    return route.fulfill({ status: 404, json: { detail: 'Not part of this test' } });
  });
  await page.goto('/game/1');
  const score = page.getByRole('textbox', { name: 'Alexandra DiFede, round 3', exact: true });
  await score.fill('25');
  await page.evaluate(snapshot => { const live = (window as unknown as { live: { listeners: Record<string, (e: { data: string }) => void> } }).live; live.listeners.snapshot({ data: JSON.stringify(snapshot) }); }, { ...game, revision: 2 });
  await expect(score).toBeFocused(); await expect(score).toHaveValue('25');
  await score.press('Enter'); await expect(page.getByRole('textbox', { name: 'Sammy DiFede, round 3' })).toBeFocused();
  await expect(page.getByText('All entered scores saved')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'test-results/seven-player-phone.png', fullPage: true });
  await page.getByRole('button', { name: 'Finish game', exact: true }).click(); await page.getByRole('button', { name: 'Finish & show results' }).click();
  await page.getByRole('button', { name: /Sammy DiFede · Round 3/ }).click();
  await expect(page.getByRole('textbox', { name: 'Sammy DiFede, round 3' })).toBeFocused();
});

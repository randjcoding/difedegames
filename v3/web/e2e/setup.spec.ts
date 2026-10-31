import { expect, test } from '@playwright/test';
test('guest-only table needs deliberate nonplaying scorekeeper assignment', async ({ page }) => {
  let created: Record<string, unknown> | undefined;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/me') return route.fulfill({ json: { user: { id: 1, name: 'Avery', email: 'synthetic@example.test', person_id: 99 }, groups: [{ id: 1, name: 'Family', role: 'admin' }], csrf_token: 'test' } });
    if (path === '/api/people') return route.fulfill({ json: { people: [{ id: 2, name: 'Guest One', has_account: false, account_id: null, group_ids: [1] }, { id: 3, name: 'Guest Two', has_account: false, account_id: null, group_ids: [1] }] } });
    if (path === '/api/catalog') return route.fulfill({ json: { games: [{ slug: 'trouble', name: 'Trouble', description: 'One result.', scoring_direction: 'high', min_players: 2, max_players: 4, fixed_rounds: null, modes: ['single'], allow_negative: false, has_dealer: false, targets: [], default_target: null, rule_note: 'Custom Strength of Win.' }] } });
    if (path === '/api/games' && route.request().method() === 'POST') { created = route.request().postDataJSON(); return route.fulfill({ status: 422, json: { detail: 'Synthetic request captured.' } }); }
    return route.fulfill({ status: 404, json: { detail: 'Unknown fixture' } });
  });
  await page.goto('/new/trouble'); await page.getByRole('button', { name: /Guest One/ }).click(); await page.getByRole('button', { name: /Guest Two/ }).click();
  await expect(page.getByText('This is a table of guests.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start with 2 players' })).toBeDisabled();
  await page.getByLabel('Nonplaying scorekeeper · optional').selectOption('1');
  await page.getByRole('button', { name: 'Start with 2 players' }).click();
  await expect.poll(() => created).toMatchObject({ scorekeeper_ids: [1], player_ids: [2, 3] });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

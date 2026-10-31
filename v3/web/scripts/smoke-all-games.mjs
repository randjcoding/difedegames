import { chromium, expect } from '@playwright/test';
const base = process.env.PILOT_BASE_URL || 'http://localhost:5173';
if (!['localhost', '127.0.0.1'].includes(new URL(base).hostname)) throw new Error('Synthetic all-game smoke is restricted to localhost.');
const browser = await chromium.launch({ channel: 'chrome' });
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(base); await page.getByLabel('Email', { exact: true }).fill('person1@example.com'); await page.getByLabel('Password', { exact: true }).fill('ExamplePass!123'); await page.getByRole('button', { name: 'Take your seat' }).click(); await page.getByRole('heading', { name: /Welcome back/ }).waitFor();
  const request = (path, method = 'GET', body) => page.evaluate(async ({ path, method, body }) => { const me = await fetch('/api/me').then(r => r.json()); const result = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json', ...(method !== 'GET' ? { 'X-CSRF-Token': me.csrf_token } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) }); const data = await result.json(); if (!result.ok) throw new Error(JSON.stringify(data)); return data; }, { path, method, body });
  const me = await request('/me'), people = (await request('/people')).people, catalog = (await request('/catalog')).games;
  const playerIds = [me.user.person_id, people.find(p => p.id !== me.user.person_id).id];
  let checked = 0;
  for (const def of catalog) for (const mode of def.modes) for (const direction of mode === 'rounds' && ['uno-classic', 'uno-flip', 'dutch-blitz', 'basic-other'].includes(def.slug) ? ['high', 'low'] : [def.scoring_direction]) {
    let game = await request('/games', 'POST', { slug: def.slug, title: def.slug === 'basic-other' ? 'Synthetic custom table' : def.name, mode, player_ids: playerIds, group_id: me.groups[0].id, viewer_group_ids: [me.groups[0].id], scoring_direction: direction, target_score: def.default_target });
    await page.goto(base + '/game/' + game.id);
    if (mode === 'rounds') {
      const first = game.rounds[0];
      const input = page.locator(`#score-${playerIds[0]}-${first.number}`); await input.fill('0'); await input.press('Tab'); await expect(page.getByText('All entered scores saved')).toBeVisible();
      game = await request('/games/' + game.id);
      for (const round of [...game.rounds]) for (const player of game.players) {
        const cell = game.scores.find(s => s.player_id === player.id && s.round_number === round.number);
        if (cell?.value != null) continue;
        game = await request(`/games/${game.id}/scores`, 'POST', { operation_id: crypto.randomUUID(), player_id: player.id, round_number: round.number, score: player.id === playerIds[0] ? 0 : def.allow_negative ? -3 : 3, expected_version: cell?.version || 0 });
      }
      if (!def.fixed_rounds) await expect.poll(async () => (await request('/games/' + game.id)).rounds.length, { timeout: 12000 }).toBeGreaterThan(1);
      await page.reload(); await page.getByRole('button', { name: 'Finish game', exact: true }).click();
    } else {
      await page.getByLabel('Who won?').selectOption(String(playerIds[0]));
      if (def.slug === 'trouble') { await page.getByLabel('In Finish').fill('1'); await page.getByLabel('On Board').fill('1'); }
      if (def.slug === 'kings-corner') await page.locator('.penalty-row input').fill('7');
      await page.getByRole('button', { name: 'Review & finish' }).click();
    }
    if (await page.getByRole('button', { name: 'Review updated scores' }).isVisible()) await page.getByRole('button', { name: 'Review updated scores' }).click();
    await page.getByRole('button', { name: 'Finish & show results' }).click();
    await page.getByRole('heading', { name: 'Well played, everyone.' }).waitFor();
    const result = await request('/games/' + game.id);
    if (result.status !== 'completed' || !result.standings.some(s => s.winner)) throw new Error('Missing final winner for ' + def.slug);
    if (!(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))) throw new Error('Phone overflow: ' + def.slug);
    console.log(`PASS ${def.slug} / ${mode} / ${direction}`); checked++;
  }
  console.log(`PASS ${checked} real local API game/mode/direction workflows, all ten games.`);
} finally { await browser.close(); }

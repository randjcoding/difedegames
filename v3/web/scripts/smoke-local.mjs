import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
const base = process.env.PILOT_BASE_URL || 'http://localhost:5173';
if (!['localhost', '127.0.0.1'].includes(new URL(base).hostname)) throw new Error('This synthetic smoke script only runs against localhost.');
const browser = await chromium.launch({ channel: 'chrome' });
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(base); await page.getByLabel('Email', { exact: true }).fill('person1@example.com'); await page.getByLabel('Password', { exact: true }).fill('ExamplePass!123');
  await page.getByRole('button', { name: 'Take your seat' }).click(); await page.getByRole('heading', { name: /Welcome back/ }).waitFor();
  await mkdir('test-results', { recursive: true }); await page.screenshot({ path: 'test-results/live-home-phone.png', fullPage: true });
  await page.getByRole('navigation').getByRole('link', { name: 'Games' }).click(); await page.getByRole('heading', { name: 'The game collection.' }).waitFor();
  await page.screenshot({ path: 'test-results/live-library-phone.png', fullPage: true });
  await page.goto(base + '/new/trouble');
  const players = page.locator('.player-option'); await players.nth(0).click(); await players.nth(1).click();
  await page.getByRole('button', { name: 'Start with 2 players' }).click(); await page.getByRole('heading', { name: 'Record your result.' }).waitFor();
  await page.getByLabel('Who won?').selectOption({ index: 1 }); await page.getByLabel('In Finish').fill('1'); await page.getByLabel('On Board').fill('1');
  await page.reload(); await page.getByRole('heading', { name: 'Record your result.' }).waitFor();
  if (await page.getByLabel('In Finish').inputValue() !== '1') throw new Error('Specialty draft did not survive refresh.');
  await page.getByRole('button', { name: 'Review & finish' }).click(); await page.getByRole('button', { name: 'Finish & show results' }).click(); await page.getByRole('heading', { name: 'Well played, everyone.' }).waitFor();
  if (!(await page.locator('.standing-row.winner').textContent()).includes('50')) throw new Error('Expected Trouble Strength of Win 50.');
  if (errors.length) throw new Error(errors.join('\n'));
  if (!(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))) throw new Error('Phone overflow.');
  await page.screenshot({ path: 'test-results/live-trouble-result-phone.png', fullPage: true });
  console.log('PASS: real local API login/home/catalog/create Trouble/draft refresh/transactional finish/exact SOW 50, no page errors or horizontal overflow.');
} finally { await browser.close(); }

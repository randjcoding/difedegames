import { chromium, expect } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
const origin = process.env.PILOT_BASE_URL || 'http://localhost:5173';
const directory = resolve(process.env.SCREENSHOT_DIR || '../verification');
await mkdir(directory, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome' });
try {
  for (const [name, width, height] of [['login-mobile', 390, 844], ['desktop', 1440, 1000]]) {
    const context = await browser.newContext({ viewport: { width, height } });
    const page = await context.newPage(); const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin, { waitUntil: 'domcontentloaded' });
    const email = page.getByLabel('Email', { exact: true }), password = page.getByLabel('Password', { exact: true }), button = page.getByRole('button', { name: 'Take your seat' });
    await expect(email).toBeVisible(); await expect(password).toBeVisible(); await expect(button).toBeVisible();
    for (const control of [email, password, button]) { const box = await control.boundingBox(); if (!box || box.y < 0 || box.y + box.height > height) throw new Error(`${name}: authentication control below the fold.`); }
    if (!(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))) throw new Error(`${name}: horizontal overflow.`);
    await page.locator('.login-story img').evaluate(image => image.decode().catch(() => {}));
    if (errors.length) throw new Error(errors.join('\n'));
    await page.screenshot({ path: resolve(directory, name + '.png') });
    console.log(`PASS ${origin} ${width}×${height}: anonymous sign-in controls above fold, no overflow/page errors; ${name}.png`);
    await context.close();
  }
} finally { await browser.close(); }

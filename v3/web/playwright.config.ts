import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  projects: [
    { name: "phone390", use: { viewport: { width: 390, height: 844 } } },
    { name: "phone375", use: { viewport: { width: 375, height: 812 } } },
    { name: "landscape", use: { viewport: { width: 844, height: 390 } } },
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  use: {
    baseURL: "http://127.0.0.1:5173",
    channel: process.env.PLAYWRIGHT_CHANNEL || "chrome",
  },
  webServer: {
    command: "npm run dev -- --port 5173",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
  },
  reporter: "list",
});

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
async function mock(page: Page) {
  const state = {
    saves: [] as Record<string, unknown>[],
    starts: [] as Record<string, unknown>[],
    directory: [] as string[],
    joins: [] as string[],
  };
  const me = {
    user: {
      id: 1,
      name: "Synthetic Organizer",
      email: "synthetic@example.test",
      person_id: 1,
      site_role: "member",
      is_owner: true,
    },
    groups: [{ id: 1, name: "Synthetic Family", role: "admin" }],
    csrf_token: "test",
    preferences: {
      style: "classic",
      palette: "midnight-blue",
      favorite_game_slugs: [],
      favorite_person_ids: [],
    },
  };
  const people = [
    {
      id: 1,
      name: "Synthetic Organizer",
      has_account: true,
      account_id: 1,
      group_ids: [1],
    },
    {
      id: 2,
      name: "Guest One",
      has_account: false,
      account_id: null,
      group_ids: [1],
    },
    {
      id: 3,
      name: "Guest Two",
      has_account: false,
      account_id: null,
      group_ids: [1],
    },
  ];
  const layout = {
    id: 1,
    group_id: 1,
    slug: "gin-rummy",
    name: "Friday Gin",
    player_ids: [2, 3],
    mode: "rounds",
    scoring_direction: "high",
    target_score: 100,
    title: null,
    is_default: true,
    created_by: 1,
  };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname,
      method = route.request().method();
    if (path === "/api/me") return route.fulfill({ json: me });
    if (path === "/api/people") return route.fulfill({ json: { people } });
    if (path === "/api/catalog")
      return route.fulfill({
        json: {
          games: [
            {
              slug: "gin-rummy",
              name: "Gin Rummy",
              description: "Manual high totals.",
              min_players: 2,
              max_players: 8,
              modes: ["rounds"],
              scoring_direction: "high",
              fixed_rounds: null,
              allow_negative: false,
              has_dealer: false,
              targets: [100, 200, 500],
              default_target: 100,
              rule_note: "Manual scores.",
            },
          ],
        },
      });
    if (path === "/api/layouts" && method === "GET")
      return route.fulfill({ json: { layouts: [layout] } });
    if (
      (path === "/api/layouts" && method === "POST") ||
      (path === "/api/layouts/1" && method === "PUT")
    ) {
      state.saves.push(route.request().postDataJSON());
      return route.fulfill({ json: layout });
    }
    if (path === "/api/layouts/1/start") {
      state.starts.push(route.request().postDataJSON());
      return route.fulfill({
        status: 422,
        json: { detail: "Synthetic start captured." },
      });
    }
    if (path === "/api/directory/groups") {
      state.directory.push(url.search);
      return route.fulfill({
        json: {
          groups: [
            {
              id: 2,
              name: "Discoverable Group",
              show_roster: false,
              member_count: null,
              membership_status: null,
            },
          ],
        },
      });
    }
    if (path === "/api/groups/2/request-join") {
      state.joins.push(path);
      return route.fulfill({ json: { status: "requested" } });
    }
    return route.fulfill({
      status: 404,
      json: { detail: "Unexpected fixture path " + path },
    });
  });
  return state;
}
test("saved setup edits keep sharing out of storage and start asks for scorekeeper explicitly", async ({
  page,
}) => {
  const state = await mock(page);
  await page.goto("/new/gin-rummy?setup=1&group=1");
  await expect(page.getByRole("button", { name: /Guest One/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page
    .locator("summary")
    .filter({ hasText: "Update saved setup" })
    .click();
  await page.getByLabel("Setup name", { exact: true }).fill("A new Friday Gin");
  await page
    .getByRole("button", { name: "Update saved setup", exact: true })
    .click();
  await expect.poll(() => state.saves.length).toBe(1);
  expect(state.saves[0]).toMatchObject({
    group_id: 1,
    slug: "gin-rummy",
    player_ids: [2, 3],
    name: "A new Friday Gin",
  });
  expect(state.saves[0]).not.toHaveProperty("viewer_group_ids");
  expect(state.saves[0]).not.toHaveProperty("scorekeeper_ids");
  await page.goto("/setups");
  await page.getByRole("button", { name: "Start game", exact: true }).click();
  await page.getByLabel("Nonplaying scorekeeper · optional").selectOption("1");
  await page.getByRole("button", { name: "Start this game" }).click();
  await expect
    .poll(() => state.starts[0])
    .toEqual({ viewer_group_ids: [], scorekeeper_ids: [1] });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
});
test("directory searches only after minimum name input and join request does not grant membership", async ({
  page,
}) => {
  const state = await mock(page);
  await page.goto("/directory");
  await page.getByLabel("Name · at least two characters").fill("A");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  expect(state.directory).toHaveLength(0);
  await expect(page.getByRole("alert")).toContainText("at least two");
  await page.getByLabel("Name · at least two characters").fill("Discover");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(page.getByText("Roster kept private")).toBeVisible();
  await page
    .getByRole("button", { name: "Request to join", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Request pending", exact: true }),
  ).toBeDisabled();
  expect(state.joins).toHaveLength(1);
  expect(state.directory[0]).toContain("q=Discover");
});

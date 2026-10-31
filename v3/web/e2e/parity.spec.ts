import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Game, Me } from "../src/types";
const styles = ["Classic", "Arcade", "Scoreboard", "Clubhouse", "Playground"];
const palettes = [
  "DiFede Dark",
  "DiFede Light",
  "Midnight Blue",
  "Espresso",
  "Emerald Forest",
  "Rose Wine",
  "Sunset Blaze",
  "Arctic Frost",
  "Royal Purple",
];
const slug = (name: string) =>
  name
    .toLowerCase()
    .replaceAll(" ", "-")
    .replace("difede-light", "classic-light");
async function fixture(page: Page) {
  const me: Me = {
    user: {
      id: 1,
      name: "Account Legal Name",
      display_name: "Preferred Name",
      email: "synthetic@example.test",
      person_id: 1,
      site_role: "super_admin",
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
  const game: Game = {
    id: 1,
    title: "Five Crowns",
    slug: "five-crowns",
    status: "active",
    revision: 1,
    mode: "rounds",
    scoring_direction: "low",
    target_score: null,
    group_id: 1,
    viewer_group_ids: [1],
    players: [
      { id: 1, name: "Preferred Name" },
      { id: 2, name: "Another Player" },
    ],
    rounds: Array.from({ length: 11 }, (_, i) => ({
      number: i + 3,
      label: i + 3 > 10 ? ["J", "Q", "K"][i - 8] : String(i + 3),
    })),
    scores: [],
    standings: [
      {
        player_id: 1,
        name: "Preferred Name",
        total: 0,
        rank: 1,
        winner: false,
      },
      {
        player_id: 2,
        name: "Another Player",
        total: 0,
        rank: 1,
        winner: false,
      },
    ],
    permissions: { can_manage: true, can_score: true },
    created_at: "2026-10-31T00:00:00Z",
    imported: false,
  };
  const state = {
    me,
    game,
    prefs: [] as Record<string, unknown>[],
    profile: [] as Record<string, unknown>[],
    auditQueries: [] as string[],
    historyQueries: [] as string[],
    adminCalls: 0,
  };
  const profile = {
    user: me.user,
    person: {
      id: 1,
      name: "Preferred Name",
      first_name: "First",
      last_name: "Last",
      email: "person@example.test",
      is_discoverable: false,
      show_full_last_name: false,
      is_minor: false,
    },
    contact: {
      address: "",
      city: "",
      state: "",
      zipcode: "",
      phone_number: "",
    },
    groups: me.groups,
  };
  const definition = {
    slug: "five-crowns",
    name: "Five Crowns",
    description: "Eleven fixed hands.",
    scoring_direction: "low",
    fixed_rounds: game.rounds.map((r) => r.number),
    round_labels: {},
    modes: ["rounds"],
    min_players: 2,
    max_players: 7,
    allow_negative: false,
    has_dealer: true,
    targets: [],
    default_target: null,
    rule_note: "Manual low scores.",
  };
  const category = {
    id: "synthetic-five-crowns-low",
    slug: "five-crowns",
    name: "Five Crowns",
    title: null,
    mode: "rounds",
    scoring_direction: "low",
    score_metric: "round_total",
    label: "Five Crowns · lowest total",
  };
  const summary = {
    games: 1,
    known_results: 1,
    unknown_results: 0,
    players: 2,
    player_appearances: 2,
    solo_wins: 1,
    shared_wins: 0,
  };
  const completed = {
    ...game,
    id: 2,
    status: "completed",
    finished_at: "2026-10-31T01:00:00Z",
    scores: game.rounds.flatMap((r) =>
      game.players.map((p) => ({
        player_id: p.id,
        round_number: r.number,
        value: p.id === 1 ? 3 : 6,
        version: 1,
      })),
    ),
    standings: [
      {
        player_id: 1,
        name: "Preferred Name",
        total: 33,
        rank: 1,
        winner: true,
      },
      {
        player_id: 2,
        name: "Another Player",
        total: 66,
        rank: 2,
        winner: false,
      },
    ],
  };
  const brief = {
    id: 2,
    title: "Five Crowns",
    slug: "five-crowns",
    mode: "rounds",
    scoring_direction: "low",
    group_id: 1,
    category_id: category.id,
    finished_at: completed.finished_at,
    created_at: game.created_at,
    player_count: 2,
    result_known: true,
    winners: [
      {
        player_id: 1,
        name: "Preferred Name",
        winning_score: 33,
        is_tie: false,
      },
    ],
  };
  const leader = {
    person_id: 1,
    name: "Preferred Name",
    played: 1,
    known_results: 1,
    unknown_results: 0,
    solo_wins: 1,
    shared_wins: 0,
    wins: 1,
    losses: 0,
    ties: 0,
    win_percentage: 100,
    win_percentage_denominator: 1,
    winning_scores: [
      { category_id: category.id, scored_wins: 1, average: 33, best: 33 },
    ],
  };
  await page.addInitScript(() => {
    class Events {
      listeners: Record<string, unknown> = {};
      addEventListener(n: string, f: unknown) {
        this.listeners[n] = f;
      }
      close() {}
    }
    Object.assign(window, { EventSource: Events });
  });
  await page.route("**/api/**", async (route) => {
    const u = new URL(route.request().url()),
      p = u.pathname,
      m = route.request().method();
    if (p === "/api/me") return route.fulfill({ json: state.me });
    if (p === "/api/dashboard")
      return route.fulfill({
        json: {
          active: [state.game],
          recent: [completed],
          stats: { games: 1, wins: 1 },
          people: state.game.players,
          groups: me.groups,
        },
      });
    if (p === "/api/statistics" || p === "/api/leaderboards")
      return route.fulfill({
        json: {
          scope: "visible",
          filters: {},
          summary,
          categories: [
            { category, games: 1, known_results: 1, unknown_results: 0 },
          ],
          players: [leader],
          history: [brief],
          pagination: { page: 1, page_size: 20, total: 1, pages: 1 },
          labels: { scope: "Synthetic visible games" },
        },
      });
    if (p === "/api/hall-of-fame")
      return route.fulfill({
        json: {
          latest_game: brief,
          biggest_beatdowns: [
            { category, game: brief, value: 33, low_total: 33, high_total: 66 },
          ],
          worst_five_crowns_hands: [
            {
              category,
              game: brief,
              person_id: 2,
              name: "Another Player",
              value: 6,
              round_number: 3,
            },
          ],
          win_leaders: [{ category, leaders: [leader] }],
          best_scores: [],
          worst_scores: [],
          trouble_strength: [],
          labels: {},
        },
      });
    if (p === "/api/games/2") return route.fulfill({ json: completed });
    if (p === "/api/auth/logout") return route.fulfill({ json: { ok: true } });
    if (p === "/api/preferences") {
      const body = route.request().postDataJSON();
      state.prefs.push(body);
      state.me.preferences = { ...state.me.preferences!, ...body };
      return route.fulfill({ json: { preferences: state.me.preferences } });
    }
    if (p === "/api/profile" && m === "PATCH") {
      const body = route.request().postDataJSON();
      state.profile.push(body);
      Object.assign(profile.person, { ...body, email: body.person_email });
      state.me.user.display_name = profile.person.name || state.me.user.name;
      return route.fulfill({ json: profile });
    }
    if (p === "/api/profile") return route.fulfill({ json: profile });
    if (p === "/api/people")
      return route.fulfill({
        json: {
          people: state.game.players.map((p) => ({
            ...p,
            has_account: true,
            account_id: p.id,
            group_ids: [1],
          })),
        },
      });
    if (p === "/api/catalog")
      return route.fulfill({
        json: {
          games: [
            definition,
            {
              ...definition,
              slug: "gin-rummy",
              name: "Gin Rummy",
              description: "Another game.",
            },
          ],
        },
      });
    if (p === "/api/games/1") return route.fulfill({ json: state.game });
    if (p === "/api/games") {
      state.historyQueries.push(u.search);
      return route.fulfill({ json: { games: [state.game], total: 1 } });
    }
    if (p === "/api/games/1/scores") {
      const b = route.request().postDataJSON();
      state.game = {
        ...state.game,
        revision: state.game.revision + 1,
        scores: [
          {
            player_id: b.player_id,
            round_number: b.round_number,
            value: b.score,
            version: b.expected_version + 1,
          },
        ],
      };
      return route.fulfill({ json: state.game });
    }
    if (p === "/api/admin/audit") {
      state.auditQueries.push(u.search);
      if (
        u.searchParams.get("game_id") === "" ||
        u.searchParams.get("actor_id") === ""
      )
        return route.fulfill({
          status: 422,
          json: { detail: "Empty integer filter" },
        });
      return route.fulfill({ json: { events: [], total: 0 } });
    }
    if (p === "/api/admin/overview") {
      state.adminCalls++;
      return route.fulfill({
        json: {
          counts: { people: 2, accounts: 2, groups: 1, games: 1 },
          pending_accounts: 0,
        },
      });
    }
    return route.fulfill({
      status: 404,
      json: { detail: "Unexpected test endpoint " + p },
    });
  });
  return state;
}
test("five designs share scoring and independently persist all nine palettes", async ({
  page,
}, info) => {
  const state = await fixture(page);
  await page.goto("/game/1");
  const input = page.getByRole("textbox", { name: "Preferred Name, round 3" });
  await input.fill("27");
  for (const name of styles) {
    await page
      .getByRole("button", { name: "Appearance: design and colors" })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog
      .getByRole("button", { name: new RegExp("^" + name + " ") })
      .click();
    await dialog
      .getByRole("button", { name: "Save appearance", exact: true })
      .click();
    await expect(page.locator("html")).toHaveAttribute(
      "data-style",
      slug(name),
    );
    await expect(input).toHaveValue("27");
    await expect(
      page
        .getByRole("navigation", { name: "Main navigation" })
        .getByRole("link", { name: "Stats", exact: false }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
  }
  for (const name of palettes) {
    await page
      .getByRole("button", { name: "Appearance: design and colors" })
      .click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name, exact: true })
      .click();
    await page
      .getByRole("button", { name: "Save appearance", exact: true })
      .click();
    await expect(page.locator("html")).toHaveAttribute(
      "data-palette",
      slug(name),
    );
  }
  expect(state.prefs).toHaveLength(14);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute(
    "data-style",
    "playground",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-palette",
    "royal-purple",
  );
  await page.screenshot({
    path: `test-results/appearance-${info.project.name}.png`,
  });
});
test("shared preview survives account boot and cancel restores appearance without saving", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/game/1?style=arcade&palette=royal-purple");
  await expect(
    page.getByRole("textbox", { name: "Preferred Name, round 3" }),
  ).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-style", "arcade");
  await page
    .getByRole("button", { name: "Appearance: design and colors" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /^Clubhouse / })
    .click();
  await page.getByRole("button", { name: "Cancel preview" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-style", "arcade");
  expect(state.prefs).toHaveLength(0);
});
test("compact eleven-round sheet fits two and three players; seven only scrolls horizontally", async ({
  page,
}) => {
  const state = await fixture(page);
  for (const count of [2, 3, 7]) {
    state.game.players = Array.from({ length: count }, (_, i) => ({
      id: i + 1,
      name: "Player " + (i + 1),
    }));
    await page.goto("/game/1?view=overview");
    await expect(
      page.getByRole("heading", { name: "Full score sheet" }),
    ).toBeVisible();
    const metrics = await page.locator(".table-scroll").evaluate((el) => ({
      height: el.clientHeight,
      scrollHeight: el.scrollHeight,
      width: el.clientWidth,
      scrollWidth: el.scrollWidth,
    }));
    expect(metrics.scrollHeight).toBeLessThanOrEqual(metrics.height + 1);
    expect(metrics.height).toBeLessThan(470);
    if (count <= 3)
      expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.width + 1);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
  }
});
test("profile edits preferred person name and clears contact email with null", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/account");
  await expect(page.getByLabel("Display name")).toHaveValue("Preferred Name");
  await page.getByLabel("Display name").fill("New preferred name");
  await page.getByLabel("Person contact email").fill("");
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await expect
    .poll(() => state.profile[0])
    .toMatchObject({ name: "New preferred name", person_email: null });
  expect(state.me.user.name).toBe("Account Legal Name");
  await expect(page.getByText("Your profile has been saved.")).toBeVisible();
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Welcome back, New." }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Your account", exact: true }),
  ).toHaveText("N");
});
test("audit omits blank integer filters and member routes do not expose admin tools", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/admin?tab=audit");
  await expect(
    page.getByText("No audit events match this view."),
  ).toBeVisible();
  expect(state.auditQueries[0]).not.toContain("game_id=");
  expect(state.auditQueries[0]).not.toContain("actor_id=");
  state.me.user.site_role = "member";
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Administrator access required." }),
  ).toBeVisible();
  expect(state.adminCalls).toBe(0);
});
test("favorites stay account-backed and search filters before ordering", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/games");
  await page
    .getByRole("button", { name: "Favorite Five Crowns", exact: true })
    .click();
  await expect
    .poll(() => state.me.preferences?.favorite_game_slugs)
    .toEqual(["five-crowns"]);
  await page.getByRole("searchbox", { name: "Find a game" }).fill("Gin");
  await expect(
    page.getByRole("heading", { name: "Gin Rummy", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Five Crowns", exact: true }),
  ).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole("button", {
      name: "Remove favorite Five Crowns",
      exact: true,
    }),
  ).toHaveAttribute("aria-pressed", "true");
});
test("all 45 design and palette combinations retain shared features and phone fit", async ({
  page,
}, info) => {
  test.setTimeout(90000);
  await fixture(page);
  await page.goto("/game/1?view=overview");
  for (const design of styles)
    for (const palette of palettes) {
      await page
        .getByRole("button", { name: "Appearance: design and colors" })
        .click();
      const dialog = page.getByRole("dialog");
      await dialog
        .getByRole("button", { name: new RegExp("^" + design + " ") })
        .click();
      await dialog.getByRole("button", { name: palette, exact: true }).click();
      await dialog
        .getByRole("button", { name: "Save appearance", exact: true })
        .click();
      await expect(page.locator("html")).toHaveAttribute(
        "data-style",
        slug(design),
      );
      await expect(page.locator("html")).toHaveAttribute(
        "data-palette",
        slug(palette),
      );
      await expect(
        page.getByRole("heading", { name: "Full score sheet" }),
      ).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBeTruthy();
      const metrics = await page.locator(".table-scroll").evaluate((el) => ({
        height: el.clientHeight,
        scroll: el.scrollHeight,
      }));
      expect(metrics.scroll).toBeLessThanOrEqual(metrics.height + 1);
      if (palette === "Midnight Blue")
        await page.screenshot({
          path: `test-results/styles/${slug(design)}-${info.project.name}.png`,
        });
    }
});
test("cross-tab appearance preserves input focus and cannot apply another account cache", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/game/1");
  const input = page.getByRole("textbox", { name: "Preferred Name, round 3" });
  await input.fill("31");
  await page.evaluate(() =>
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: "difede-appearance-v1:account:99",
        newValue: JSON.stringify({ style: "playground", palette: "rose-wine" }),
      }),
    ),
  );
  await expect(page.locator("html")).toHaveAttribute("data-style", "classic");
  await page.evaluate(() =>
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: "difede-appearance-v1:account:1",
        newValue: JSON.stringify({
          style: "scoreboard",
          palette: "sunset-blaze",
        }),
      }),
    ),
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-style",
    "scoreboard",
  );
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("31");
  const trigger = page.getByRole("button", {
    name: "Appearance: design and colors",
  });
  await trigger.focus();
  await trigger.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
});
test("saving appearance locks dismissal and logout restores anonymous defaults", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/game/1");
  let release!: () => void;
  const gate = new Promise<void>((r) => {
    release = r;
  });
  await page.route("**/api/preferences", async (route) => {
    const body = route.request().postDataJSON();
    state.me.preferences = { ...state.me.preferences!, ...body };
    await gate;
    await route.fulfill({ json: { preferences: state.me.preferences } });
  });
  await page
    .getByRole("button", { name: "Appearance: design and colors" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /^Arcade / })
    .click();
  await page
    .getByRole("button", { name: "Save appearance", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Close dialog" }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeVisible();
  release();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.goto("/account");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Take your seat" }),
  ).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-style", "classic");
  await expect(page.locator("html")).toHaveAttribute(
    "data-palette",
    "midnight-blue",
  );
});
test("home shows stats before live games and visible records before one compact latest sheet", async ({
  page,
}, info) => {
  await fixture(page);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Hall of Fame", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Hall of Shame", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Full score sheet", exact: true }),
  ).toBeVisible();
  const stats = await page.locator(".stats-strip").boundingBox(),
    live = await page.locator(".live-tables").boundingBox();
  expect(stats!.y).toBeLessThan(live!.y);
  expect(await page.locator(".latest-results .standing-row").count()).toBe(0);
  await expect(
    page.getByRole("link", { name: "Administration" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: `test-results/styles/home-classic-${info.project.name}.png`,
    fullPage: true,
  });
});

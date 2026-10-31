# Shared design verification

These screenshots were captured from deterministic synthetic Playwright fixtures, not from private production accounts. Names and game results are test data.

Each of Classic, Arcade, Scoreboard, Clubhouse and Playground is shown in the same Midnight Blue palette at 390×844 (`*-phone390.png`) and 1440×1000 (`*-desktop.png`). This holds color constant so typography, navigation, framing and density differences are visible. The two `home-classic-*` files show the shared home hierarchy: statistics, active games, visible Fame/Shame records, compact latest score sheet and recent games.

The test suite also exercised all 45 design/palette combinations at 375×812, 390×844, 844×390 and 1440×1000. Coverage includes whole-page fit, compact sheets, preferences, focus, account separation and the shared gameplay/authentication checks. Automated Chromium testing does not replace physical iPhone Safari validation.

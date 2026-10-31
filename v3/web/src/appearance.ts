import { useSyncExternalStore } from "react";
export const STYLES = [
  {
    id: "classic",
    name: "Classic",
    description:
      "The familiar game-night hub. Clear cards, lively color, everything within reach.",
  },
  {
    id: "arcade",
    name: "Arcade",
    description:
      "Big personality, bold type and playful cabinet-style controls.",
  },
  {
    id: "scoreboard",
    name: "Scoreboard",
    description: "Fast, focused and compact. Built around the scores.",
  },
  {
    id: "clubhouse",
    name: "Clubhouse",
    description:
      "An editorial table with elegant type and a little breathing room.",
  },
  {
    id: "playground",
    name: "Playground",
    description: "Friendly rounded shapes and easy-going family energy.",
  },
] as const;
export const PALETTES = [
  {
    id: "difede-dark",
    name: "DiFede Dark",
    dark: true,
    background: "#0f0f1a",
    surface: "#1a1a2e",
    text: "#eeeef7",
    muted: "#b4b4cc",
    accent: "#117b75",
    link: "#5eead4",
    tint: "#242442",
    line: "#41415e",
    gold: "#e5bd79",
  },
  {
    id: "classic-light",
    name: "DiFede Light",
    dark: false,
    background: "#f4f6f9",
    surface: "#ffffff",
    text: "#24384c",
    muted: "#5f6d7a",
    accent: "#226b9c",
    link: "#226b9c",
    tint: "#e6eef6",
    line: "#c4cfdb",
    gold: "#866323",
  },
  {
    id: "midnight-blue",
    name: "Midnight Blue",
    dark: true,
    background: "#0a0e27",
    surface: "#171d3b",
    text: "#eff0ff",
    muted: "#b3bce1",
    accent: "#5c55be",
    link: "#bbb7ff",
    tint: "#242b53",
    line: "#414a7a",
    gold: "#ebca79",
  },
  {
    id: "espresso",
    name: "Espresso",
    dark: true,
    background: "#1a1210",
    surface: "#2a1f1b",
    text: "#f5e9dd",
    muted: "#cdb49e",
    accent: "#89512d",
    link: "#efbb8d",
    tint: "#3e2d24",
    line: "#634d3d",
    gold: "#e6be73",
  },
  {
    id: "emerald-forest",
    name: "Emerald Forest",
    dark: true,
    background: "#0a1a14",
    surface: "#142b20",
    text: "#e6f4e9",
    muted: "#b0cbb9",
    accent: "#176e48",
    link: "#73e2ad",
    tint: "#224131",
    line: "#426750",
    gold: "#dbce85",
  },
  {
    id: "rose-wine",
    name: "Rose Wine",
    dark: true,
    background: "#1a0f14",
    surface: "#301c28",
    text: "#f9e8f1",
    muted: "#d5b1c6",
    accent: "#9b3e70",
    link: "#f4a4d0",
    tint: "#472c40",
    line: "#775369",
    gold: "#e6b48d",
  },
  {
    id: "sunset-blaze",
    name: "Sunset Blaze",
    dark: false,
    background: "#f8f4ef",
    surface: "#fffdf9",
    text: "#462c24",
    muted: "#795e52",
    accent: "#a94324",
    link: "#a94324",
    tint: "#f6e4d6",
    line: "#d8c2b2",
    gold: "#8c611a",
  },
  {
    id: "arctic-frost",
    name: "Arctic Frost",
    dark: false,
    background: "#e8eef5",
    surface: "#f9fcff",
    text: "#23394d",
    muted: "#546c80",
    accent: "#08688e",
    link: "#08688e",
    tint: "#d7e8f4",
    line: "#adc6da",
    gold: "#80601e",
  },
  {
    id: "royal-purple",
    name: "Royal Purple",
    dark: true,
    background: "#12091e",
    surface: "#251438",
    text: "#f2e9fc",
    muted: "#c6b0dc",
    accent: "#7d38ad",
    link: "#dbacff",
    tint: "#3a2152",
    line: "#68417e",
    gold: "#e8c579",
  },
] as const;
export type StyleId = (typeof STYLES)[number]["id"];
export type PaletteId = (typeof PALETTES)[number]["id"];
export type Appearance = { style: StyleId; palette: PaletteId };
export const defaultAppearance: Appearance = {
  style: "classic",
  palette: "midnight-blue",
};
export const normalizeAppearance = (
  value: Partial<Appearance> | null | undefined,
): Appearance => ({
  style: STYLES.some((s) => s.id === value?.style)
    ? value!.style!
    : defaultAppearance.style,
  palette: PALETTES.some((p) => p.id === value?.palette)
    ? value!.palette!
    : defaultAppearance.palette,
});
const key = "difede-appearance-v1";
let activeKey = key;
const listeners = new Set<() => void>();
let current: Appearance = defaultAppearance;
let initialized = false;
let sharedPreview: Partial<Appearance> | null = null;
function paint(value: Appearance) {
  const root = document.documentElement,
    p = PALETTES.find((p) => p.id === value.palette)!;
  root.dataset.style = value.style;
  root.dataset.palette = value.palette;
  root.dataset.theme = p.dark ? "dark" : "light";
  root.style.colorScheme = p.dark ? "dark" : "light";
  const tokens = {
    "--paper": p.background,
    "--surface": p.surface,
    "--ink": p.text,
    "--muted": p.muted,
    "--line": p.line,
    "--accent": p.accent,
    "--accent-hover": p.accent,
    "--teal": p.accent,
    "--teal-deep": p.surface,
    "--link": p.link,
    "--tint": p.tint,
    "--gold": p.gold,
    "--white": "#ffffff",
    "--on-accent": "#ffffff",
    "--error": p.dark ? "#ffb4a5" : "#9d2d21",
    "--error-bg": p.dark ? "#44242d" : "#fff0ec",
  };
  for (const [name, value] of Object.entries(tokens))
    root.style.setProperty(name, value);
}
export function setAppearance(value: Partial<Appearance>, persist = true) {
  current = normalizeAppearance({ ...current, ...value });
  paint(current);
  if (persist)
    try {
      localStorage.setItem(activeKey, JSON.stringify(current));
    } catch {
      /* In-memory selection remains usable. */
    }
  listeners.forEach((fn) => fn());
}
export function selectAppearanceAccount(
  id?: number,
  preferences?: { style: string; palette: string },
) {
  const nextKey = id ? `${key}:account:${id}` : key;
  let saved: Partial<Appearance> | null = null;
  try {
    saved = JSON.parse(localStorage.getItem(nextKey) || "null");
  } catch {
    /* Use defaults if the boot cache is unavailable. */
  }
  activeKey = nextKey;
  const account = normalizeAppearance(
    preferences ? (preferences as Appearance) : saved,
  );
  setAppearance(
    sharedPreview
      ? normalizeAppearance({ ...account, ...sharedPreview })
      : account,
    !sharedPreview,
  );
}
export function commitAppearance(value: Appearance) {
  sharedPreview = null;
  setAppearance(value, true);
  const url = new URL(location.href);
  url.searchParams.delete("style");
  url.searchParams.delete("palette");
  history.replaceState(history.state, "", url.pathname + url.search + url.hash);
}
export function initializeAppearance() {
  if (initialized) return;
  initialized = true;
  try {
    current = normalizeAppearance(
      JSON.parse(localStorage.getItem(key) || "null"),
    );
  } catch {
    current = defaultAppearance;
  }
  const query = new URLSearchParams(location.search);
  if (query.has("style") || query.has("palette")) {
    sharedPreview = {
      style: (query.get("style") || current.style) as StyleId,
      palette: (query.get("palette") || current.palette) as PaletteId,
    };
    current = normalizeAppearance(sharedPreview);
  }
  paint(current);
  window.addEventListener("storage", (event) => {
    if (event.key === activeKey) {
      try {
        setAppearance(
          normalizeAppearance(JSON.parse(event.newValue || "null")),
          false,
        );
      } catch {
        /* Ignore malformed cross-tab state. */
      }
    }
  });
}
export function useAppearance() {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    () => current,
  );
}

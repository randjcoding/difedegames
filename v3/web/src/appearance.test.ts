import { describe, expect, it } from "vitest";
import {
  defaultAppearance,
  normalizeAppearance,
  PALETTES,
  STYLES,
} from "./appearance";
const luminance = (hex: string) => {
  const rgb = hex
    .slice(1)
    .match(/../g)!
    .map((v) => parseInt(v, 16) / 255)
    .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
};
const contrast = (a: string, b: string) => {
  const x = luminance(a),
    y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};
describe("shared appearance registry", () => {
  it("has five unique designs and nine independent palettes with the agreed default", () => {
    expect(STYLES).toHaveLength(5);
    expect(PALETTES).toHaveLength(9);
    expect(new Set(STYLES.map((s) => s.id)).size).toBe(5);
    expect(defaultAppearance).toEqual({
      style: "classic",
      palette: "midnight-blue",
    });
    expect(
      normalizeAppearance({
        style: "unknown" as never,
        palette: "invalid" as never,
      }),
    ).toEqual(defaultAppearance);
  });
  for (const p of PALETTES)
    it(`${p.name} keeps semantic body/link/action colors readable`, () => {
      for (const [a, b] of [
        [p.text, p.surface],
        [p.muted, p.surface],
        [p.link, p.surface],
        [p.text, p.tint],
        ["#ffffff", p.accent],
      ])
        expect(contrast(a, b), `${p.id}: ${a} on ${b}`).toBeGreaterThanOrEqual(
          4.5,
        );
    });
});

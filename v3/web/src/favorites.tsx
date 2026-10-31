import { useState } from "react";
import { api } from "./api";
import { useAuth } from "./core";
export function useFavorites() {
  const { me, refresh } = useAuth(),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>();
  const games = me.preferences?.favorite_game_slugs || [],
    people = me.preferences?.favorite_person_ids || [];
  async function update(body: unknown) {
    setBusy(true);
    setError(undefined);
    try {
      await api("/preferences", "PATCH", body);
      await refresh();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return {
    games,
    people,
    busy,
    error,
    toggleGame: (slug: string) =>
      update({
        favorite_game_slugs: games.includes(slug)
          ? games.filter((s) => s !== slug)
          : [...games, slug],
      }),
    togglePerson: (id: number) =>
      update({
        favorite_person_ids: people.includes(id)
          ? people.filter((p) => p !== id)
          : [...people, id],
      }),
  };
}
export function FavoriteButton({
  active,
  disabled,
  label,
  onClick,
}: {
  active: boolean;
  disabled: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="favorite-button"
      aria-label={`${active ? "Remove favorite" : "Favorite"} ${label}`}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path
          d="m12 3 2.8 5.7 6.3.9-4.5 4.4 1.1 6.2L12 17.3l-5.7 3 1.1-6.3L3 9.6l6.2-.9Z"
          fill={active ? "currentColor" : "none"}
          stroke="currentColor"
          strokeWidth="1.5"
        />
      </svg>
      <span>{active ? "Favorite" : "Save favorite"}</span>
    </button>
  );
}

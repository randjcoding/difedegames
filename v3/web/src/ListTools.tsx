import type { ReactNode } from "react";
export function ListFilters({
  query,
  setQuery,
  onSearch,
  onReset,
  children,
  label = "Search",
}: {
  query: string;
  setQuery: (value: string) => void;
  onSearch: () => void;
  onReset: () => void;
  children?: ReactNode;
  label?: string;
}) {
  return (
    <form
      className="list-filters"
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        onSearch();
      }}
    >
      <label>
        {label}
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search…"
        />
      </label>
      {children}
      <div className="filter-actions">
        <button className="button primary" type="submit">
          Apply
        </button>
        <button className="button secondary" type="button" onClick={onReset}>
          Reset
        </button>
      </div>
    </form>
  );
}
export function Pagination({
  total,
  offset,
  limit = 25,
  onPage,
}: {
  total: number;
  offset: number;
  limit?: number;
  onPage: (offset: number) => void;
}) {
  return (
    <nav className="pagination" aria-label="List pages">
      <button
        className="button secondary"
        disabled={offset === 0}
        onClick={() => onPage(Math.max(0, offset - limit))}
      >
        ← Previous
      </button>
      <span aria-live="polite">
        {total
          ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`
          : "0 results"}
      </span>
      <button
        className="button secondary"
        disabled={offset + limit >= total}
        onClick={() => onPage(offset + limit)}
      >
        Next →
      </button>
    </nav>
  );
}

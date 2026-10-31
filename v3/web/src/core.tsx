import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { api } from "./api";
import type { Me } from "./types";
export const Auth = createContext<{ me: Me; refresh: () => Promise<void> }>(
  {} as never,
);
export const useAuth = () => useContext(Auth);
export function ErrorNote({ error }: { error: unknown }) {
  return error ? (
    <div role="alert" className="notice error">
      {error instanceof Error ? error.message : String(error)}
    </div>
  ) : null;
}
export function useLoad<T>(path: string | null) {
  const [data, setData] = useState<T>(),
    [error, setError] = useState<unknown>(),
    [serial, setSerial] = useState(0);
  useEffect(() => {
    let alive = true;
    setError(undefined);
    setData(undefined);
    if (!path) return;
    api<T>(path)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch((e) => {
        if (alive) setError(e);
      });
    return () => {
      alive = false;
    };
  }, [path, serial]);
  return { data, error, reload: () => setSerial((x) => x + 1) };
}
export function PageHeading({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
      </div>
      {children}
    </header>
  );
}
export function Art({
  slug,
  className = "",
}: {
  slug: string;
  className?: string;
}) {
  return (
    <div className={"art art-" + slug + " " + className}>
      <div className="art-fallback" aria-hidden="true">
        <span>DiFede</span>
        <i>Game night</i>
      </div>
      <picture>
        <source
          type="image/webp"
          srcSet={`/art/${slug}-768.webp 768w, /art/${slug}-1536.webp 1536w`}
          sizes="(max-width: 600px) 90vw, (max-width: 1200px) 40vw, 500px"
        />
        <img
          src={"/art/" + slug + ".png"}
          alt=""
          loading={slug === "hero" ? "eager" : "lazy"}
          decoding="async"
          onError={(e) => {
            e.currentTarget.style.display = "none";
          }}
        />
      </picture>
    </div>
  );
}
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  busy = false,
  onCloseFocus,
}: {
  onCloseFocus?: () => void;
  busy?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content
          className="dialog"
          onCloseAutoFocus={(e) => {
            if (onCloseFocus) {
              e.preventDefault();
              onCloseFocus();
            }
          }}
          onEscapeKeyDown={(e) => {
            if (busy) e.preventDefault();
          }}
          onInteractOutside={(e) => {
            if (busy) e.preventDefault();
          }}
        >
          <Dialog.Title>{title}</Dialog.Title>
          <Dialog.Description>{description}</Dialog.Description>
          {children}
          <Dialog.Close
            disabled={busy}
            className="icon-button dialog-close"
            aria-label="Close dialog"
          >
            ×
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function Busy({ error }: { error?: unknown }) {
  return error ? (
    <ErrorNote error={error} />
  ) : (
    <div className="loading" role="status">
      <span className="loading-dot" /> Setting the table…
    </div>
  );
}

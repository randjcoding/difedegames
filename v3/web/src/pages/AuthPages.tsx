import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { Me } from "../types";
import { Art, Busy, ErrorNote, PageHeading, useLoad } from "../core";
export function Login({ onLogin }: { onLogin: (me: Me) => void }) {
  const [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    const form = new FormData(e.currentTarget);
    try {
      onLogin(
        await api<Me>("/auth/login", "POST", {
          email: form.get("email"),
          password: form.get("password"),
        }),
      );
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-layout">
      <section className="login-story">
        <span className="eyebrow">THE BEST NIGHTS ARE SHARED</span>
        <h1>
          A place
          <br />
          at the <em>table.</em>
        </h1>
        <p>
          The cards, the friendly competition, the people you love. We’ll keep
          the score.
        </p>
        <Art slug="hero" />
        <span className="edition-label">THE DIFEDE GAMES COLLECTION</span>
      </section>
      <section className="login-form">
        <span className="eyebrow">WELCOME BACK</span>
        <h2>
          Your next game
          <br />
          starts here.
        </h2>
        <p className="muted">
          Sign in to your invitation-only V3 pilot account.
        </p>
        <form onSubmit={submit}>
          <label>
            Email
            <input type="email" name="email" autoComplete="username" required />
          </label>
          <label>
            Password
            <input
              type="password"
              name="password"
              autoComplete="current-password"
              required
            />
          </label>
          <ErrorNote error={error} />
          <button className="button primary wide" disabled={busy}>
            {busy ? "Signing in…" : "Take your seat →"}
          </button>
        </form>
        <Link to="/forgot-password" className="text-link">
          Forgot your password?
        </Link>
        <div className="small-note">
          New here? Use the invitation link from your group organizer. Your
          existing history stays attached to your person.
        </div>
      </section>
    </div>
  );
}

export function Invitation({
  me,
  onJoined,
}: {
  me?: Me;
  onJoined: () => Promise<void>;
}) {
  const { token } = useParams(),
    nav = useNavigate(),
    { data, error } = useLoad<{
      group_name?: string;
      invited_name?: string;
      group?: { name: string };
      name?: string;
    }>(`/invites/${token}`),
    [failure, setFailure] = useState<unknown>(),
    [busy, setBusy] = useState(false);
  async function accept(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setFailure(undefined);
    const f = new FormData(e.currentTarget);
    try {
      await api(
        `/invites/${token}/accept`,
        "POST",
        me
          ? {}
          : {
              name: f.get("name"),
              email: f.get("email"),
              password: f.get("password"),
            },
      );
      await onJoined();
      nav("/");
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }
  if (!data) return <Busy error={error} />;
  return (
    <section className="invite-page paper">
      <span className="eyebrow">THERE’S A SEAT FOR YOU</span>
      <h1>You’re invited.</h1>
      <p>
        Join {data.group_name || data.group?.name || data.name || "your group"}
        {data.invited_name
          ? ` as ${data.invited_name}, keeping this person’s history`
          : ""}
        .
      </p>
      <form onSubmit={accept}>
        {me ? (
          <p>
            Join using your existing identity:{" "}
            <strong>{me.user.display_name || me.user.name}</strong>.
          </p>
        ) : (
          <>
            <label>
              Your full name
              <input
                name="name"
                defaultValue={data.invited_name || ""}
                autoComplete="name"
                required
                maxLength={100}
              />
            </label>
            <label>
              Email
              <input name="email" type="email" autoComplete="email" required />
            </label>
            <label>
              Create password
              <input
                name="password"
                type="password"
                minLength={10}
                autoComplete="new-password"
                required
              />
              <small>Use at least 10 characters.</small>
            </label>
          </>
        )}
        <ErrorNote error={failure} />
        <button className="button primary wide" disabled={busy}>
          {busy ? "Joining…" : "Accept invitation →"}
        </button>
      </form>
      {!me && (
        <p className="small-note">
          Already have a V3 account? <Link to="/">Sign in</Link>, then reopen
          this invitation to join with your existing identity.
        </p>
      )}
    </section>
  );
}

export function NotFound() {
  return (
    <div className="empty-state">
      <h1>This table isn’t here.</h1>
      <Link to="/" className="button primary">
        Back home
      </Link>
    </div>
  );
}
export function ForgotPassword() {
  const [message, setMessage] = useState(""),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false);
  return (
    <section className="paper invite-page">
      <PageHeading
        eyebrow="GET BACK TO GAME NIGHT"
        title="Reset your password"
      />
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(undefined);
          const form = new FormData(e.currentTarget);
          try {
            const result = await api<{ message: string }>(
              "/auth/password-reset",
              "POST",
              { email: form.get("email") },
            );
            setMessage(
              result.message ||
                "Your request has been received. If delivery is configured and the account is eligible, follow the reset instructions.",
            );
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          Sign-in email
          <input name="email" type="email" autoComplete="email" required />
        </label>
        <ErrorNote error={error} />
        {message && (
          <p className="notice" role="status">
            {message}
          </p>
        )}
        <button className="button primary" disabled={busy}>
          Request reset instructions
        </button>
      </form>
      <p className="small-note">
        This does not reveal whether an account exists. If email delivery is
        unavailable, ask your administrator for a private reset link.
      </p>
      <Link className="text-link" to="/">
        ← Sign in
      </Link>
    </section>
  );
}
export function ResetPassword({ onReset }: { onReset: () => void }) {
  const { token } = useParams(),
    check = useLoad<{ valid: boolean }>("/auth/password-reset/" + token),
    [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false),
    [done, setDone] = useState(false);
  if (!check.data)
    return (
      <section className="paper invite-page">
        <Busy error={check.error} />
        <Link className="text-link" to="/forgot-password">
          Request new instructions →
        </Link>
      </section>
    );
  return (
    <section className="paper invite-page">
      <PageHeading
        eyebrow="A FRESH START"
        title={done ? "Password updated" : "Choose a new password"}
      />
      {done ? (
        <>
          <p>
            Sign in again with your new password. Previous sessions have been
            signed out.
          </p>
          <Link className="button primary" to="/">
            Sign in →
          </Link>
        </>
      ) : (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const form = new FormData(e.currentTarget);
            if (form.get("password") !== form.get("confirm")) {
              setError(new Error("The passwords do not match."));
              return;
            }
            setBusy(true);
            setError(undefined);
            try {
              await api("/auth/password-reset/" + token, "POST", {
                password: form.get("password"),
              });
              setDone(true);
              onReset();
            } catch (e) {
              setError(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            New password
            <input
              type="password"
              name="password"
              minLength={10}
              required
              autoComplete="new-password"
            />
          </label>
          <label>
            Confirm new password
            <input
              type="password"
              name="confirm"
              minLength={10}
              required
              autoComplete="new-password"
            />
          </label>
          <ErrorNote error={error} />
          <button className="button primary" disabled={busy}>
            Save new password
          </button>
        </form>
      )}
    </section>
  );
}

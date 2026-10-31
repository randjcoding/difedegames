import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { Busy, ErrorNote, PageHeading, useAuth, useLoad } from "../core";
import type { Group, Me } from "../types";
type Profile = {
  user: Me["user"];
  person: {
    id: number;
    name: string;
    first_name: string;
    last_name: string;
    email: string | null;
    is_discoverable: boolean;
    show_full_last_name: boolean;
    is_minor: boolean;
  } | null;
  contact: Record<string, string | null>;
  groups: Group[];
};
export function Account({ onLogout }: { onLogout: () => void }) {
  const { me, refresh } = useAuth(),
    loaded = useLoad<Profile>("/profile");
  const [profile, setProfile] = useState<Profile>(),
    [error, setError] = useState<unknown>(),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (loaded.data) setProfile(loaded.data);
  }, [loaded.data]);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!profile) return;
    setBusy(true);
    setError(undefined);
    setMessage("");
    try {
      const next = await api<Profile>("/profile", "PATCH", {
        name: profile.person?.name,
        first_name: profile.person?.first_name,
        last_name: profile.person?.last_name,
        person_email: profile.person?.email || null,
        is_discoverable: profile.person?.is_discoverable,
        show_full_last_name: profile.person?.show_full_last_name,
        contact: profile.contact,
      });
      setProfile(next);
      await refresh();
      setMessage("Your profile has been saved.");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function password(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    if (form.get("password") !== form.get("confirm")) {
      setError(new Error("The new passwords do not match."));
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await api("/auth/password", "POST", {
        current_password: form.get("current"),
        new_password: form.get("password"),
      });
      onLogout();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function logout() {
    setBusy(true);
    try {
      await api("/auth/logout", "POST", {});
      onLogout();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onLogout();
      else setError(e);
    } finally {
      setBusy(false);
    }
  }
  if (!profile) return <Busy error={loaded.error} />;
  const person = profile.person;
  return (
    <>
      <PageHeading eyebrow="YOUR PLACE AT THE TABLE" title="Your account." />
      <ErrorNote error={error} />
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      <div className="account-grid">
        <form className="paper feature-panel" onSubmit={save}>
          <h2>Profile & privacy</h2>
          <label>
            Display name
            <input
              required
              maxLength={100}
              value={profile.person?.name || ""}
              onChange={(e) =>
                setProfile({
                  ...profile,
                  person: profile.person
                    ? { ...profile.person, name: e.target.value }
                    : null,
                })
              }
            />
          </label>
          <p className="small-note">
            Sign-in email: {profile.user.email}. Ask an administrator if this
            needs repair.
          </p>
          {person && (
            <>
              <div className="form-grid">
                <label>
                  First name
                  <input
                    value={person.first_name || ""}
                    onChange={(e) =>
                      setProfile({
                        ...profile,
                        person: { ...person, first_name: e.target.value },
                      })
                    }
                  />
                </label>
                <label>
                  Last name
                  <input
                    value={person.last_name || ""}
                    onChange={(e) =>
                      setProfile({
                        ...profile,
                        person: { ...person, last_name: e.target.value },
                      })
                    }
                  />
                </label>
              </div>
              <label>
                Person contact email
                <input
                  type="email"
                  value={person.email || ""}
                  onChange={(e) =>
                    setProfile({
                      ...profile,
                      person: { ...person, email: e.target.value },
                    })
                  }
                />
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={person.is_discoverable}
                  onChange={(e) =>
                    setProfile({
                      ...profile,
                      person: { ...person, is_discoverable: e.target.checked },
                    })
                  }
                />
                Let people find my profile in the directory
              </label>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={person.show_full_last_name}
                  onChange={(e) =>
                    setProfile({
                      ...profile,
                      person: {
                        ...person,
                        show_full_last_name: e.target.checked,
                      },
                    })
                  }
                />
                Show my full last name in shared statistics
              </label>
              {person.is_minor && (
                <p className="small-note">
                  This identity is marked as a minor. Contact your group
                  administrator for changes to that flag.
                </p>
              )}
            </>
          )}
          <details>
            <summary>Private contact details</summary>
            <p className="small-note">
              These details are restricted to your account and authorized
              administrators.
            </p>
            <div className="form-grid">
              {[
                ["address", "Street address"],
                ["city", "City"],
                ["state", "State"],
                ["zipcode", "Postal code"],
                ["phone_number", "Phone number"],
              ].map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    value={profile.contact[key] || ""}
                    onChange={(e) =>
                      setProfile({
                        ...profile,
                        contact: { ...profile.contact, [key]: e.target.value },
                      })
                    }
                  />
                </label>
              ))}
            </div>
          </details>
          <button className="button primary" disabled={busy}>
            Save profile
          </button>
        </form>
        <div className="feature-stack">
          <section className="paper feature-panel">
            <h2>Your groups</h2>
            {profile.groups.map((g) => (
              <Link
                className="management-link"
                key={g.id}
                to={"/groups/" + g.id}
              >
                <span>{g.name}</span>
                <span className="tag">
                  {g.role === "admin" ? "Group lead" : "Member"}
                </span>
              </Link>
            ))}
            <p className="small-note">
              One identity can belong to several groups. Leaving a group does
              not remove your scores or account.
            </p>
            {me.user.site_role === "super_admin" && (
              <Link className="button secondary" to="/admin">
                Open administration →
              </Link>
            )}
            <button
              className="button secondary"
              disabled={busy}
              onClick={logout}
            >
              Sign out
            </button>
          </section>
          <form className="paper feature-panel" onSubmit={password}>
            <h2>Change password</h2>
            <label>
              Current password
              <input
                name="current"
                type="password"
                autoComplete="current-password"
                required
              />
            </label>
            <label>
              New password
              <input
                name="password"
                type="password"
                autoComplete="new-password"
                minLength={10}
                required
              />
            </label>
            <label>
              Confirm new password
              <input
                name="confirm"
                type="password"
                autoComplete="new-password"
                minLength={10}
                required
              />
            </label>
            <p className="small-note">
              Changing your password signs out every session, including this
              one.
            </p>
            <button className="button primary" disabled={busy}>
              Change password & sign out
            </button>
          </form>
        </div>
      </div>
    </>
  );
}

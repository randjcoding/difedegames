import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import { api, ApiError, setCsrf } from "./api";
import type { Me } from "./types";
import { Auth, Busy, ErrorNote } from "./core";
import GameBoard from "./GameBoard";
import { SavedSetups } from "./pages/SavedSetups";
import { Directory } from "./pages/Directory";
import { AppearancePicker } from "./AppearancePicker";
import {
  Analytics,
  PersonStatistics,
  GroupStatistics,
} from "./pages/Analytics";
import { Admin } from "./pages/Admin";
import { GroupDetails } from "./pages/GroupDetails";
import { Corrections, GameAudit } from "./pages/Corrections";
import { selectAppearanceAccount } from "./appearance";
import {
  Login,
  Invitation,
  NotFound,
  ForgotPassword,
  ResetPassword,
} from "./pages/AuthPages";
import { Home } from "./pages/Home";
import { Library } from "./pages/Library";
import { NewGame } from "./pages/NewGame";
import { People } from "./pages/People";
import { History } from "./pages/History";
import { Account } from "./pages/Account";
function Mark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 32 32">
        <path
          d="M5 10 16 4 27 10v12L16 28 5 22Z M5 10l11 6 11-6 M16 16v12"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
        />
        <circle cx="16" cy="10" r="1.5" fill="currentColor" />
      </svg>
    </span>
  );
}

export default function App() {
  const [me, setMe] = useState<Me>(),
    [ready, setReady] = useState(false),
    [authError, setAuthError] = useState<unknown>();
  const refresh = async () => {
    const user = await api<Me>("/me");
    setCsrf(user.csrf_token);
    setMe(user);
  };
  useEffect(() => {
    refresh()
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) setAuthError(e);
      })
      .finally(() => setReady(true));
  }, []);
  useEffect(() => {
    const expired = () => {
      setMe(undefined);
      setCsrf("");
      setAuthError(
        new Error(
          "Your session expired. Sign in again; your score drafts remain on this device.",
        ),
      );
    };
    window.addEventListener("difede-session-expired", expired);
    return () => window.removeEventListener("difede-session-expired", expired);
  }, []);
  useEffect(() => {
    selectAppearanceAccount(me?.user.id, me?.preferences);
  }, [me?.user.id, me?.preferences?.style, me?.preferences?.palette]);
  useEffect(() => {
    const viewport = window.visualViewport;
    const update = () => {
      const focused = document.activeElement instanceof HTMLInputElement;
      document.documentElement.dataset.keyboard =
        focused && !!viewport && window.innerHeight - viewport.height > 130
          ? "true"
          : "false";
      document.documentElement.style.setProperty(
        "--visible-height",
        `${viewport?.height || window.innerHeight}px`,
      );
    };
    viewport?.addEventListener("resize", update);
    document.addEventListener("focusin", update);
    document.addEventListener("focusout", update);
    update();
    return () => {
      viewport?.removeEventListener("resize", update);
      document.removeEventListener("focusin", update);
      document.removeEventListener("focusout", update);
    };
  }, []);
  if (!ready) return <Busy />;
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <Link className="brand" to="/">
          <Mark />
          <span>
            DiFede<span className="brand-sub">GAMES · V3 PILOT</span>
          </span>
        </Link>
        <div className="top-actions">
          <a className="v2-link" href="https://games.difedes.com">
            Return to V2 ↗
          </a>
          <AppearancePicker me={me} refresh={refresh} />
          {me && (
            <Link to="/account" className="avatar" aria-label="Your account">
              {(me.user.display_name || me.user.name).slice(0, 1).toUpperCase()}
            </Link>
          )}
        </div>
      </header>
      {me && (
        <nav className="navigation" aria-label="Main navigation">
          {[
            ["/", "Home"],
            ["/games", "Games"],
            ["/leaderboards", "Stats"],
            ["/people", "People"],
            ["/history", "History"],
          ].map(([url, label], index) => (
            <NavLink key={url} to={url} end={url === "/"}>
              <span className="nav-number">0{index + 1}</span>
              {label}
            </NavLink>
          ))}
          <span className="nav-caption">
            Good company.
            <br />
            Great game nights.
          </span>
        </nav>
      )}
      <main id="main" className={me ? "main authenticated" : "main"}>
        {!!authError && <ErrorNote error={authError} />}
        <Routes>
          <Route path="/forgot-password" element={<ForgotPassword />} />
          <Route
            path="/reset-password/:token"
            element={
              <ResetPassword
                onReset={() => {
                  setMe(undefined);
                  setCsrf("");
                }}
              />
            }
          />
          <Route
            path="/invite/:token"
            element={<Invitation me={me} onJoined={refresh} />}
          />
          {me ? (
            <Route
              path="*"
              element={
                <Auth.Provider value={{ me, refresh }}>
                  <Routes>
                    <Route index element={<Home />} />
                    <Route path="games" element={<Library />} />
                    <Route path="setups" element={<SavedSetups />} />
                    <Route path="directory" element={<Directory />} />
                    <Route path="leaderboards" element={<Analytics />} />
                    <Route path="person/:id" element={<PersonStatistics />} />
                    <Route path="groups/:id" element={<GroupDetails />} />
                    <Route
                      path="groups/:id/statistics"
                      element={<GroupStatistics />}
                    />
                    <Route path="admin" element={<Admin />} />
                    <Route path="game/:id/correct" element={<Corrections />} />
                    <Route path="game/:id/audit" element={<GameAudit />} />
                    <Route path="new/:slug" element={<NewGame />} />
                    <Route path="game/:id" element={<GameBoard />} />
                    <Route path="people" element={<People />} />
                    <Route path="history" element={<History />} />
                    <Route
                      path="account"
                      element={
                        <Account
                          onLogout={() => {
                            setMe(undefined);
                            setCsrf("");
                          }}
                        />
                      }
                    />
                    <Route path="*" element={<NotFound />} />
                  </Routes>
                </Auth.Provider>
              }
            />
          ) : (
            <Route
              path="*"
              element={
                <Login
                  onLogin={(user) => {
                    setCsrf(user.csrf_token);
                    setMe(user);
                    setAuthError(undefined);
                  }}
                />
              }
            />
          )}
        </Routes>
        <footer className="page-footer">
          <span>Made for a place around the table.</span>
          <span>V3 invite-only pilot · Separate data from V2</span>
        </footer>
      </main>
    </>
  );
}

# Welcome Screen, Production Picker & Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a welcome/onboarding page for new users, a production picker for multi-production users, user avatar/initials in the header, and smooth UI transitions.

**Architecture:** Restructure `AppContent` to have three states: no auth → AuthPage, auth + no productions → WelcomePage, auth + productions → Home (with production picker if multiple). Extract the production loading logic into `AppContent` so it controls which view to show. Add a Toast system for notifications.

**Tech Stack:** React, CSS

**Spec:** `docs/superpowers/specs/2026-03-25-firebase-auth-access-control-design.md` (Sections 4-5, polish)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `frontend/src/components/WelcomePage.tsx` | New — onboarding page for users with no productions |
| `frontend/src/components/ProductionPicker.tsx` | New — production selection cards for multi-production users |
| `frontend/src/components/UserAvatar.tsx` | New — avatar/initials badge for header |
| `frontend/src/components/Toast.tsx` | New — toast notification system |
| `frontend/src/App.tsx` | Restructure routing: auth → welcome → picker → home |
| `frontend/src/styles/layout.css` | Welcome page, production picker styles |
| `frontend/src/styles/components.css` | Toast styles, avatar styles |

---

## Task 1: Create WelcomePage component

**Files:**
- Create: `frontend/src/components/WelcomePage.tsx`
- Modify: `frontend/src/styles/layout.css`

- [ ] **Step 1: Create WelcomePage.tsx**

A full-page onboarding screen shown when a user is authenticated but has no productions. Visually distinct from the document review UI — more of a landing/marketing feel.

```tsx
import { useAuth } from '../hooks/useAuth';

interface Props {
  onIngest: () => void;
}

export default function WelcomePage({ onIngest }: Props) {
  const { user, logout } = useAuth();

  return (
    <div className="welcome-page">
      <div className="welcome-header">
        <span className="welcome-logo">Descubre</span>
        <div className="welcome-user">
          <span>{user?.displayName || user?.email}</span>
          <button className="btn btn-ghost btn-sm" style={{ color: 'var(--color-primary-200)' }} onClick={logout}>Sign out</button>
        </div>
      </div>

      <div className="welcome-content">
        <h1 className="welcome-title">Welcome to Descubre</h1>
        <p className="welcome-subtitle">
          Your document review platform for e-discovery productions.
        </p>

        <div className="welcome-features">
          <div className="welcome-feature">
            <div className="welcome-feature-icon">&#x1F50D;</div>
            <h3>Search</h3>
            <p>Full-text search with boolean operators, phrase matching, and AI-powered natural language queries.</p>
          </div>
          <div className="welcome-feature">
            <div className="welcome-feature-icon">&#x1F3F7;</div>
            <h3>Tag & Code</h3>
            <p>Responsive, privilege, and custom tags with keyboard shortcuts and bulk coding.</p>
          </div>
          <div className="welcome-feature">
            <div className="welcome-feature-icon">&#x1F916;</div>
            <h3>AI Tools</h3>
            <p>Document summarization, find-similar, and natural language search powered by Claude.</p>
          </div>
        </div>

        <div className="welcome-actions">
          <button className="btn btn-primary" style={{ padding: '10px 32px', fontSize: 'var(--text-base)' }} onClick={onIngest}>
            Ingest a Production
          </button>
          <p className="welcome-hint">
            Or wait for a colleague to invite you to an existing production.
          </p>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add welcome page styles to layout.css**

Add to the end of `frontend/src/styles/layout.css`:

```css
/* ── Welcome Page ── */

.welcome-page {
  min-height: 100vh;
  background: var(--color-primary-900);
  background-image:
    radial-gradient(ellipse 80% 50% at 50% 0%, rgba(16, 42, 67, 1), transparent),
    radial-gradient(ellipse 50% 30% at 70% 100%, rgba(192, 139, 48, 0.08), transparent);
  color: var(--color-neutral-0);
  display: flex;
  flex-direction: column;
}

.welcome-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-6);
}

.welcome-logo {
  font-family: var(--font-serif);
  font-size: var(--text-xl);
  font-weight: var(--font-bold);
  letter-spacing: -0.01em;
}

.welcome-user {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-sm);
  color: var(--color-primary-300);
}

.welcome-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--space-8) var(--space-6);
  text-align: center;
  max-width: 800px;
  margin: 0 auto;
}

.welcome-title {
  font-family: var(--font-serif);
  font-size: 2.5rem;
  font-weight: var(--font-bold);
  letter-spacing: -0.02em;
  margin-bottom: var(--space-3);
}

.welcome-subtitle {
  font-size: var(--text-lg);
  color: var(--color-primary-300);
  margin-bottom: var(--space-10);
  line-height: var(--leading-relaxed);
}

.welcome-features {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-6);
  margin-bottom: var(--space-10);
  width: 100%;
}

.welcome-feature {
  padding: var(--space-5);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: var(--radius-lg);
  text-align: center;
}

.welcome-feature-icon {
  font-size: 1.75rem;
  margin-bottom: var(--space-3);
}

.welcome-feature h3 {
  font-family: var(--font-serif);
  font-size: var(--text-base);
  font-weight: var(--font-semibold);
  margin-bottom: var(--space-2);
}

.welcome-feature p {
  font-size: var(--text-sm);
  color: var(--color-primary-300);
  line-height: var(--leading-relaxed);
}

.welcome-actions {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-4);
}

.welcome-hint {
  font-size: var(--text-sm);
  color: var(--color-primary-400);
  font-style: italic;
}
```

- [ ] **Step 3: Verify TypeScript compiles**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

---

## Task 2: Create ProductionPicker component

**Files:**
- Create: `frontend/src/components/ProductionPicker.tsx`
- Modify: `frontend/src/styles/layout.css`

- [ ] **Step 1: Create ProductionPicker.tsx**

Shown when a user has multiple productions. Displays cards to pick which one to work in.

```tsx
import { useAuth } from '../hooks/useAuth';
import type { ProductionInfo } from '../types';

interface Props {
  productions: ProductionInfo[];
  onSelect: (production: ProductionInfo) => void;
  onIngest: () => void;
}

export default function ProductionPicker({ productions, onSelect, onIngest }: Props) {
  const { user, logout } = useAuth();

  return (
    <div style={{ minHeight: '100vh', background: 'var(--color-neutral-50)' }}>
      <div className="app-header">
        <span className="logo">Descubre</span>
        <div className="user-menu">
          <button className="btn-header" onClick={onIngest}>+ Ingest</button>
          <span style={{ opacity: 0.7 }}>{user?.displayName || user?.email}</span>
          <button className="btn-header" onClick={logout}>Sign out</button>
        </div>
      </div>

      <div className="content-area" style={{ paddingTop: 'var(--space-8)' }}>
        <h2 className="section-title" style={{ marginBottom: 'var(--space-6)', textAlign: 'center' }}>
          Select a Production
        </h2>

        <div className="production-grid">
          {productions.map(p => (
            <div key={p.id} className="production-card card" onClick={() => onSelect(p)}>
              <div className="production-card-name">{p.name}</div>
              {p.description && <div className="production-card-desc">{p.description}</div>}
              <div className="production-card-meta">
                {p.is_owner ? (
                  <span className="badge badge-blue">Owner</span>
                ) : (
                  <span className="badge badge-gray">Shared</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add production picker styles**

Add to `frontend/src/styles/layout.css`:

```css
/* ── Production Picker ── */

.production-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-4);
  max-width: 900px;
  margin: 0 auto;
}

.production-card {
  padding: var(--space-5);
  cursor: pointer;
  transition: box-shadow var(--transition-slow), transform var(--transition-slow);
}
.production-card:hover {
  box-shadow: var(--shadow-lg);
  transform: translateY(-2px);
}

.production-card-name {
  font-family: var(--font-serif);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  color: var(--color-primary-900);
  margin-bottom: var(--space-2);
}

.production-card-desc {
  font-size: var(--text-sm);
  color: var(--color-neutral-500);
  margin-bottom: var(--space-3);
  line-height: var(--leading-relaxed);
}

.production-card-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
```

- [ ] **Step 3: Commit**

---

## Task 3: Create UserAvatar and Toast components

**Files:**
- Create: `frontend/src/components/UserAvatar.tsx`
- Create: `frontend/src/components/Toast.tsx`
- Modify: `frontend/src/styles/components.css`

- [ ] **Step 1: Create UserAvatar.tsx**

A small initials/avatar badge for the header.

```tsx
interface Props {
  name: string | null;
  email: string;
  photoUrl?: string | null;
  size?: number;
}

export default function UserAvatar({ name, email, photoUrl, size = 28 }: Props) {
  const initials = name
    ? name.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase()
    : email[0].toUpperCase();

  if (photoUrl) {
    return (
      <img
        src={photoUrl}
        alt={name || email}
        style={{
          width: size,
          height: size,
          borderRadius: '50%',
          objectFit: 'cover',
          border: '2px solid rgba(255,255,255,0.15)',
        }}
      />
    );
  }

  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: '50%',
        background: 'var(--color-brand-500)',
        color: '#fff',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: size * 0.4,
        fontWeight: 600,
        letterSpacing: '0.02em',
        flexShrink: 0,
      }}
    >
      {initials}
    </div>
  );
}
```

- [ ] **Step 2: Create Toast.tsx**

A simple toast notification system.

```tsx
import { useEffect, useState } from 'react';

export interface ToastMessage {
  id: number;
  text: string;
  type: 'success' | 'error' | 'info';
}

let toastId = 0;
let addToastFn: ((msg: Omit<ToastMessage, 'id'>) => void) | null = null;

export function showToast(text: string, type: ToastMessage['type'] = 'info') {
  addToastFn?.({ text, type });
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  useEffect(() => {
    addToastFn = (msg) => {
      const id = ++toastId;
      setToasts(prev => [...prev, { ...msg, id }]);
      setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 4000);
    };
    return () => { addToastFn = null; };
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div key={t.id} className={`toast toast-${t.type}`}>
          {t.text}
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Add styles to components.css**

Add to `frontend/src/styles/components.css`:

```css
/* ── Toast ── */

.toast-container {
  position: fixed;
  bottom: var(--space-6);
  right: var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  z-index: 300;
}

.toast {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  box-shadow: var(--shadow-lg);
  animation: toast-in 0.2s ease;
  max-width: 360px;
}

@keyframes toast-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.toast-success {
  background: var(--color-success-50);
  color: var(--color-success-700);
  border: 1px solid var(--color-success-100);
}

.toast-error {
  background: var(--color-danger-50);
  color: var(--color-danger-700);
  border: 1px solid var(--color-danger-100);
}

.toast-info {
  background: var(--color-primary-50);
  color: var(--color-primary-700);
  border: 1px solid var(--color-primary-100);
}
```

- [ ] **Step 4: Commit**

---

## Task 4: Restructure App.tsx with routing and polish

**Files:**
- Modify: `frontend/src/App.tsx`

This is the key integration task. Restructure `AppContent` to control the full app flow.

- [ ] **Step 1: Restructure AppContent**

Read `frontend/src/App.tsx`. The current flow is:
- `AppContent`: loading → AuthPage → Home

Change to:
- `AppContent`: loading → AuthPage → `AppRouter`
- `AppRouter`: loads productions → WelcomePage (0 prods) → ProductionPicker (2+ prods) → Home (1 prod or after selection)

Add imports for WelcomePage, ProductionPicker, UserAvatar, ToastContainer.

Restructure AppContent:

```tsx
function AppContent() {
  const { user, loading } = useAuth();
  if (loading) return <div className="loading-center"><span className="spinner spinner-md" /> Loading...</div>;
  if (!user) return <AuthPage />;
  return <AppRouter />;
}

function AppRouter() {
  const [productions, setProductions] = useState<ProductionInfo[]>([]);
  const [activeProduction, setActiveProduction] = useState<ProductionInfo | null>(null);
  const [prodLoading, setProdLoading] = useState(true);
  const [showIngestWizard, setShowIngestWizard] = useState(false);

  const loadProductions = async () => {
    setProdLoading(true);
    try {
      const prods = await listProductions();
      setProductions(prods);
      if (prods.length === 1) setActiveProduction(prods[0]);
    } catch {}
    setProdLoading(false);
  };

  useEffect(() => { loadProductions(); }, []);

  const handleIngestComplete = () => {
    loadProductions();
  };

  if (prodLoading) {
    return <div className="loading-center"><span className="spinner spinner-md" /> Loading...</div>;
  }

  // No productions — show welcome page
  if (productions.length === 0) {
    return (
      <>
        <WelcomePage onIngest={() => setShowIngestWizard(true)} />
        {showIngestWizard && (
          <IngestWizard onClose={() => setShowIngestWizard(false)} onComplete={handleIngestComplete} />
        )}
        <ToastContainer />
      </>
    );
  }

  // Multiple productions, none selected — show picker
  if (!activeProduction) {
    return (
      <>
        <ProductionPicker
          productions={productions}
          onSelect={setActiveProduction}
          onIngest={() => setShowIngestWizard(true)}
        />
        {showIngestWizard && (
          <IngestWizard onClose={() => setShowIngestWizard(false)} onComplete={handleIngestComplete} />
        )}
        <ToastContainer />
      </>
    );
  }

  // Active production — show Home
  return (
    <>
      <Home
        production={activeProduction}
        onSwitchProduction={() => setActiveProduction(null)}
        onIngestComplete={handleIngestComplete}
      />
      <ToastContainer />
    </>
  );
}
```

- [ ] **Step 2: Update Home component**

The Home component currently manages its own production state. Refactor it to accept `production` as a prop:

```tsx
interface HomeProps {
  production: ProductionInfo;
  onSwitchProduction: () => void;
  onIngestComplete: () => void;
}

function Home({ production, onSwitchProduction, onIngestComplete }: HomeProps) {
```

Remove the `activeProduction` state — use `production` prop instead. Remove production loading from useEffect. Update all references from `activeProduction` to `production`.

Update the header to use UserAvatar and show a "Switch" button if needed:

```tsx
<div className="app-header">
  <span className="logo" onClick={clearSearch}>Descubre</span>
  <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-primary-300)', opacity: 0.7 }}>/</span>
  <span style={{ fontSize: 'var(--text-sm)', color: 'var(--color-primary-200)', cursor: 'pointer' }} onClick={onSwitchProduction}>
    {production.name}
  </span>
  {production.is_owner && (
    <button className="btn-header" onClick={() => setShowManageAccess(true)}>Share</button>
  )}
  <div className="user-menu">
    <button className="btn-header" onClick={() => setShowIngestWizard(true)}>+ Ingest</button>
    <UserAvatar name={user?.displayName ?? null} email={user?.email ?? ''} size={26} />
    <span style={{ opacity: 0.7 }}>{user?.displayName || user?.email}</span>
    <button className="btn-header" onClick={logout}>Sign out</button>
  </div>
</div>
```

- [ ] **Step 3: Verify TypeScript**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Commit**

import { useAuth } from '../hooks/useAuth';

interface Props {
  onIngest: () => void;
}

export default function WelcomePage({ onIngest }: Props) {
  const { user, logout } = useAuth();

  return (
    <div className="welcome-page">
      <div className="welcome-header">
        <span className="welcome-logo">Vigilist</span>
        <div className="welcome-user">
          <span>{user?.displayName || user?.email}</span>
          <button className="btn btn-ghost btn-sm" style={{ color: 'var(--color-primary-200)' }} onClick={logout}>Sign out</button>
        </div>
      </div>

      <div className="welcome-content">
        <h1 className="welcome-title">Welcome to Vigilist</h1>
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
            <h3>Tag &amp; Code</h3>
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

import { useEffect, useRef, useState } from 'react';
import Omnibox from './Omnibox';
import UserAvatar from './UserAvatar';
import { useAuth } from '../hooks/useAuth';
import type { ProductionInfo } from '../types';
import type { SearchMode } from '../utils/searchMode';

interface AppHeaderProps {
  production?: ProductionInfo;
  productions: ProductionInfo[];
  onSelectProduction?: (p: ProductionInfo) => void;
  onShowAllProductions?: () => void;
  onSearch?: (query: string, metadata?: Record<string, string>, forceMode?: SearchMode) => void;
  onLogoClick?: () => void;
  initialQuery?: string;
  onAsk?: (question: string) => void;
  onOpenReview?: () => void;
  onOpenDashboard?: () => void;
  onOpenShare?: () => void;
  onOpenSettings?: () => void;
  onOpenAudit?: () => void;
  onOpenIngest?: () => void;
  onOpenGuide?: () => void;
  onRandomDoc?: () => void;
}

/**
 * The command bar: brand, production switcher, search-or-ask omnibox,
 * the two daily-use actions (Review, Dashboard), and a gear menu holding
 * everything administrative. Shared by Home and the case-desk picker.
 */
export default function AppHeader({
  production,
  productions,
  onSelectProduction,
  onShowAllProductions,
  onSearch,
  onLogoClick,
  initialQuery,
  onAsk,
  onOpenReview,
  onOpenDashboard,
  onOpenShare,
  onOpenSettings,
  onOpenAudit,
  onOpenIngest,
  onOpenGuide,
  onRandomDoc,
}: AppHeaderProps) {
  const { user, logout } = useAuth();
  const [openMenu, setOpenMenu] = useState<'none' | 'switcher' | 'gear'>('none');
  const switcherRef = useRef<HTMLDivElement>(null);
  const gearRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as Node;
      if (switcherRef.current?.contains(t) || gearRef.current?.contains(t)) return;
      setOpenMenu('none');
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const gearItems = [
    onOpenShare && { label: 'Share…', action: onOpenShare },
    onOpenSettings && { label: 'Production settings', action: onOpenSettings },
    onOpenIngest && { label: '＋ Ingest a production', action: onOpenIngest },
    onRandomDoc && { label: 'Random document', action: onRandomDoc },
    onOpenAudit && { label: 'Audit log', action: onOpenAudit },
    onOpenGuide && { label: 'Guide', action: onOpenGuide },
  ].filter(Boolean) as { label: string; action: () => void }[];

  return (
    <header className="command-bar">
      <span
        className="command-bar-logo"
        onClick={onLogoClick}
        role={onLogoClick ? 'button' : undefined}
      >
        Vigilist
      </span>

      {production && (
        <div className="cb-switcher" ref={switcherRef}>
          <button
            type="button"
            className="cb-switcher-btn"
            onClick={() => setOpenMenu(openMenu === 'switcher' ? 'none' : 'switcher')}
            aria-haspopup="menu"
            aria-expanded={openMenu === 'switcher'}
          >
            {production.name}
            {productions.length > 1 && <span className="cb-caret">▾</span>}
          </button>
          {openMenu === 'switcher' && (
            <div className="dropdown cb-menu" role="menu">
              {productions.map(p => (
                <button
                  key={p.id}
                  type="button"
                  role="menuitem"
                  className={`dropdown-item ${p.id === production.id ? 'is-current' : ''}`}
                  onClick={() => { setOpenMenu('none'); if (p.id !== production.id) onSelectProduction?.(p); }}
                >
                  {p.name}
                </button>
              ))}
              {onShowAllProductions && (
                <button
                  type="button"
                  role="menuitem"
                  className="dropdown-item cb-menu-footer"
                  onClick={() => { setOpenMenu('none'); onShowAllProductions(); }}
                >
                  All productions…
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {onSearch && <Omnibox onSearch={onSearch} initialQuery={initialQuery} onAsk={onAsk} />}

      <div className="cb-actions">
        {onOpenReview && (
          <button type="button" className="cb-action cb-action-primary" onClick={onOpenReview}>
            ✦ Review
          </button>
        )}
        {onOpenDashboard && (
          <button type="button" className="cb-action" onClick={onOpenDashboard}>
            Dashboard
          </button>
        )}
        <div className="cb-gear" ref={gearRef}>
          <button
            type="button"
            className="cb-action cb-icon"
            onClick={() => setOpenMenu(openMenu === 'gear' ? 'none' : 'gear')}
            aria-haspopup="menu"
            aria-expanded={openMenu === 'gear'}
            aria-label="Settings and tools"
          >
            ⚙
          </button>
          {openMenu === 'gear' && (
            <div className="dropdown cb-menu cb-menu-right" role="menu">
              <div className="cb-menu-user">{user?.displayName || user?.email}</div>
              {gearItems.map(item => (
                <button
                  key={item.label}
                  type="button"
                  role="menuitem"
                  className="dropdown-item"
                  onClick={() => { setOpenMenu('none'); item.action(); }}
                >
                  {item.label}
                </button>
              ))}
              <button
                type="button"
                role="menuitem"
                className="dropdown-item cb-menu-footer"
                onClick={logout}
              >
                Sign out
              </button>
            </div>
          )}
        </div>
        <span className="cb-avatar">
          <UserAvatar name={user?.displayName ?? null} email={user?.email ?? ''} photoUrl={user?.photoURL} size={28} />
        </span>
      </div>
    </header>
  );
}

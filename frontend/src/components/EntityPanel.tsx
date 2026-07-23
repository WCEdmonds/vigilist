import { useEffect, useState } from 'react';
import { getEntity, getEntityConnections, getEntityMentions } from '../api/client';
import type { EntityConnections, EntityMentionsPage, EntityProfile } from '../types';

interface Props {
  entityId: string;
  onClose: () => void;
  onOpenEntity: (entityId: string) => void;
  onOpenDocument: (docId: string, entityId: string) => void;
}

export default function EntityPanel({ entityId, onClose, onOpenEntity, onOpenDocument }: Props) {
  const [profile, setProfile] = useState<{ id: string; value: EntityProfile } | null>(null);
  const [mentions, setMentions] = useState<{ id: string; value: EntityMentionsPage } | null>(null);
  const [connections, setConnections] = useState<{ id: string; value: EntityConnections } | null>(null);
  const [error, setError] = useState<{ id: string; value: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEntity(entityId)
      .then(p => { if (!cancelled) setProfile({ id: entityId, value: p }); })
      .catch(e => { if (!cancelled) setError({ id: entityId, value: String(e.message || e) }); });
    getEntityMentions(entityId)
      .then(m => { if (!cancelled) setMentions({ id: entityId, value: m }); })
      .catch(() => {});
    getEntityConnections(entityId)
      .then(c => { if (!cancelled) setConnections({ id: entityId, value: c }); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [entityId]);

  const currentProfile = profile?.id === entityId ? profile.value : null;
  const currentMentions = mentions?.id === entityId ? mentions.value : null;
  const currentConnections = connections?.id === entityId ? connections.value : null;
  const currentError = error?.id === entityId ? error.value : null;

  return (
    <div className="entity-panel" style={{
      position: 'absolute', top: 0, right: 0, bottom: 0, width: 380, zIndex: 30,
      background: 'var(--color-bg, #fff)', borderLeft: '1px solid var(--color-border, #ddd)',
      display: 'flex', flexDirection: 'column', boxShadow: '-4px 0 16px rgba(0,0,0,0.12)',
    }}>
      <div className="panel-header" style={{ display: 'flex', alignItems: 'center' }}>
        <span style={{ fontWeight: 600 }}>
          {currentProfile ? currentProfile.canonical_name : 'Loading…'}
          {currentProfile && (
            <span className="badge" style={{ marginLeft: 8, fontSize: 'var(--text-xs)' }}>
              {currentProfile.entity_type === 'person' ? 'Person' : 'Organization'}
            </span>
          )}
        </span>
        <button onClick={onClose} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }} aria-label="Close entity panel">✕</button>
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)' }}>
        {currentError && <div className="empty-state">{currentError}</div>}
        {currentProfile && (
          <>
            {currentProfile.attributes.role && <div style={{ marginBottom: 8, opacity: 0.85 }}>{currentProfile.attributes.role}</div>}
            {currentProfile.overview
              ? <p style={{ marginBottom: 12 }}>{currentProfile.overview}</p>
              : <p style={{ marginBottom: 12, opacity: 0.6 }}>No overview yet.</p>}
            <div style={{ marginBottom: 12, opacity: 0.75 }}>
              Mentioned {currentProfile.mention_count} times across {currentProfile.document_count} documents.
            </div>
            {currentProfile.aliases.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div className="panel-header" style={{ padding: 0 }}>Also appears as</div>
                <div>{currentProfile.aliases.join(' · ')}</div>
              </div>
            )}
          </>
        )}

        {currentConnections && (currentConnections.stated.length > 0 || currentConnections.cooccurrence.length > 0) && (
          <div style={{ marginBottom: 16 }}>
            <div className="panel-header" style={{ padding: 0 }}>Connections</div>
            {currentConnections.stated.map((c, i) => (
              <div key={`s${i}`} style={{ padding: '4px 0' }}>
                <button className="btn btn-ghost btn-xs" onClick={() => onOpenEntity(c.entity_id)}>
                  {c.canonical_name}
                </button>
                <span style={{ opacity: 0.7 }}> — {c.relationship_type?.replace(/_/g, ' ')}</span>
                {c.description && <div style={{ opacity: 0.6, fontSize: 'var(--text-xs)' }}>{c.description}</div>}
              </div>
            ))}
            {currentConnections.cooccurrence.map((c, i) => (
              <div key={`c${i}`} style={{ padding: '4px 0' }}>
                <button className="btn btn-ghost btn-xs" onClick={() => onOpenEntity(c.entity_id)}>
                  {c.canonical_name}
                </button>
                <span style={{ opacity: 0.7 }}> — appear together in {c.shared_doc_count} docs</span>
              </div>
            ))}
          </div>
        )}

        {currentMentions && currentMentions.documents.length > 0 && (
          <div>
            <div className="panel-header" style={{ padding: 0 }}>Mentions ({currentMentions.total} documents)</div>
            {currentMentions.documents.map(d => (
              <div key={d.document_id} style={{ margin: '8px 0' }}>
                <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }}
                        onClick={() => onOpenDocument(d.document_id, entityId)}>
                  {d.bates_begin}{d.title ? ` — ${d.title}` : ''}
                </button>
                {d.mentions.slice(0, 3).map((m, i) => (
                  <div key={i} style={{ opacity: 0.7, fontSize: 'var(--text-xs)', padding: '2px 0 2px 12px' }}>
                    …{m.context_snippet || m.surface_text}…
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

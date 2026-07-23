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
  const [profile, setProfile] = useState<EntityProfile | null>(null);
  const [mentions, setMentions] = useState<EntityMentionsPage | null>(null);
  const [connections, setConnections] = useState<EntityConnections | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEntity(entityId)
      .then(p => { if (!cancelled) setProfile(p); })
      .catch(e => { if (!cancelled) setError(String(e.message || e)); });
    getEntityMentions(entityId)
      .then(m => { if (!cancelled) setMentions(m); })
      .catch(() => {});
    getEntityConnections(entityId)
      .then(c => { if (!cancelled) setConnections(c); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [entityId]);

  return (
    <div className="entity-panel" style={{
      position: 'absolute', top: 0, right: 0, bottom: 0, width: 380, zIndex: 30,
      background: 'var(--color-bg, #fff)', borderLeft: '1px solid var(--color-border, #ddd)',
      display: 'flex', flexDirection: 'column', boxShadow: '-4px 0 16px rgba(0,0,0,0.12)',
    }}>
      <div className="panel-header" style={{ display: 'flex', alignItems: 'center' }}>
        <span style={{ fontWeight: 600 }}>
          {profile ? profile.canonical_name : 'Loading…'}
          {profile && (
            <span className="badge" style={{ marginLeft: 8, fontSize: 'var(--text-xs)' }}>
              {profile.entity_type === 'person' ? 'Person' : 'Organization'}
            </span>
          )}
        </span>
        <button onClick={onClose} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }} aria-label="Close entity panel">✕</button>
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)' }}>
        {error && <div className="empty-state">{error}</div>}
        {profile && (
          <>
            {profile.attributes.role && <div style={{ marginBottom: 8, opacity: 0.85 }}>{profile.attributes.role}</div>}
            {profile.overview
              ? <p style={{ marginBottom: 12 }}>{profile.overview}</p>
              : <p style={{ marginBottom: 12, opacity: 0.6 }}>No overview yet.</p>}
            <div style={{ marginBottom: 12, opacity: 0.75 }}>
              Mentioned {profile.mention_count} times across {profile.document_count} documents.
            </div>
            {profile.aliases.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div className="panel-header" style={{ padding: 0 }}>Also appears as</div>
                <div>{profile.aliases.join(' · ')}</div>
              </div>
            )}
          </>
        )}

        {connections && (connections.stated.length > 0 || connections.cooccurrence.length > 0) && (
          <div style={{ marginBottom: 16 }}>
            <div className="panel-header" style={{ padding: 0 }}>Connections</div>
            {connections.stated.map((c, i) => (
              <div key={`s${i}`} style={{ padding: '4px 0' }}>
                <button className="btn btn-ghost btn-xs" onClick={() => onOpenEntity(c.entity_id)}>
                  {c.canonical_name}
                </button>
                <span style={{ opacity: 0.7 }}> — {c.relationship_type?.replace(/_/g, ' ')}</span>
                {c.description && <div style={{ opacity: 0.6, fontSize: 'var(--text-xs)' }}>{c.description}</div>}
              </div>
            ))}
            {connections.cooccurrence.map((c, i) => (
              <div key={`c${i}`} style={{ padding: '4px 0' }}>
                <button className="btn btn-ghost btn-xs" onClick={() => onOpenEntity(c.entity_id)}>
                  {c.canonical_name}
                </button>
                <span style={{ opacity: 0.7 }}> — appear together in {c.shared_doc_count} docs</span>
              </div>
            ))}
          </div>
        )}

        {mentions && mentions.documents.length > 0 && (
          <div>
            <div className="panel-header" style={{ padding: 0 }}>Mentions ({mentions.total} documents)</div>
            {mentions.documents.map(d => (
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

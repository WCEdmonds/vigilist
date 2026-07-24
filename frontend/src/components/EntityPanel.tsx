import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getEntity, getEntityConnections, getEntityMentions } from '../api/client';
import { entityDisplayName } from '../utils/entityDisplay';
import type { EntityConnection, EntityConnections, EntityMentionsPage, EntityProfile } from '../types';

/** One row per counterpart+relationship: relationships are stored
 * per-document (that's the citation trail), but the panel reads better
 * aggregated — count of supporting documents, evidence quotes behind an
 * expander. */
interface ConnGroup {
  key: string;
  entityId: string;
  name: string;
  relationship: string;
  count: number;
  descriptions: string[];
}

function groupStated(stated: EntityConnection[]): ConnGroup[] {
  const groups = new Map<string, ConnGroup>();
  for (const c of stated) {
    const rel = c.relationship_type || 'other';
    const key = `${c.entity_id}|${rel}`;
    let g = groups.get(key);
    if (!g) {
      g = { key, entityId: c.entity_id, name: entityDisplayName(c.canonical_name), relationship: rel.replace(/_/g, ' '), count: 0, descriptions: [] };
      groups.set(key, g);
    }
    g.count += 1;
    if (c.description && !g.descriptions.includes(c.description)) g.descriptions.push(c.description);
  }
  return [...groups.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

interface Props {
  entityId: string;
  onClose: () => void;
  onOpenEntity: (entityId: string) => void;
  onOpenDocument: (docId: string, entityId: string) => void;
}

/** Mention snippet with the entity's surface text marker-highlighted. */
function Snippet({ text, name }: { text: string; name: string }) {
  const idx = name ? text.toLowerCase().indexOf(name.toLowerCase()) : -1;
  if (idx === -1) return <>…{text}…</>;
  return (
    <>
      …{text.slice(0, idx)}
      <span className="marker-hl">{text.slice(idx, idx + name.length)}</span>
      {text.slice(idx + name.length)}…
    </>
  );
}

export default function EntityPanel({ entityId, onClose, onOpenEntity, onOpenDocument }: Props) {
  const [profile, setProfile] = useState<{ id: string; value: EntityProfile } | null>(null);
  const [mentions, setMentions] = useState<{ id: string; value: EntityMentionsPage } | null>(null);
  const [connections, setConnections] = useState<{ id: string; value: EntityConnections } | null>(null);
  const [error, setError] = useState<{ id: string; value: string } | null>(null);
  const [openConnKey, setOpenConnKey] = useState<string | null>(null);

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
  const loading = !currentProfile && !currentError;

  return (
    <div className="entity-panel" style={{
      position: 'absolute', top: 0, right: 0, bottom: 0, width: 380, zIndex: 30,
      background: 'var(--color-card)', borderLeft: '1px solid var(--color-neutral-200)',
      display: 'flex', flexDirection: 'column', boxShadow: '-4px 0 16px rgba(20,24,29,0.12)',
    }}>
      <div className="panel-header" style={{ display: 'flex', alignItems: 'center' }}>
        <span style={{ fontWeight: 600 }}>
          {currentProfile ? currentProfile.canonical_name : 'Entity'}
          {currentProfile && (
            <span className={`entity-dot entity-${currentProfile.entity_type}`} style={{ marginLeft: 8 }}>●</span>
          )}
          {currentProfile && (
            <span style={{ marginLeft: 4, fontWeight: 400 }}>
              {currentProfile.entity_type === 'person' ? 'Person' : 'Organization'}
            </span>
          )}
        </span>
        <button onClick={onClose} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }} aria-label="Close entity panel">✕</button>
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)' }}>
        {currentError && <div className="empty-state">{currentError}</div>}

        {loading && (
          <div className="entity-loading">
            <span className="spinner spinner-md" />
            <span>Pulling the record on this entity…</span>
          </div>
        )}

        {currentProfile && (
          <>
            {currentProfile.attributes.role && (
              <div className="entity-role">{currentProfile.attributes.role}</div>
            )}
            <div className="entity-stat-line">
              {currentProfile.mention_count.toLocaleString()}&nbsp;MENTIONS&nbsp;·&nbsp;{currentProfile.document_count.toLocaleString()}&nbsp;DOCUMENTS
            </div>
            {currentProfile.overview
              ? (
                <div className="entity-overview">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{currentProfile.overview}</ReactMarkdown>
                </div>
              )
              : <p style={{ margin: '0 0 12px', opacity: 0.6 }}>No overview yet.</p>}
            {currentProfile.aliases.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div className="panel-header" style={{ padding: 0, background: 'none', border: 'none' }}>Also appears as</div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}>
                  {currentProfile.aliases.map(a => <span key={a} className="badge badge-gray">{a}</span>)}
                </div>
              </div>
            )}
          </>
        )}

        {currentConnections && (currentConnections.stated.length > 0 || currentConnections.cooccurrence.length > 0) && (
          <div style={{ marginBottom: 16 }}>
            <div className="panel-header" style={{ padding: 0, background: 'none', border: 'none' }}>Connections</div>
            {groupStated(currentConnections.stated).map(g => (
              <div key={g.key} className="entity-conn">
                <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }} onClick={() => onOpenEntity(g.entityId)}>
                  {g.name}
                </button>
                <span className="entity-rel">{g.relationship}</span>
                {g.count > 1 && <span className="entity-conn-count">·&nbsp;{g.count}&nbsp;DOCS</span>}
                {g.descriptions.length > 0 && <div className="entity-conn-desc">"{g.descriptions[0]}"</div>}
                {g.descriptions.length > 1 && (
                  <>
                    <button
                      type="button"
                      className="entity-conn-more"
                      onClick={() => setOpenConnKey(openConnKey === g.key ? null : g.key)}
                    >
                      {openConnKey === g.key ? 'Hide sources' : `${g.descriptions.length - 1} more source${g.descriptions.length === 2 ? '' : 's'}`}
                    </button>
                    {openConnKey === g.key && g.descriptions.slice(1).map((d, i) => (
                      <div key={i} className="entity-conn-desc">"{d}"</div>
                    ))}
                  </>
                )}
              </div>
            ))}
            {currentConnections.cooccurrence.map((c, i) => (
              <div key={`c${i}`} className="entity-conn">
                <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }} onClick={() => onOpenEntity(c.entity_id)}>
                  {entityDisplayName(c.canonical_name)}
                </button>
                <span className="entity-rel">together in {c.shared_doc_count} docs</span>
              </div>
            ))}
          </div>
        )}

        {currentMentions && currentMentions.documents.length > 0 && (
          <div>
            <div className="panel-header" style={{ padding: 0, background: 'none', border: 'none' }}>
              Mentions ({currentMentions.total} documents)
            </div>
            {currentMentions.documents.map(d => (
              <div key={d.document_id} style={{ margin: '10px 0' }}>
                <button className="btn btn-ghost btn-xs entity-bates" onClick={() => onOpenDocument(d.document_id, entityId)}>
                  {d.bates_begin}{d.title ? ` — ${d.title}` : ''}
                </button>
                {d.mentions.slice(0, 3).map((m, i) => (
                  <div key={i} className="entity-snippet">
                    <Snippet
                      text={m.context_snippet || m.surface_text}
                      name={m.surface_text}
                    />
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

import { useEffect, useState } from 'react';
import { getLeaderboard } from '../api/client';

interface Props {
  productionId: number;
}

type Tab = 'views' | 'activity';

export default function Leaderboard({ productionId }: Props) {
  const [tab, setTab] = useState<Tab>('views');
  const [views, setViews] = useState<{ user_id: string; email: string; count: number }[]>([]);
  const [activity, setActivity] = useState<{ user_id: string; email: string; display_name: string | null; notes: number; tags: number; total: number }[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    getLeaderboard(productionId)
      .then(data => { setViews(data.views); setActivity(data.activity); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [productionId]);

  if (loading) return null;
  if (views.length === 0 && activity.length === 0) return null;

  const maxViews = views.length > 0 ? views[0].count : 1;
  const maxActivity = activity.length > 0 ? activity[0].total : 1;

  return (
    <div style={{ marginTop: 'var(--space-6)', marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginBottom: 'var(--space-3)' }}>
        <span style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'rgba(44,62,107,0.5)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Leaderboard
        </span>
        <div style={{ display: 'flex', gap: 2, background: 'rgba(44,62,107,0.05)', borderRadius: 'var(--radius-md)', padding: 2 }}>
          {(['views', 'activity'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              style={{
                padding: '3px 10px', borderRadius: 'var(--radius-sm)', border: 'none', cursor: 'pointer',
                background: tab === t ? 'var(--color-card)' : 'transparent',
                color: tab === t ? 'var(--color-ink)' : 'rgba(44,62,107,0.4)',
                fontWeight: tab === t ? 600 : 400,
                fontSize: 11, boxShadow: tab === t ? 'var(--shadow-xs)' : 'none',
              }}
            >
              {t === 'views' ? 'Pages Viewed' : 'Notes & Tags'}
            </button>
          ))}
        </div>
      </div>

      <div className="card" style={{ overflow: 'hidden' }}>
        {tab === 'views' && (
          <div style={{ padding: 'var(--space-2)' }}>
            {views.length === 0 && (
              <div style={{ padding: 'var(--space-4)', textAlign: 'center', color: 'rgba(44,62,107,0.3)', fontSize: 'var(--text-xs)' }}>No views yet</div>
            )}
            {views.map((v, i) => (
              <div key={v.user_id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: '6px var(--space-2)' }}>
                <span style={{ width: 20, fontSize: 11, fontWeight: 700, color: i === 0 ? 'var(--color-ink)' : 'rgba(44,62,107,0.35)', textAlign: 'center' }}>
                  {i + 1}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {v.email}
                  </div>
                  <div style={{ height: 4, background: 'rgba(44,62,107,0.06)', borderRadius: 2, marginTop: 3 }}>
                    <div style={{ height: '100%', width: `${(v.count / maxViews) * 100}%`, background: 'var(--color-ink)', borderRadius: 2, transition: 'width 0.3s' }} />
                  </div>
                </div>
                <span style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'rgba(44,62,107,0.5)', flexShrink: 0 }}>
                  {v.count.toLocaleString()}
                </span>
              </div>
            ))}
          </div>
        )}

        {tab === 'activity' && (
          <div style={{ padding: 'var(--space-2)' }}>
            {activity.length === 0 && (
              <div style={{ padding: 'var(--space-4)', textAlign: 'center', color: 'rgba(44,62,107,0.3)', fontSize: 'var(--text-xs)' }}>No activity yet</div>
            )}
            {activity.map((a, i) => (
              <div key={a.user_id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', padding: '6px var(--space-2)' }}>
                <span style={{ width: 20, fontSize: 11, fontWeight: 700, color: i === 0 ? 'var(--color-ink)' : 'rgba(44,62,107,0.35)', textAlign: 'center' }}>
                  {i + 1}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {a.display_name || a.email}
                  </div>
                  <div style={{ height: 4, background: 'rgba(44,62,107,0.06)', borderRadius: 2, marginTop: 3 }}>
                    <div style={{ height: '100%', width: `${(a.total / maxActivity) * 100}%`, background: 'var(--color-ink)', borderRadius: 2, transition: 'width 0.3s' }} />
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 'var(--space-2)', flexShrink: 0 }}>
                  <span style={{ fontSize: 10, color: 'rgba(44,62,107,0.4)' }}>{a.notes} notes</span>
                  <span style={{ fontSize: 10, color: 'rgba(44,62,107,0.4)' }}>{a.tags} tags</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

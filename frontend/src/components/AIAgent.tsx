import { useEffect, useMemo, useRef, useState } from 'react';
import Markdown, { defaultUrlTransform, type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { streamChat, type ChatMessage } from '../api/client';
import { showToast } from './Toast';

export interface AttachedDoc {
  id: string;
  label: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  attachedDocs: AttachedDoc[];
  onRemoveDoc: (id: string) => void;
  onOpenDocument?: (docId: string) => void;
}

const DEFAULT_SIZE = { w: 420, h: 620 };

function transcriptText(messages: ChatMessage[]): string {
  return messages
    .map(m => `${m.role === 'user' ? 'You' : 'AI Agent'}:\n${m.content}`)
    .join('\n\n────────────────────\n\n');
}

export default function AIAgent({ open, onClose, attachedDocs, onRemoveDoc, onOpenDocument }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [activity, setActivity] = useState<{ summary: string; ok?: boolean; resultSummary?: string }[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);

  // Render assistant messages as markdown. Document citations arrive as links
  // with a `doc:<id>` href — render those as in-app links that open the viewer.
  const mdComponents: Components = useMemo(() => ({
    a({ href, children, node: _node, ...props }) {
      if (href && href.startsWith('doc:')) {
        const id = href.slice(4);
        return (
          <a
            className="ai-agent-doc-link"
            href={`#doc-${id}`}
            onClick={(e) => { e.preventDefault(); onOpenDocument?.(id); }}
          >
            {children}
          </a>
        );
      }
      return <a href={href} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>;
    },
  }), [onOpenDocument]);

  const renderMarkdown = (text: string) => (
    <Markdown
      remarkPlugins={[remarkGfm]}
      components={mdComponents}
      urlTransform={(url) => (url.startsWith('doc:') ? url : defaultUrlTransform(url))}
    >
      {text}
    </Markdown>
  );
  const [size, setSize] = useState<{ w: number; h: number }>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('vigilist.aiAgent.size') || '');
      if (saved && typeof saved.w === 'number' && typeof saved.h === 'number') return saved;
    } catch { /* ignore */ }
    return DEFAULT_SIZE;
  });

  // Drag the top-left handle to resize (panel is anchored bottom-right, so it grows up/left).
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const startW = size.w;
    const startH = size.h;
    const onMove = (ev: MouseEvent) => {
      const w = Math.min(Math.max(340, startW + (startX - ev.clientX)), window.innerWidth - 32);
      const h = Math.min(Math.max(360, startH + (startY - ev.clientY)), window.innerHeight - 32);
      setSize({ w, h });
    };
    const cleanup = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      dragCleanupRef.current = null;
    };
    const onUp = () => {
      cleanup();
      setSize(curr => { localStorage.setItem('vigilist.aiAgent.size', JSON.stringify(curr)); return curr; });
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    dragCleanupRef.current = cleanup;
  };

  // Clean up any in-progress resize drag listeners if the panel unmounts mid-drag.
  useEffect(() => () => { dragCleanupRef.current?.(); }, []);

  // Keep the transcript scrolled to the latest message as it grows/streams.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, streamingText, open]);

  // Focus the composer when the panel opens.
  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 50);
  }, [open]);

  // Close on Escape only when focus is within the panel (it's non-modal now).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && panelRef.current?.contains(document.activeElement)) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const send = async () => {
    const text = input.trim();
    if (!text || streaming) return;

    const nextMessages: ChatMessage[] = [...messages, { role: 'user', content: text }];
    setMessages(nextMessages);
    setInput('');
    setStreaming(true);
    setStreamingText('');
    setActivity([]);

    const controller = new AbortController();
    abortRef.current = controller;
    let acc = '';
    let errored = false;

    await streamChat(
      nextMessages,
      attachedDocs.map(d => d.id),
      {
        onDelta: (delta) => { acc += delta; setStreamingText(acc); },
        onError: (message) => { errored = true; showToast(message, 'error'); },
        onToolUse: (evt) => setActivity(prev => [...prev, { summary: evt.summary }]),
        onToolResult: (evt) => setActivity(prev => {
          const next = [...prev];
          for (let i = next.length - 1; i >= 0; i--) {
            if (next[i].ok === undefined) { next[i] = { ...next[i], ok: evt.ok, resultSummary: evt.summary }; break; }
          }
          return next;
        }),
      },
      controller.signal,
    );

    abortRef.current = null;
    setStreaming(false);
    setStreamingText('');
    setActivity([]);
    if (acc) {
      setMessages([...nextMessages, { role: 'assistant', content: acc }]);
    } else if (!errored) {
      showToast('The AI agent returned an empty response.', 'error');
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    // Keep whatever text streamed so far as the assistant turn.
    if (streamingText) {
      setMessages(prev => [...prev, { role: 'assistant', content: streamingText }]);
    }
    setStreaming(false);
    setStreamingText('');
    setActivity([]);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const copyTranscript = async () => {
    if (messages.length === 0) return;
    try {
      await navigator.clipboard.writeText(transcriptText(messages));
      showToast('Conversation copied to clipboard', 'success');
    } catch {
      showToast('Could not copy to clipboard', 'error');
    }
  };

  const downloadTranscript = () => {
    if (messages.length === 0) return;
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const blob = new Blob([transcriptText(messages)], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vigilist-ai-chat-${stamp}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const clearConversation = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setMessages([]);
    setStreamingText('');
    setStreaming(false);
    setActivity([]);
  };

  if (!open) return null;

  const hasConversation = messages.length > 0 || streaming;

  return (
    <div
      className="ai-agent-panel"
      role="dialog"
      aria-label="AI Agent"
      style={{ width: size.w, height: size.h }}
      onMouseDown={() => panelRef.current?.focus()}
      ref={panelRef}
      tabIndex={-1}
    >
      <div className="ai-agent-resize-handle" onMouseDown={startResize} aria-hidden="true" />
      <div className="ai-agent-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          <span className="ai-indicator" style={{ fontSize: 10, padding: '1px 6px' }}>AI</span>
          <h2 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>AI Agent</h2>
        </div>
        <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
          <button className="btn btn-ghost btn-sm" onClick={copyTranscript} disabled={messages.length === 0} title="Copy conversation">
            Copy
          </button>
          <button className="btn btn-ghost btn-sm" onClick={downloadTranscript} disabled={messages.length === 0} title="Download conversation">
            Download
          </button>
          <button className="btn btn-ghost btn-sm" onClick={clearConversation} disabled={!hasConversation} title="Clear conversation">
            Clear
          </button>
          <button className="modal-close-btn" aria-label="Close" onClick={onClose}>&times;</button>
        </div>
      </div>

      {attachedDocs.length > 0 && (
        <div className="ai-agent-docs">
          <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', marginRight: 4 }}>
            Context:
          </span>
          {attachedDocs.map(d => (
            <span key={d.id} className="ai-agent-doc-chip">
              {d.label}
              <button
                aria-label={`Remove ${d.label}`}
                onClick={() => onRemoveDoc(d.id)}
                title="Remove from context"
              >
                &times;
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="ai-agent-body" ref={scrollRef}>
        {messages.length === 0 && !streaming && (
          <div className="ai-agent-empty">
            <div style={{ fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)', color: 'var(--color-neutral-500)', marginBottom: 'var(--space-2)' }}>
              Ask the AI agent
            </div>
            <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-400)', maxWidth: 380 }}>
              {attachedDocs.length > 0
                ? `${attachedDocs.length} document${attachedDocs.length === 1 ? '' : 's'} attached. Ask a question, request a summary, or look for connections across them.`
                : 'Select documents and use "Send to AI Agent", or just ask a question about your review workflow.'}
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`ai-agent-msg ai-agent-msg-${m.role}`}>
            <div className="ai-agent-msg-role">{m.role === 'user' ? 'You' : 'AI Agent'}</div>
            <div className="ai-agent-msg-content ai-agent-markdown">
              {m.role === 'assistant' ? renderMarkdown(m.content) : m.content}
            </div>
          </div>
        ))}

        {streaming && (
          <div className="ai-agent-msg ai-agent-msg-assistant">
            <div className="ai-agent-msg-role">AI Agent</div>
            {activity.length > 0 && (
              <div className="ai-agent-activity">
                {activity.map((a, i) => (
                  <div key={i} className={`ai-agent-activity-row${a.ok === false ? ' is-error' : ''}`}>
                    <span className="ai-agent-activity-icon">{a.ok === undefined ? '⋯' : a.ok ? '✓' : '✕'}</span>
                    {a.resultSummary || a.summary}
                  </div>
                ))}
              </div>
            )}
            <div className="ai-agent-msg-content ai-agent-markdown">
              {streamingText ? renderMarkdown(streamingText) : <span className="ai-agent-typing"><span /><span /><span /></span>}
            </div>
          </div>
        )}
      </div>

      <div className="ai-agent-composer">
        <textarea
          ref={inputRef}
          className="ai-agent-input"
          placeholder="Ask a question…  (Enter to send, Shift+Enter for newline)"
          value={input}
          rows={1}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={streaming}
        />
        {streaming ? (
          <button className="btn btn-secondary" onClick={stop}>Stop</button>
        ) : (
          <button className="btn btn-primary" onClick={send} disabled={!input.trim()}>Send</button>
        )}
      </div>
    </div>
  );
}

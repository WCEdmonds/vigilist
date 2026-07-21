import { useEffect, useRef } from 'react';
import { getByBates } from '../api/client';
import type { ChatState } from '../hooks/useChat';
import { showToast } from './Toast';
import { renderChatMarkdown } from '../utils/chatMarkdown';

interface Props {
  chat: ChatState;
  placeholder: string;
  autoFocusToken: number;
  /** Opens a document cited by the AI ([BATES](doc:…) links in replies). */
  onOpenDocument?: (id: string) => void;
  /** Scopes Bates-citation lookups to the current production. */
  productionId?: number;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const SAMPLE_QUESTIONS = [
  'What is this production about, and what stands out?',
  'Build a timeline of the key events with Bates citations.',
  'Which documents most need attorney attention, and why?',
];

export default function ChatPanel({ chat, placeholder, autoFocusToken, onOpenDocument, productionId }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat.messages, chat.streamingText]);

  useEffect(() => {
    if (autoFocusToken > 0) inputRef.current?.focus();
  }, [autoFocusToken]);

  const submit = () => {
    const el = inputRef.current;
    if (!el) return;
    const value = el.value;
    if (!value.trim() || chat.streaming) return;
    chat.send(value);
    el.value = '';
  };

  const copyTranscript = async () => {
    try {
      await navigator.clipboard.writeText(chat.transcriptText());
      showToast('Transcript copied', 'success');
    } catch {
      showToast('Copy failed', 'error');
    }
  };

  // Doc citations render as .chat-doc-link buttons deep inside markdown
  // blocks — one delegated handler beats threading a callback through the
  // (pure) renderer.
  const handleBodyClick = async (e: React.MouseEvent) => {
    const link = (e.target as HTMLElement).closest<HTMLElement>('.chat-doc-link');
    if (!link || !onOpenDocument) return;
    const target = link.dataset.docTarget || '';
    try {
      if (UUID_RE.test(target)) {
        onOpenDocument(target);
      } else {
        const found = await getByBates(target, productionId);
        onOpenDocument(found.id);
      }
    } catch {
      showToast(`Could not find ${target} in this production`, 'error');
    }
  };

  const download = () => {
    const blob = new Blob([chat.transcriptText()], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `vigilist-ai-chat-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div className="chat-panel">
      {chat.messages.length > 0 && (
        <div className="chat-actions">
          <button type="button" className="btn btn-ghost btn-xs" onClick={copyTranscript}>Copy</button>
          <button type="button" className="btn btn-ghost btn-xs" onClick={download}>Download</button>
          <button type="button" className="btn btn-ghost btn-xs" onClick={chat.clear}>Clear</button>
        </div>
      )}

      {chat.attachedDocs.length > 0 && (
        <div className="ai-agent-docs chat-docs">
          <span className="chat-docs-label">Context:</span>
          {chat.attachedDocs.map(d => (
            <span key={d.id} className="ai-agent-doc-chip">
              {d.label}
              <button type="button" onClick={() => chat.removeDoc(d.id)} aria-label={`Remove ${d.label}`}>×</button>
            </span>
          ))}
        </div>
      )}

      <div className="chat-body" ref={scrollRef} onClick={handleBodyClick}>
        {chat.messages.length === 0 && !chat.streaming && (
          <div className="chat-empty">
            <div><span className="brief-ai-mark">✦</span> {placeholder}</div>
            <div className="chat-suggestions">
              {SAMPLE_QUESTIONS.map(q => (
                <button key={q} type="button" className="chat-suggestion" onClick={() => chat.send(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {chat.messages.map((m, i) => (
          <div key={i} className={`ai-agent-msg ai-agent-msg-${m.role === 'user' ? 'user' : 'assistant'}`}>
            <div className="ai-agent-msg-role">{m.role === 'user' ? 'You' : '✦ AI'}</div>
            <div className="ai-agent-msg-content">
              {m.role === 'user' ? m.content : renderChatMarkdown(m.content)}
            </div>
          </div>
        ))}
        {chat.streaming && (
          <div className="ai-agent-msg ai-agent-msg-assistant">
            <div className="ai-agent-msg-role">✦ AI</div>
            {chat.activity.length > 0 && (
              <div className="ai-agent-activity">
                {chat.activity.map((a, i) => (
                  <div key={i} className={`ai-agent-activity-row${a.ok === false ? ' is-error' : ''}`}>
                    <span className="ai-agent-activity-icon">{a.ok === undefined ? '⋯' : a.ok ? '✓' : '✕'}</span>
                    {a.summary}
                  </div>
                ))}
              </div>
            )}
            <div className="ai-agent-msg-content">
              {chat.streamingText ? renderChatMarkdown(chat.streamingText) : (
                <span className="ai-agent-typing"><span /><span /><span /></span>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="chat-composer">
        <textarea
          ref={inputRef}
          className="chat-input"
          rows={2}
          placeholder={placeholder}
          aria-label="Ask the AI"
          disabled={chat.streaming}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
          }}
        />
        {chat.streaming ? (
          <button type="button" className="btn btn-secondary btn-sm" onClick={chat.stop}>Stop</button>
        ) : (
          <button type="button" className="btn btn-primary btn-sm" disabled={chat.streaming} onClick={submit}>Send</button>
        )}
      </div>
    </div>
  );
}

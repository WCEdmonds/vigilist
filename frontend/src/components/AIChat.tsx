import { useEffect, useRef, useState } from 'react';
import { streamChat, type ChatMessage } from '../api/client';
import { showToast } from './Toast';

interface AIChatProps {
  open: boolean;
  onClose: () => void;
  /** Document ids the conversation is grounded in. */
  docIds: string[];
}

/**
 * Floating AI assistant. Users select documents in the list, click
 * "Send to AI Agent", and ask questions grounded in those documents.
 * Conversation is session-only (resets on reload) but can be copied or
 * downloaded as a transcript.
 */
export default function AIChat({ open, onClose, docIds }: AIChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, streaming]);

  // Cancel any in-flight stream when the panel closes.
  useEffect(() => {
    if (!open && abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
      setStreaming(false);
    }
  }, [open]);

  const send = async () => {
    const text = input.trim();
    if (!text || streaming) return;
    if (docIds.length === 0) {
      showToast('Select one or more documents first, then click "Send to AI Agent".', 'error');
      return;
    }

    const history: ChatMessage[] = [...messages, { role: 'user', content: text }];
    setMessages([...history, { role: 'assistant', content: '' }]);
    setInput('');
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamChat(docIds, history, (chunk) => {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, content: last.content + chunk };
          }
          return next;
        });
      }, controller.signal);
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant' && !last.content) {
            next[next.length - 1] = { ...last, content: `Error: ${e?.message || 'chat failed'}` };
          }
          return next;
        });
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const transcript = () =>
    messages
      .filter((m) => m.content)
      .map((m) => `${m.role === 'user' ? 'You' : 'AI'}: ${m.content}`)
      .join('\n\n');

  const copyTranscript = async () => {
    try {
      await navigator.clipboard.writeText(transcript());
      showToast('Transcript copied to clipboard', 'success');
    } catch {
      showToast('Could not copy transcript', 'error');
    }
  };

  const downloadTranscript = () => {
    const blob = new Blob([transcript()], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'vigilist_ai_chat.txt';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  if (!open) return null;

  return (
    <div
      style={{
        position: 'fixed', bottom: 88, right: 24, zIndex: 1000,
        width: 'min(560px, calc(100vw - 32px))', height: 'min(680px, calc(100vh - 120px))',
        display: 'flex', flexDirection: 'column',
        background: 'white', borderRadius: 'var(--radius-lg, 12px)',
        boxShadow: '0 12px 40px rgba(44,62,107,0.28)', border: '1px solid var(--color-neutral-200, #e5e7eb)',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 16px', borderBottom: '1px solid var(--color-neutral-100, #f0f0f0)', background: 'var(--color-neutral-50, #fafafa)' }}>
        <span className="ai-indicator" style={{ padding: '0 5px', fontSize: 10 }}>AI</span>
        <strong style={{ fontSize: 'var(--text-sm)', color: 'var(--color-ink)' }}>Document Assistant</strong>
        <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)' }}>
          {docIds.length} doc{docIds.length === 1 ? '' : 's'} attached
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          {messages.some((m) => m.content) && (
            <>
              <button className="btn btn-ghost btn-xs" onClick={copyTranscript} title="Copy transcript">Copy</button>
              <button className="btn btn-ghost btn-xs" onClick={downloadTranscript} title="Download transcript">Download</button>
              <button className="btn btn-ghost btn-xs" onClick={() => setMessages([])} title="Clear conversation">Clear</button>
            </>
          )}
          <button className="btn btn-ghost btn-xs" onClick={onClose} title="Close">✕</button>
        </div>
      </div>

      {/* Messages */}
      <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        {messages.length === 0 && (
          <div style={{ color: 'var(--color-neutral-400)', fontSize: 'var(--text-sm)', margin: 'auto', textAlign: 'center', maxWidth: 360 }}>
            {docIds.length === 0
              ? 'Select documents in the list and click "Send to AI Agent", then ask questions here.'
              : 'Ask a question about the attached documents — summaries, key parties, dates, whether a topic appears, and more. Answers cite Bates numbers.'}
          </div>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            style={{
              alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: '85%',
              padding: '9px 12px',
              borderRadius: 10,
              fontSize: 'var(--text-sm)',
              lineHeight: 1.5,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              background: m.role === 'user' ? 'var(--color-ink, #2c3e6b)' : 'var(--color-neutral-100, #f0f2f7)',
              color: m.role === 'user' ? 'white' : 'var(--color-ink, #2c3e6b)',
            }}
          >
            {m.content || (streaming && i === messages.length - 1 ? <span className="spinner spinner-sm" /> : '')}
          </div>
        ))}
      </div>

      {/* Input */}
      <div style={{ borderTop: '1px solid var(--color-neutral-100, #f0f0f0)', padding: 12, display: 'flex', gap: 8, alignItems: 'flex-end' }}>
        <textarea
          className="input"
          placeholder={docIds.length === 0 ? 'Attach documents first…' : 'Ask about the attached documents…'}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={2}
          disabled={docIds.length === 0}
          style={{ flex: 1, resize: 'none', fontFamily: 'inherit' }}
        />
        {streaming ? (
          <button className="btn btn-secondary btn-sm" onClick={() => abortRef.current?.abort()}>Stop</button>
        ) : (
          <button className="btn btn-primary btn-sm" onClick={send} disabled={!input.trim() || docIds.length === 0}>Send</button>
        )}
      </div>
    </div>
  );
}

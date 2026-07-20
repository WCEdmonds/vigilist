import { useCallback, useMemo, useRef, useState } from 'react';
import { streamChat, type ChatMessage } from '../api/client';
import type { AttachedDoc } from '../types';
import { showToast } from '../components/Toast';

export interface ChatState {
  messages: ChatMessage[];
  streaming: boolean;
  streamingText: string;
  attachedDocs: AttachedDoc[];
  attachDocs: (docs: AttachedDoc[]) => void;
  removeDoc: (id: string) => void;
  send: (text: string) => void;
  stop: () => void;
  clear: () => void;
  transcriptText: () => string;
}

function formatTranscript(messages: ChatMessage[]): string {
  return messages
    .map(m => `${m.role === 'user' ? 'You' : 'AI Agent'}:\n${m.content}`)
    .join('\n\n────────────────────\n\n');
}

/**
 * The AI chat state machine, extracted from the retired AIAgent overlay so
 * the context rail and the omnibox can share one conversation. Owned by Home:
 * the conversation lives as long as the production view does.
 */
export function useChat(): ChatState {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [attachedDocs, setAttachedDocs] = useState<AttachedDoc[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  // Mirrors the accumulated streamed text so `stop()` can commit synchronously
  // without waiting on the aborted fetch to settle — the same synchronous-commit
  // behavior the retired AIAgent overlay relied on.
  const accRef = useRef('');

  const attachDocs = useCallback((docs: AttachedDoc[]) => {
    setAttachedDocs(prev => {
      const seen = new Set(prev.map(d => d.id));
      return [...prev, ...docs.filter(d => !seen.has(d.id))];
    });
  }, []);

  const removeDoc = useCallback((id: string) => {
    setAttachedDocs(prev => prev.filter(d => d.id !== id));
  }, []);

  const send = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed || streaming) return;

    const nextMessages: ChatMessage[] = [...messages, { role: 'user', content: trimmed }];
    setMessages(nextMessages);
    setStreaming(true);
    setStreamingText('');

    const controller = new AbortController();
    abortRef.current = controller;
    accRef.current = '';
    let errored = false;

    streamChat(
      nextMessages,
      attachedDocs.map(d => d.id),
      {
        onDelta: delta => { accRef.current += delta; setStreamingText(accRef.current); },
        onError: message => { errored = true; showToast(message, 'error'); },
      },
      controller.signal,
    ).then(() => {
      if (controller.signal.aborted) { setStreaming(false); setStreamingText(''); return; }
      abortRef.current = null;
      setStreaming(false);
      setStreamingText('');
      if (accRef.current) {
        setMessages([...nextMessages, { role: 'assistant', content: accRef.current }]);
      } else if (!errored) {
        showToast('The AI agent returned an empty response.', 'error');
      }
      accRef.current = '';
    }).catch(() => {
      // getIdToken() (client.ts, pre-stream) can reject outright — e.g. offline —
      // which streamChat doesn't catch. If stop() already ran, it committed the
      // partial text and reset state synchronously; mirror the .then() short
      // circuit so we don't double-handle or toast on top of a user-initiated stop.
      if (controller.signal.aborted) { setStreaming(false); setStreamingText(''); return; }
      abortRef.current = null;
      accRef.current = '';
      setStreaming(false);
      setStreamingText('');
      showToast('Chat request failed — check your connection.', 'error');
    });
  }, [messages, attachedDocs, streaming]);

  const stop = useCallback(() => {
    if (!abortRef.current) return;
    abortRef.current.abort();
    abortRef.current = null;
    // Keep whatever text streamed so far as the assistant turn, mirroring how
    // the retired AIAgent overlay handled a user-initiated stop.
    if (accRef.current) {
      setMessages(prev => [...prev, { role: 'assistant', content: accRef.current }]);
    }
    accRef.current = '';
    setStreaming(false);
    setStreamingText('');
  }, []);

  const clear = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    accRef.current = '';
    setMessages([]);
    setStreaming(false);
    setStreamingText('');
    setAttachedDocs([]);
  }, []);

  const transcriptText = useCallback(() => formatTranscript(messages), [messages]);

  return useMemo(
    () => ({ messages, streaming, streamingText, attachedDocs, attachDocs, removeDoc, send, stop, clear, transcriptText }),
    [messages, streaming, streamingText, attachedDocs, attachDocs, removeDoc, send, stop, clear, transcriptText],
  );
}

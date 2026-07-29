import { useCallback, useMemo, useRef, useState } from 'react';
import { getConversation, streamChat, type ChatMessage } from '../api/client';
import type { AttachedDoc } from '../types';
import { showToast } from '../components/Toast';

export interface ChatActivity {
  summary: string;
  ok?: boolean;
}

export interface ChatState {
  messages: ChatMessage[];
  streaming: boolean;
  streamingText: string;
  /** Tool calls made during the current/most recent turn ("Searching…"). */
  activity: ChatActivity[];
  attachedDocs: AttachedDoc[];
  attachDocs: (docs: AttachedDoc[]) => void;
  removeDoc: (id: string) => void;
  send: (text: string) => void;
  stop: () => void;
  clear: () => void;
  transcriptText: () => string;
  /** Saved thread this conversation belongs to; null until the first turn
   *  is persisted, or after `clear()` starts a fresh one. */
  conversationId: string | null;
  loadConversation: (id: string) => Promise<void>;
  loadingConversation: boolean;
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
export function useChat(productionId?: number): ChatState {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [activity, setActivity] = useState<ChatActivity[]>([]);
  const [attachedDocs, setAttachedDocs] = useState<AttachedDoc[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  // Mirrors the accumulated streamed text so `stop()` can commit synchronously
  // without waiting on the aborted fetch to settle — the same synchronous-commit
  // behavior the retired AIAgent overlay relied on.
  const accRef = useRef('');
  // Mirrors conversationId so a second send fired before React re-renders
  // still carries the thread id, instead of silently opening a duplicate
  // conversation server-side.
  const convoRef = useRef<string | null>(null);

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
    setActivity([]);

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
        onToolUse: evt => setActivity(prev => [...prev, { summary: evt.summary }]),
        onToolResult: evt => setActivity(prev => {
          // Mark the oldest still-pending activity row settled.
          const next = [...prev];
          for (let i = 0; i < next.length; i++) {
            if (next[i].ok === undefined) { next[i] = { ...next[i], ok: evt.ok }; break; }
          }
          return next;
        }),
        onConversation: id => { convoRef.current = id; setConversationId(id); },
      },
      controller.signal,
      productionId,
      convoRef.current ?? undefined,
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
  }, [messages, attachedDocs, streaming, productionId]);

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
    // Detach from the saved thread — clearing starts a new conversation
    // rather than wiping the one already on the server.
    convoRef.current = null;
    setConversationId(null);
    setMessages([]);
    setStreaming(false);
    setStreamingText('');
    setActivity([]);
    setAttachedDocs([]);
  }, []);

  /** Replace the live conversation with a saved one. */
  const loadConversation = useCallback(async (id: string) => {
    abortRef.current?.abort();
    abortRef.current = null;
    accRef.current = '';
    setLoadingConversation(true);
    try {
      const convo = await getConversation(id);
      convoRef.current = convo.id;
      setConversationId(convo.id);
      setMessages(convo.messages.map(m => ({ role: m.role, content: m.content })));
      setStreaming(false);
      setStreamingText('');
      setActivity([]);
      // Attachments are recorded per turn for provenance, but re-attaching
      // them would silently re-ground the next question in documents the user
      // didn't pick this session. Start clean.
      setAttachedDocs([]);
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Could not open that conversation', 'error');
    } finally {
      setLoadingConversation(false);
    }
  }, []);

  const transcriptText = useCallback(() => formatTranscript(messages), [messages]);

  return useMemo(
    () => ({ messages, streaming, streamingText, activity, attachedDocs, attachDocs, removeDoc, send, stop, clear, transcriptText, conversationId, loadConversation, loadingConversation }),
    [messages, streaming, streamingText, activity, attachedDocs, attachDocs, removeDoc, send, stop, clear, transcriptText, conversationId, loadConversation, loadingConversation],
  );
}

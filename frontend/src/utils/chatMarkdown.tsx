import type { ReactNode } from 'react';

/* Minimal markdown renderer for AI chat replies. The chat model writes
 * GitHub-flavored prose — headings, bold, lists, blockquotes, inline code,
 * rules — and rendering it as plain text leaves `**` and `##` litter in the
 * transcript. This covers that subset deterministically with React elements
 * (no HTML injection). Anything unrecognized falls through as plain text. */

const INLINE_PATTERN = /(\[[^\]]+\]\(doc:[^)]+\)|\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g;

const DOC_LINK = /^\[([^\]]+)\]\(doc:([^)]+)\)$/;
const ANY_LINK = /^\[([^\]]+)\]\([^)]+\)$/;

function renderInline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(INLINE_PATTERN)) {
    const idx = match.index ?? 0;
    if (idx > last) out.push(text.slice(last, idx));
    const token = match[0];
    const key = `${keyBase}-i${i++}`;
    const docLink = DOC_LINK.exec(token);
    const anyLink = docLink ? null : ANY_LINK.exec(token);
    if (docLink) {
      // The chat model cites documents as [BATES](doc:BATES). Rendered as a
      // button; ChatPanel opens the document via click delegation.
      out.push(
        <button key={key} type="button" className="chat-doc-link" data-doc-target={docLink[2].trim()}>
          {docLink[1]}
        </button>,
      );
    } else if (anyLink) {
      // Non-doc links: show the label, drop the URL (chat never needs to
      // send users off-app).
      out.push(anyLink[1]);
    } else if (token.startsWith('**')) {
      // Recurse: models routinely put doc citations inside bold runs
      // (**[BATES](doc:…) — description**); without recursion the link
      // renders as raw markup.
      out.push(<strong key={key}>{renderInline(token.slice(2, -2), key)}</strong>);
    } else if (token.startsWith('`')) {
      out.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else {
      out.push(<em key={key}>{renderInline(token.slice(1, -1), key)}</em>);
    }
    last = idx + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const BULLET = /^\s*[-*]\s+/;
const ORDERED = /^\s*\d+[.)]\s+/;

export function renderChatMarkdown(text: string): ReactNode[] {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let quote: string[] = [];
  let key = 0;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const k = `p${key++}`;
    blocks.push(<p key={k}>{renderInline(paragraph.join(' '), k)}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const k = `l${key++}`;
    const items = list.items.map((item, n) => (
      <li key={`${k}-${n}`}>{renderInline(item, `${k}-${n}`)}</li>
    ));
    blocks.push(list.ordered ? <ol key={k}>{items}</ol> : <ul key={k}>{items}</ul>);
    list = null;
  };
  const flushQuote = () => {
    if (!quote.length) return;
    const k = `q${key++}`;
    blocks.push(<blockquote key={k}>{renderInline(quote.join(' '), k)}</blockquote>);
    quote = [];
  };
  const flushAll = () => { flushParagraph(); flushList(); flushQuote(); };

  for (const line of lines) {
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const k = `h${key++}`;
      blocks.push(<p key={k} className="chat-md-heading">{renderInline(heading[2], k)}</p>);
      continue;
    }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushAll();
      blocks.push(<hr key={`r${key++}`} />);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      flushParagraph(); flushList();
      quote.push(line.replace(/^\s*>\s?/, ''));
      continue;
    }
    if (BULLET.test(line) || ORDERED.test(line)) {
      flushParagraph(); flushQuote();
      const ordered = ORDERED.test(line);
      if (!list || list.ordered !== ordered) { flushList(); list = { ordered, items: [] }; }
      list.items.push(line.replace(ordered ? ORDERED : BULLET, ''));
      continue;
    }
    if (!line.trim()) {
      flushAll();
      continue;
    }
    // Continuation of the current list item or paragraph.
    if (list) list.items[list.items.length - 1] += ` ${line.trim()}`;
    else { flushQuote(); paragraph.push(line.trim()); }
  }
  flushAll();
  return blocks;
}

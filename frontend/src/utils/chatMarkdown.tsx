import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/* Markdown for AI chat replies.
 *
 * This was a hand-rolled parser covering headings, lists, quotes and inline
 * marks. It had no table support, so a GFM table fell through to the
 * paragraph branch and every row was joined with spaces onto a single line —
 * which is what the model's document tables looked like in the transcript.
 *
 * react-markdown + remark-gfm were already dependencies (EntityPanel renders
 * entity overviews with them), so chat now uses the same parser: tables,
 * nested lists, and everything else come free and there is one markdown
 * implementation in the app instead of two.
 *
 * The one thing the hand-rolled version did that a stock parser does not is
 * render document and entity citations as clickable buttons. That is kept
 * below via the `a` component override.
 */

/** Citations the chat model emits: [BATES](doc:BATES) and [Name](entity:uuid).
 *  ChatPanel opens them by click delegation on the data attributes. */
function CitationLink({ href, children }: { href?: string; children?: ReactNode }) {
  const target = href ?? '';

  if (target.startsWith('doc:')) {
    return (
      <button type="button" className="chat-doc-link" data-doc-target={target.slice(4).trim()}>
        {children}
      </button>
    );
  }
  if (target.startsWith('entity:')) {
    return (
      <button type="button" className="chat-entity-link" data-entity-target={target.slice(7).trim()}>
        {children}
      </button>
    );
  }
  // Any other link renders as its label with the URL dropped — chat never
  // needs to send a reviewer off-app, and emitting no href at all means no
  // sanitization question to get wrong.
  return <>{children}</>;
}

/** Render one assistant reply. A component rather than a helper returning
 *  JSX so fast refresh can track it. */
export function ChatMarkdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      // Identity transform so `doc:` and `entity:` survive to CitationLink;
      // react-markdown's default would strip unknown protocols. Safe because
      // CitationLink never renders an <a href>, only buttons or plain text.
      urlTransform={url => url}
      components={{
        a: CitationLink,
        // The rail is narrow and these tables are wide (Bates + description).
        // Scroll the table inside its own box rather than letting it stretch
        // the message and push the whole transcript sideways.
        table: ({ children }) => (
          <div className="chat-md-table-wrap"><table>{children}</table></div>
        ),
      }}
    >
      {text}
    </ReactMarkdown>
  );
}

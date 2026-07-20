import type { ReactNode } from 'react';

export interface Slide {
  id: string;
  /** Short heading shown above the body. */
  title: string;
  /** Emoji glyph — matches the existing WelcomePage feature icons. */
  icon: string;
  body: ReactNode;
  /** Only shown to users who own a production (or have none yet). */
  ownerOnly?: boolean;
}

export const SLIDES: Slide[] = [
  {
    id: 'welcome',
    title: 'Welcome to Vigilist',
    icon: '\u{1F4DA}',
    body: (
      <>
        <p>
          Vigilist is a document review platform for e-discovery productions. A{' '}
          <strong>production</strong> is one set of documents — everything you search,
          tag, and review lives inside one.
        </p>
        <p>
          This guide takes about a minute. Reopen it anytime from the{' '}
          <strong>⚙ menu → Guide</strong>.
        </p>
      </>
    ),
  },
  {
    id: 'search',
    title: 'Search, or just ask',
    icon: '\u{1F50D}',
    body: (
      <>
        <p>
          The search box in the top bar understands both <strong>full-text</strong> queries
          ("phrases", AND/OR/NOT, wildcard*) and plain questions. Type a question and the
          pill flips to <strong>✦ Ask</strong> — press <strong>✦ Ask AI</strong> to send
          it to the AI chat instead of searching. Narrow by file type and export results to CSV from the results header. Save searches from the search box's <strong>Saved</strong> menu.
        </p>
      </>
    ),
  },
  {
    id: 'brief',
    title: 'Your production, already read',
    icon: '✦',
    body: (
      <>
        <p>
          When a production is ingested, AI clusters it into <strong>themes</strong>,
          summarizes every document, and writes a <strong>Production Brief</strong> at the
          top of Home — who's involved, what it spans, what stands out. Click a theme
          chip to filter the list. If generation fails, owners can retry it from the card.
        </p>
      </>
    ),
  },
  {
    id: 'tagging',
    title: 'Tag and code in bulk',
    icon: '\u{1F3F7}',
    body: (
      <>
        <p>
          Select documents with the checkboxes and a bar appears at the bottom: tag them,
          download a ZIP, or clear the selection. Titles are inline-editable. Suggestions
          you accept in the <strong>Review</strong> workspace become ordinary tags — same colors, same filters.
        </p>
      </>
    ),
  },
  {
    id: 'viewer',
    title: 'Read, annotate, and connect',
    icon: '\u{1F4C4}',
    body: (
      <>
        <p>
          Open any document to page through it, drop pin annotations, write notes, and
          inspect metadata. <strong>✦ AI tools</strong> under the sidebar's Metadata tab
          summarize the document or find similar ones across the production.
        </p>
      </>
    ),
  },
  {
    id: 'rail',
    title: 'The Intelligence rail',
    icon: '\u{1F4AC}',
    body: (
      <>
        <p>
          The right-hand rail follows your work: with nothing selected, ask the production
          anything; select one document for its summary and quick actions; select several
          to ask about them together. Collapse it with the ▸ button — the ✦ tab brings it
          back. Your conversation stays until you switch productions.
        </p>
      </>
    ),
  },
  {
    id: 'review',
    title: 'Review, two lanes',
    icon: '\u{2705}',
    body: (
      <>
        <p>
          <strong>✦ Review</strong> in the top bar opens the workspace. The AI lane
          classifies documents against your case description — sort by confidence, agree
          or override (accepting writes a real tag), bulk-accept above a threshold, or cut
          a review queue from any slice. The human lane holds queues and batches for your
          team.
        </p>
      </>
    ),
  },
  {
    id: 'owner',
    title: 'Running a production',
    icon: '\u{2699}',
    ownerOnly: true,
    body: (
      <>
        <p>
          Everything administrative lives in the <strong>⚙ menu</strong>: ingest a new
          production, share access, production settings (your case description), the audit
          log, and this guide. <strong>Dashboard</strong> in the top bar tracks progress.
          When you ingest, describe the case — the AI uses it for the brief and
          classification, and you'll get a cost estimate before anything runs.
        </p>
      </>
    ),
  },
];

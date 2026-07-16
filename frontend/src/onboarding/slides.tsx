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
          This guide is a quick tour of what you can do. It takes about a minute.
          Once you are inside a production you can reopen it any time from the{' '}
          <strong>Guide</strong> button in the header.
        </p>
      </>
    ),
  },
  {
    id: 'search',
    title: 'Search that understands you',
    icon: '\u{1F50D}',
    body: (
      <>
        <p>
          Type keywords for a <strong>full-text</strong> search. Ask a question in plain
          English — or type anything long — and Vigilist switches to{' '}
          <strong>semantic</strong> search, which finds documents by meaning rather than
          exact wording.
        </p>
        <p>
          We pick the mode for you, but you are never stuck with it: every result set has
          a <strong>Try semantic</strong> / <strong>Try full-text</strong> toggle to run
          the same query the other way.
        </p>
        <p>
          Narrow results by file type — email, PDF, video, audio, Office — and export any
          result set to CSV.
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
          Tags carry a category — responsive, privilege, or your own custom ones. Create a
          tag on the fly whenever you need one.
        </p>
        <p>
          Tick the checkboxes on any rows and a bar appears at the bottom of the screen.
          From there you can tag, download the native files as a ZIP, or send the
          selection straight to the AI Agent.
        </p>
        <p>
          Filter the document list by tag, by file type, and sort by Bates number,
          recency, or size. Document titles are editable inline in the list; the
          Bates numbers are not.
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
          Open any document to page through it, draw <strong>annotations</strong> on the
          page, leave <strong>notes</strong> for your team, and inspect the extracted
          metadata.
        </p>
        <p>
          <strong>Find similar</strong> pulls up documents that resemble the one you are
          reading — useful for chasing a thread once you have found one good hit.
        </p>
      </>
    ),
  },
  {
    id: 'ai',
    title: 'AI that reads with you',
    icon: '\u{1F916}',
    body: (
      <>
        <p>
          The <strong>AI</strong> button in the bottom-right corner opens a chat panel.
          Attach documents to it — from the bulk bar, via{' '}
          <strong>Send to AI Agent</strong> — and ask questions about them.
        </p>
        <p>
          <strong>Smart Review</strong> has AI score documents for responsiveness before
          you read them, so the likely-relevant material rises to the top.
        </p>
        <p>
          <strong>Topic Groups</strong> and <strong>Corpus Analysis</strong> cluster the
          production by subject, which is a fast way to get the shape of a set you have
          never seen.
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
          <strong>+ Ingest</strong> loads a new production. <strong>Share</strong> invites
          colleagues — invite someone who has not signed up yet and their access resolves
          automatically on first login.
        </p>
        <p>
          <strong>Review Queues</strong> split the work into batches and hand them to
          reviewers. The <strong>Dashboard</strong> tracks progress across the team, and
          the <strong>Audit Log</strong> records who did what.
        </p>
      </>
    ),
  },
];

import { Fragment, type ReactNode } from 'react';

/**
 * Render a Postgres `ts_headline` snippet as React nodes, turning
 * `<mark>...</mark>` wrappers into real `<mark>` elements and leaving
 * every other character as plain text. Because we build React elements
 * (no raw HTML injection), any tag-looking text inside the document
 * body is rendered harmlessly as literal characters.
 */
export function renderHighlightedSnippet(input: string): ReactNode {
  if (!input) return null;
  const parts = input.split(/(<mark>.*?<\/mark>)/);
  return parts.map((part, i) => {
    const match = /^<mark>(.*?)<\/mark>$/.exec(part);
    if (match) {
      return <mark key={i}>{match[1]}</mark>;
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

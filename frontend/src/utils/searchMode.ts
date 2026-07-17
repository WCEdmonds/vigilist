export type SearchMode = 'fulltext' | 'semantic';

/**
 * Heuristic used everywhere a query's mode is auto-detected: long queries,
 * question words, or a question mark read as "asking the production"
 * (semantic); everything else is full-text.
 */
export function detectSearchMode(query: string): SearchMode {
  return query.length > 40
    || /\b(what|where|who|when|why|how|which|find|show|any|all)\b/i.test(query)
    || query.includes('?')
    ? 'semantic'
    : 'fulltext';
}

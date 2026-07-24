/** Display helpers for entity names (cast strip, doc-row chips). */

// Legal-process machinery is real in the corpus but never a "character" —
// courts, reporters, clerks, and party placeholders don't belong in
// player-facing entity strips.
const ENTITY_NOISE = [
  /^the court$/i,
  /court report(ing|er)/i,
  /\b(district|superior|circuit|supreme|county|municipal|appellate|bankruptcy) court\b/i,
  /^court of\b/i,
  /\b(court clerk|clerk of)\b/i,
  /^(plaintiffs?|defendants?|petitioners?|respondents?|appellants?|appellees?)$/i,
  /^the (state|people|government)$/i,
  /\bnotary\b/i,
];

export const isEntityNoise = (name: string) => ENTITY_NOISE.some(re => re.test(name));

/** Tame ALL-CAPS extraction artifacts; leave mixed-case names untouched. */
export function entityDisplayName(name: string): string {
  if (name.length <= 3 || name !== name.toUpperCase()) return name;
  return name
    .toLowerCase()
    .replace(/\b[a-z]/g, ch => ch.toUpperCase())
    .replace(/\b(Llc|Llp|Pllc|Pc|Pa|Inc|Ltd|Co|Ii|Iii|Iv|Usa|Us|Cv)\b/g, s => s.toUpperCase());
}

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

const NAME_SUFFIXES = new Set(['jr', 'sr', 'ii', 'iii', 'iv', 'esq']);
// A "family name" ending in one of these is a place/org wearing a comma
// ("Smith County, Texas"), not a person filed surname-first.
const NON_PERSON_ENDINGS = new Set([
  'county', 'city', 'court', 'school', 'district', 'department', 'office',
  'inc', 'llc', 'llp', 'pllc', 'pc', 'pa', 'co', 'corp', 'company', 'associates',
]);

/** "Schlegel, Matthew S" → "Matthew S. Schlegel". Conservative: bails on
 * anything that doesn't look like a surname-first person name. */
function reorderCommaName(name: string): string {
  const parts = name.split(',').map(p => p.trim()).filter(Boolean);
  if (parts.length < 2 || parts.length > 3) return name;
  let suffix = '';
  if (parts.length === 3) {
    if (!NAME_SUFFIXES.has(parts[2].replace(/\./g, '').toLowerCase())) return name;
    suffix = ` ${parts[2].replace(/\.$/, '')}`;
  }
  const [family, given] = parts;
  const familyTokens = family.split(/\s+/);
  const givenTokens = given.split(/\s+/);
  if (familyTokens.length > 2 || givenTokens.length > 3) return name;
  if (!givenTokens.every(t => /^[A-Za-z][A-Za-z'’.-]*$/.test(t))) return name;
  if (NON_PERSON_ENDINGS.has(familyTokens[familyTokens.length - 1].toLowerCase())) return name;
  const formattedGiven = givenTokens
    .map(t => (/^[A-Za-z]\.?$/.test(t) ? `${t[0].toUpperCase()}.` : t))
    .join(' ');
  return `${formattedGiven} ${family}${suffix}`;
}

/** Tame ALL-CAPS extraction artifacts and surname-first filing order;
 * leave already-natural names untouched. Pass the entity type when known —
 * orgs never get the comma reorder. */
export function entityDisplayName(name: string, entityType?: 'person' | 'org'): string {
  let out = name;
  if (out.length > 3 && out === out.toUpperCase()) {
    out = out
      .toLowerCase()
      .replace(/\b[a-z]/g, ch => ch.toUpperCase())
      .replace(/\b(Llc|Llp|Pllc|Pc|Pa|Inc|Ltd|Co|Ii|Iii|Iv|Usa|Us|Cv)\b/g, s => s.toUpperCase());
  }
  if (entityType !== 'org' && out.includes(',')) {
    out = reorderCommaName(out);
  }
  return out;
}

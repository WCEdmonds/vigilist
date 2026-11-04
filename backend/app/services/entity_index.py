"""In-memory index over a production's entities for fast resolution.

``match_entity`` (entity_resolution) scans every same-type entity for every
candidate and re-normalizes each one on every pass: O(candidates x entities)
per document, so a batch apply over a large matter slows down quadratically.
``EntityIndex`` is built once (per apply, per sync chunk, or per
``persist_extraction`` call) and answers the same question with dict lookups.

Parity contract with ``match_entity``:

* Passes 1-4 (attach: exact, alias, email, typo) return exactly the same
  decision, in the same pass order, with ties resolved the same way: the
  entity earliest in iteration order (load order, then creation order) wins.
  Every map below keeps the minimum-order entity per key.
* Pass 4 (typo) is exact: ``is_typo_variant`` requires the same token count
  and exactly one differing token, so two names that qualify agree on every
  other position. Each canonical norm is filed under one "wildcard" key per
  token position (the norm with that token removed, plus the position and
  token count); a candidate looks up its own wildcard keys and only those
  entities are tested.
* Pass 5 (suggest) is BLOCKED and may miss a few low-value suggestions:
  only entities sharing an exact normalized token, or the first or last
  three characters of the whole normalized name, are scored. Initial-pattern
  (same last token) and token similarity (gated on a shared exact token) are
  always inside the block; only whole-string similarity >= 0.85 between
  names sharing no token, no 3-char prefix and no 3-char suffix is lost.
  Scoring inside the block is the same arithmetic as match_entity (with
  exact upper-bound prefilters and a token-ratio cache), and ties keep the
  earliest entity. A missed suggestion still creates the entity, so the
  attach/create decision never changes. The parity test
  (tests/test_entity_index.py) asserts identical attach/create decisions and
  >= 95% of suggestions reproduced on a synthetic corpus.

Consistency: an index built at the start of an apply stays consistent for
that apply only because entity results apply one batch at a time per
production (pipeline._poll_parallel_inner, ``serial``) and the sync stage
runs alone. Callers drop the index whenever a document's transaction fails,
since the index already reflects that document's (rolled back) changes.
"""

from collections import Counter
from difflib import SequenceMatcher

from app.services.entity_resolution import (
    _SIMILARITY_THRESHOLD,
    _TOKEN_SIMILARITY_THRESHOLD,
    _initial_pattern,
    is_typo_variant,
    normalize_name,
)

_TOKEN_RATIO_CACHE_MAX = 500_000


class _Rec:
    """One entity as the index sees it. ``obj`` is the ORM object it was
    built from (only valid in the session that loaded it)."""

    __slots__ = ("id", "entity_type", "canonical_name", "norm", "aliases",
                 "alias_norms", "emails", "order", "obj", "chars")

    def __init__(self, entity, order: int):
        self.id = entity.id
        self.entity_type = entity.entity_type
        self.canonical_name = entity.canonical_name
        self.norm = normalize_name(entity.canonical_name or "")
        self.aliases = list(entity.aliases or [])
        self.alias_norms = {n for n in (normalize_name(a) for a in self.aliases if a) if n}
        self.emails = list((entity.attributes or {}).get("emails") or [])
        self.order = order
        self.obj = entity
        self.chars = Counter(self.norm)


def _valid_emails(emails) -> set:
    return {em.lower() for em in emails or [] if em and "@" in em}


def _typo_keys(etype: str, norm: str) -> list:
    tokens = norm.split()
    if len(tokens) < 2:
        return []  # is_typo_variant never accepts single-token names
    n = len(tokens)
    return [(etype, n, i, " ".join(tokens[:i] + tokens[i + 1:])) for i in range(n)]


def _block_keys(etype: str, norm: str) -> set:
    if not norm:
        return set()
    keys = {(etype, "t", t) for t in norm.split()}
    keys.add((etype, "p", norm[:3]))
    keys.add((etype, "s", norm[-3:]))
    return keys


class EntityIndex:
    def __init__(self, entities=(), session=None):
        # The session the ORM objects in the records belong to; another
        # session must load them by id (see persist_extraction).
        self.session = session
        self.loaded = True
        self._ratio_cache: dict = {}
        self._reset(entities)

    @classmethod
    def deferred(cls) -> "EntityIndex":
        """An index that loads itself (``rebuild``) on the first
        persist_extraction call it is passed to, in that call's session."""
        idx = cls()
        idx.loaded = False
        return idx

    def _reset(self, entities) -> None:
        self._recs: dict = {}
        self._next_order = 0
        self._exact: dict = {}
        self._alias: dict = {}
        self._email: dict = {}
        self._typo: dict = {}
        self._block: dict = {}
        for e in entities:
            self.add(e)

    @classmethod
    async def load(cls, db, production_id: int) -> "EntityIndex":
        from sqlalchemy import select
        from app.models import Entity

        rows = (await db.execute(
            select(Entity).where(Entity.production_id == production_id)
        )).scalars().all()
        return cls(rows, session=db)

    async def rebuild(self, db, production_id: int) -> None:
        """Reload from the database in place (callers keep the reference)."""
        fresh = await EntityIndex.load(db, production_id)
        self.session = fresh.session
        self._recs, self._next_order = fresh._recs, fresh._next_order
        self._exact, self._alias, self._email = fresh._exact, fresh._alias, fresh._email
        self._typo, self._block = fresh._typo, fresh._block
        self.loaded = True

    def __len__(self) -> int:
        return len(self._recs)

    def get(self, entity_id):
        return self._recs.get(entity_id)

    # ── maintenance ──

    @staticmethod
    def _keep_min(d: dict, key, rec: _Rec) -> None:
        cur = d.get(key)
        if cur is None or rec.order < cur.order:
            d[key] = rec

    def add(self, entity) -> _Rec:
        """Index a (new) entity after every existing one in iteration order."""
        rec = _Rec(entity, self._next_order)
        self._next_order += 1
        self._recs[rec.id] = rec
        t = rec.entity_type
        if rec.norm:
            self._keep_min(self._exact, (t, rec.norm), rec)
        for an in rec.alias_norms:
            self._keep_min(self._alias, (t, an), rec)
        for em in _valid_emails(rec.emails):
            self._keep_min(self._email, (t, em), rec)
        for k in _typo_keys(t, rec.norm):
            self._typo.setdefault(k, []).append(rec)  # appended in order
        for k in _block_keys(t, rec.norm):
            self._block.setdefault(k, []).append(rec)
        return rec

    def add_aliases(self, rec: _Rec, aliases) -> None:
        for a in aliases:
            rec.aliases.append(a)
            an = normalize_name(a) if a else ""
            if an and an not in rec.alias_norms:
                rec.alias_norms.add(an)
                self._keep_min(self._alias, (rec.entity_type, an), rec)

    def set_emails(self, rec: _Rec, emails) -> None:
        rec.emails = list(emails)
        for em in _valid_emails(rec.emails):
            self._keep_min(self._email, (rec.entity_type, em), rec)

    def refresh(self, rec: _Rec, entity) -> None:
        """Align a record's aliases/emails with the database row."""
        self.add_aliases(rec, [a for a in (entity.aliases or []) if a not in rec.aliases])
        db_emails = list((entity.attributes or {}).get("emails") or [])
        if db_emails != rec.emails:
            self.set_emails(rec, db_emails)

    # ── matching ──

    def _token_ratio(self, a: str, b: str) -> float:
        key = (a, b)
        r = self._ratio_cache.get(key)
        if r is None:
            if len(self._ratio_cache) >= _TOKEN_RATIO_CACHE_MAX:
                self._ratio_cache.clear()
            r = SequenceMatcher(None, a, b).ratio()
            self._ratio_cache[key] = r
        return r

    def _token_similarity(self, a: str, b: str) -> float:
        """entity_resolution._token_similarity with cached token ratios."""
        ta, tb = a.split(), b.split()
        if not ta or not tb:
            return 0.0
        if len(ta) > len(tb):
            ta, tb = tb, ta
        if not (set(ta) & set(tb)):
            return 0.0
        used: set = set()
        total = 0.0
        for t in ta:
            best, best_j = 0.0, None
            for j, u in enumerate(tb):
                if j in used:
                    continue
                r = self._token_ratio(t, u)
                if r > best:
                    best, best_j = r, j
            if best_j is not None:
                used.add(best_j)
            total += best
        coverage = len(used) / len(tb)
        return (total / len(ta)) * (0.5 + 0.5 * coverage)

    def match(self, candidate: dict) -> tuple:
        """Same contract as match_entity, but returns index records:
        ("attach", rec) | ("suggest", rec, score, rationale) | ("create", None)."""
        ctype = candidate["type"]
        cand_norm = normalize_name(candidate["name"])

        if cand_norm:
            rec = self._exact.get((ctype, cand_norm))
            if rec is not None:
                return ("attach", rec)
            rec = self._alias.get((ctype, cand_norm))
            if rec is not None:
                return ("attach", rec)

        cand_emails = _valid_emails(set(candidate.get("emails") or []))
        hits = [r for r in (self._email.get((ctype, em)) for em in cand_emails) if r is not None]
        if hits:
            return ("attach", min(hits, key=lambda r: r.order))

        if cand_norm:
            best_typo = None
            for k in _typo_keys(ctype, cand_norm):
                for r in self._typo.get(k, ()):
                    if best_typo is not None and r.order >= best_typo.order:
                        break  # lists are in order: nothing earlier remains
                    if is_typo_variant(cand_norm, r.norm):
                        best_typo = r
                        break
            if best_typo is not None:
                return ("attach", best_typo)

        if not cand_norm:
            return ("create", None)
        pool: dict = {}
        for k in _block_keys(ctype, cand_norm):
            for r in self._block.get(k, ()):
                pool[r.order] = r
        cand_len = len(cand_norm)
        cand_chars = Counter(cand_norm)
        best = None  # (score, rec, rationale)
        for order in sorted(pool):
            r = pool[order]
            e_norm = r.norm
            if not e_norm:
                continue
            if _initial_pattern(cand_norm, e_norm):
                score = 0.9
                rationale = f'initial pattern: "{candidate["name"]}" ~ "{r.canonical_name}"'
            else:
                # Exact upper bounds of SequenceMatcher.ratio() (what its
                # real_quick_ratio / quick_ratio compute) skip building a
                # matcher for pairs that cannot reach the threshold.
                total_len = cand_len + len(e_norm)
                ratio = 0.0
                if 2.0 * min(cand_len, len(e_norm)) / total_len >= _SIMILARITY_THRESHOLD:
                    common = sum((cand_chars & r.chars).values())
                    if 2.0 * common / total_len >= _SIMILARITY_THRESHOLD:
                        ratio = SequenceMatcher(None, cand_norm, e_norm).ratio()
                if ratio >= _SIMILARITY_THRESHOLD:
                    score = ratio
                    rationale = f'name similarity {ratio:.2f}: "{candidate["name"]}" ~ "{r.canonical_name}"'
                else:
                    token = self._token_similarity(cand_norm, e_norm)
                    if token >= _TOKEN_SIMILARITY_THRESHOLD:
                        score = token
                        rationale = f'token similarity {token:.2f}: "{candidate["name"]}" ~ "{r.canonical_name}"'
                    else:
                        continue
            if best is None or score > best[0]:
                best = (score, r, rationale)
        if best is not None:
            return ("suggest", best[1], round(best[0], 3), best[2])
        return ("create", None)

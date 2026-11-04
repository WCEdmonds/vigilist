"""Synthetic ACME-style entity corpus for the entity-index parity and
performance tests. No client data: every name is generated from the fake
word lists below with a fixed seed."""

import random
import uuid

FIRST = ["Alan", "Beth", "Carla", "Dmitri", "Elena", "Farid", "Greta", "Hiro", "Ines", "Jorge",
         "Kara", "Liam", "Mona", "Nils", "Olga", "Pavel", "Quinn", "Rosa", "Sven", "Tara",
         "Umar", "Vera", "Wade", "Xena", "Yusuf", "Zara", "Anton", "Bruno", "Celia", "Dario",
         "Edith", "Felix", "Gavin", "Hanna", "Ivan", "Jules", "Kenji", "Lena", "Marco", "Nadia",
         "Oscar", "Petra", "Ralf", "Selma", "Tomas", "Ulla", "Viktor", "Wanda", "Yara", "Zoltan",
         "Andrea", "Bianca", "Cyril", "Dagny", "Emil", "Freya", "Gunnar", "Hedda", "Igor", "Jasmin"]
LAST = ["Abbott", "Barlow", "Castellan", "Draper", "Ellison", "Fairbanks", "Garrick", "Holloway",
        "Ingram", "Jardine", "Kessler", "Lockhart", "Marlowe", "Northcott", "Oakley", "Pemberton",
        "Quimby", "Radcliffe", "Sterling", "Thackeray", "Underwood", "Vance", "Whitlock", "Yardley",
        "Zimmer", "Ashdown", "Blackwood", "Crowther", "Dunmore", "Everly", "Fenwick", "Goodall",
        "Hartley", "Ironside", "Jessop", "Kingsley", "Langford", "Merriweather", "Nightingale",
        "Ormsby", "Prescott", "Rutherford", "Sinclair", "Talbot", "Upton", "Vickers", "Wexford",
        "Ashby", "Brannigan", "Carrow", "Delacroix", "Emberly", "Foxworth", "Grimsby", "Hollister",
        "Inkster", "Jolliffe", "Kirkwood", "Lindqvist", "Montague", "Norwood", "Overton", "Penrose",
        "Quarles", "Rainier", "Stanhope", "Treadwell", "Ulverston", "Valentine", "Westbrook",
        "Yelverton", "Zabriskie", "Aldridge", "Beaumont", "Chadwick", "Dorsey", "Eastwood", "Fairfax"]
ORG_WORDS = ["Acme", "Globex", "Initech", "Umbrella", "Stark", "Wayne", "Tyrell", "Cyberdyne",
             "Soylent", "Hooli", "Vandelay", "Wonka", "Gringotts", "Monarch", "Oscorp", "Massive",
             "Dynamic", "Pinnacle", "Summit", "Harbor", "Meridian", "Northwind", "Contoso", "Fabrikam",
             "Litware", "Proseware", "Adventure", "Coho", "Lucerne", "Margie", "Trey", "Wingtip",
             "Alpine", "Blue", "Granite", "Silver", "Redwood", "Lakeside", "Riverbend", "Crescent",
             "Keystone", "Liberty", "Frontier", "Heritage", "Sterling", "Beacon", "Atlas", "Orion",
             "Apex", "Vertex", "Quantum", "Nimbus", "Zenith", "Horizon", "Cobalt", "Ember", "Falcon"]
ORG_KIND = ["Holdings", "Logistics", "Capital", "Systems", "Labs", "Partners", "Industries",
            "Foods", "Medical", "Energy", "Realty", "Consulting", "Freight", "Media", "Bank"]
ORG_SUFFIX = ["Inc.", "LLC", "Corp", "Ltd", "", "", ""]
PLACE_A = ["North", "South", "East", "West", "Upper", "Lower", "Old", "New", "Lake", "Port"]
PLACE_B = ["Haven", "Ridge", "Brook", "Field", "Ford", "Glen", "Marsh", "Vale", "Crest", "Point",
           "Hollow", "Springs", "Meadow", "Bridge", "Harbor", "Grove"]
PLACE_KIND = ["Elementary School", "High School", "Middle School", "Park", "Library",
              "Courthouse", "Hospital", "Station", ""]


def _typo(rng: random.Random, word: str) -> str:
    """One random edit: insertion, deletion or substitution."""
    if len(word) < 3:
        return word + "e"
    i = rng.randrange(1, len(word))
    kind = rng.choice(["ins", "del", "sub"])
    letter = rng.choice("aeioulnrst")
    if kind == "ins":
        return word[:i] + letter + word[i:]
    if kind == "del":
        return word[:i] + word[i + 1:]
    return word[:i] + letter + word[i + 1:]


def _person(rng):
    first, last = rng.choice(FIRST), rng.choice(LAST)
    if rng.random() < 0.15:
        last = f"{last}-{rng.choice(LAST)}"
    return first, last


def _org(rng):
    parts = [rng.choice(ORG_WORDS)]
    if rng.random() < 0.7:
        parts.append(rng.choice(ORG_KIND))
    if rng.random() < 0.3:
        parts.insert(1, rng.choice(ORG_WORDS))
    suffix = rng.choice(ORG_SUFFIX)
    return " ".join(parts + ([suffix] if suffix else []))


def _place(rng):
    kind = rng.choice(PLACE_KIND)
    return " ".join(p for p in (rng.choice(PLACE_A), rng.choice(PLACE_B), kind) if p)


def make_entities(n: int, seed: int = 7, factory=None) -> list:
    """``n`` existing entities (persons ~60%, orgs ~30%, locations ~10%).
    ``factory(**fields)`` builds each one (default: app.models.Entity)."""
    if factory is None:
        from app.models import Entity

        def factory(**kw):
            return Entity(production_id=1, mention_count=0, **kw)
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        r = rng.random()
        aliases, emails = [], []
        if r < 0.6:
            first, last = _person(rng)
            name = rng.choice([f"{first} {last}", f"{last}, {first}", f"{first} {rng.choice('ABCDEFGH')}. {last}"])
            etype = "person"
            if rng.random() < 0.4:
                aliases.append(f"{first[0]}. {last}")
            if rng.random() < 0.3:
                aliases.append(last)
            if rng.random() < 0.5:
                emails.append(f"{first[0].lower()}{last.lower()}@{rng.choice(ORG_WORDS).lower()}.example")
        elif r < 0.9:
            name, etype = _org(rng), "org"
            if rng.random() < 0.3:
                aliases.append(name.split()[0])
        else:
            name, etype = _place(rng), "location"
        out.append(factory(id=uuid.uuid4(), entity_type=etype, canonical_name=name,
                           aliases=aliases, attributes={"emails": emails} if emails else {}))
    return out


def make_candidates(existing: list, n: int, seed: int = 11) -> list:
    """Extraction candidates: a mix of exact/reformatted repeats, alias and
    email hits, typo and substitution variants, initials, token drops, and
    brand-new names."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        e = rng.choice(existing)
        name, etype, emails = e.canonical_name, e.entity_type, []
        r = rng.random()
        if r < 0.15:
            name = name.upper() if rng.random() < 0.5 else f"Mr. {name}"
        elif r < 0.25 and e.aliases:
            name = rng.choice(e.aliases)
        elif r < 0.32 and (e.attributes or {}).get("emails"):
            name = rng.choice(FIRST)
            emails = [e.attributes["emails"][0].upper()]
        elif r < 0.55:
            toks = name.replace(",", "").split()
            i = rng.randrange(len(toks))
            toks[i] = _typo(rng, toks[i])
            name = " ".join(toks)
        elif r < 0.62 and etype == "person":
            toks = name.replace(",", "").split()
            name = f"{toks[0][0]}. {toks[-1]}"
        elif r < 0.70:
            toks = name.split()
            if len(toks) > 1:
                toks.pop(rng.randrange(len(toks)))
            name = " ".join(toks)
        else:
            etype = rng.choice(["person", "person", "org", "location"])
            if etype == "person":
                first, last = _person(rng)
                name = f"{first} {last}"
                if rng.random() < 0.3:
                    emails = [f"{first.lower()}@{rng.choice(ORG_WORDS).lower()}.example"]
            elif etype == "org":
                name = _org(rng)
            else:
                name = _place(rng)
        forms = [name] + ([name.split()[-1]] if len(name.split()) > 1 and rng.random() < 0.3 else [])
        out.append({"name": name, "type": etype, "surface_forms": forms, "role": None, "emails": emails})
    return out

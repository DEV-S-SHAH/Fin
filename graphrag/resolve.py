"""Entity normalisation and resolution.

The identity of an entity in this system is a *slug*: a lowercase,
alphanumeric-only key derived from the surface name. Two mentions collapse to
the same node when they produce the same slug, which is what lets entities
discovered in different chunks merge into one node.

The normaliser is intentionally conservative. It removes the class of
variation that shows up in extracted text (case, accents, punctuation,
possessives, parenthetical qualifiers, honorifics) without guessing at
semantic identity, because semantic aliasing belongs to the LLM, not to a
string function.
"""

from __future__ import annotations

import re
import unicodedata

# Punctuation and symbols that carry no identity signal.
_PUNCT = re.compile(r"[^0-9a-z]+")

# Parenthetical disambiguators: "Mercury (planet)" -> "Mercury".
_PAREN = re.compile(r"\s*\([^)]*\)")

# Possessives: "Bell's" -> "Bell", "Bell'" -> "Bell".
_POSSESSIVE = re.compile(r"['\u2019]s?$")

# Honorifics and rank prefixes that are not part of the referent's identity.
_LEADING_TITLES = re.compile(
    r"^(?:dr|prof|professor|mr|mrs|ms|miss|sir|dame|lord|lady|capt|captain|"
    r"gen|general|rev|reverend|st|saint|president|senator|governor|judge|"
    r"the)\.?\s+",
    re.IGNORECASE,
)

# Titles that are part of a proper name and must not be stripped.
_KEEP_TITLE = re.compile(
    r"^(?:king|queen|prince|princess|pope|emperor|empress|duke|duchess|earl)\b",
    re.IGNORECASE,
)

# A trailing generic descriptor is kept, not stripped: dropping it would merge
# "Arctic Ocean" and "Pacific Ocean" into "ocean".
_NUMERIC = re.compile(r"^[0-9a-z]+$")


UNSPECIFIED = "unspecified"

# Function words that no entity is ever named. Rejecting these keeps articles
# and conjunctions from becoming nodes when a model emits one by mistake.
_NON_ENTITY_WORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "nor", "so", "yet", "if", "then",
        "than", "that", "this", "these", "those", "there", "here", "when",
        "where", "which", "who", "whom", "whose", "what", "why", "how",
        "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
        "did", "done", "has", "have", "had", "will", "would", "shall", "should",
        "may", "might", "must", "can", "could", "of", "in", "on", "at", "to",
        "for", "from", "by", "with", "as", "it", "its", "they", "them", "their",
        "he", "she", "his", "her", "we", "us", "our", "you", "your", "not",
        "no", "yes", "all", "any", "some", "each", "every", "both", "other",
        "others", "another", "into", "onto", "over", "under", "above", "below",
        "between", "among", "during", "before", "after", "about", "afterwards",
        UNSPECIFIED,
    }
)


def strip_accents(value: str) -> str:
    """Fold accented characters to their ASCII base."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def clean_name(value: str) -> str:
    """Return a display-ready surface form of *value*."""
    text = unicodedata.normalize("NFKC", value).strip()
    text = strip_accents(text)
    text = _PAREN.sub("", text)
    text = _POSSESSIVE.sub("", text)
    text = text.replace("\u2019", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\n\r.,;:!?-'")


def slugify(value: str) -> str:
    """Reduce *value* to its canonical identity key.

    Returns an empty string when nothing meaningful survives, which callers
    must treat as "reject this candidate" rather than as a valid id.
    """
    text = clean_name(value)
    if not text:
        return ""

    # Only strip a leading title when what follows still looks like a name and
    # the phrase is not a regnal title that belongs to the name itself.
    if not _KEEP_TITLE.match(text):
        stripped = _LEADING_TITLES.sub("", text)
        # Require at least two remaining words-worth of content, so "The" or
        # "Dr" alone cannot collapse to an empty key.
        if stripped and stripped != text:
            text = stripped

    text = strip_accents(text).lower()
    slug = _PUNCT.sub("", text)
    return slug if _NUMERIC.match(slug) else ""


def singularize(word: str) -> str:
    """Return a plausible singular form of *word*, or *word* unchanged.

    Deliberately conservative: only the endings that are unambiguous in
    technical prose are stripped. Words ending in ``ss``/``us``/``is``/``as``/
    ``os`` are left alone so genuine singulars survive ("bias" not "bia",
    "gas" not "ga", "analysis" not "analysi").
    """
    lowered = word.lower()
    if len(lowered) < 4 or not lowered.endswith("s"):
        return word
    if lowered.endswith(("ss", "us", "is", "as", "os")):
        return word
    if lowered.endswith("ies") and len(lowered) > 4:
        return word[:-3] + "y"
    if lowered.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    return word[:-1]


def name_variants(text: str) -> list[str]:
    """Search forms of *text*, most faithful first.

    A question is written in the asker's words, so it says "transformers" where
    the graph holds "Transformer". This yields the original plus de-pluralised
    forms of the trailing words, which is enough to bridge that gap without
    the risk of a general stemmer inventing matches.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    words = cleaned.split()
    variants = [cleaned]

    # De-pluralise from the end, so a trailing noun is corrected first
    # ("transformer architectures" -> "transformer architecture").
    for start in range(len(words)):
        tail = [singularize(w) for w in words[start:]]
        if tail == words[start:]:
            break
        candidate = " ".join(words[:start] + tail)
        if candidate not in variants:
            variants.append(candidate)

    return variants


def is_usable_name(value: str, min_length: int = 2) -> bool:
    """Reject placeholders and noise that a model may emit for "unknown"."""
    if not value or not value.strip():
        return False
    candidate = clean_name(value)
    if len(candidate) < min_length:
        return False
    placeholders = {
        "unknown",
        "n a",
        "na",
        "none",
        "null",
        "n/a",
        "tbd",
        "example",
        "placeholder",
        "test",
        "entity",
        "entities",
        "document",
        "text",
        "passage",
        "various",
        "misc",
        "multiple",
        "unnamed",
    }
    lowered = candidate.lower()
    if lowered in placeholders or lowered in _NON_ENTITY_WORDS:
        return False
    # A single letter is almost always a stray variable, not a named entity.
    if len(candidate) == 1 and candidate.isalpha():
        return False
    return True


def merge_observation(
    existing: dict[str, str], new: dict[str, str]
) -> dict[str, str]:
    """Combine two observations of the same entity.

    Keeps the longer, more informative surface name and description. A missing
    incoming field never overwrites a populated one, which makes ingestion
    order-independent and therefore safe to re-run.
    """
    merged = dict(existing)
    for key in ("name", "entity_type", "description"):
        incoming = (new.get(key) or "").strip()
        current = (merged.get(key) or "").strip()
        if not incoming:
            continue
        if not current:
            merged[key] = incoming
        elif key == "description":
            # Prefer the richer description; if the incoming one adds new
            # information, keep the longer text.
            if len(incoming) > len(current) * 1.5 and incoming not in current:
                merged[key] = incoming
        else:
            if len(incoming) > len(current):
                merged[key] = incoming
    return merged

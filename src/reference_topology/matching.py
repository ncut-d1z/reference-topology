"""Conservative bibliographic identity matching, implemented independently.

Scores rank candidates; they are not probabilities. Only an unambiguous strong
identifier with no contradictory evidence is eligible for automatic acceptance.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import unquote, urlsplit


MATCHER_VERSION = "python-conservative-1.0"
_DOI = re.compile(r"(?<![\w])10\.\d{4,9}/[^\s<>\"“”]+", re.I)
_ARXIV = re.compile(
    r"(?<![\w.])(?P<id>(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7}))"
    r"(?P<version>v\d+)?(?!\w|\.\d)", re.I
)


def normalize_doi(value: str | None) -> str | None:
    """Extract a DOI, preserving balanced suffix parentheses, or return None."""
    if not value:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if re.match(r"^https?://(?:dx\.)?doi\.org/", text, re.I):
        text = unquote(urlsplit(text).path.lstrip("/"))
    else:
        text = unquote(text)
    found = _DOI.search(text)
    if not found:
        return None
    doi = found.group().rstrip(".,;:!?。，；：'’")
    # A closing parenthesis in a DOI can be meaningful. Remove only unmatched
    # closing punctuation belonging to the containing bibliography entry.
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while doi.endswith(closing) and doi.count(closing) > doi.count(opening):
            doi = doi[:-1].rstrip(".,;:!?。，；：'’")
    suffix = doi.partition("/")[2]
    if not suffix or not any(c.isalnum() for c in suffix):
        return None
    return doi.casefold()


def normalize_arxiv(value: str | None) -> str | None:
    """Return the base arXiv identifier; version remains separate metadata.

    Bare identifiers are accepted. Within prose a recognizable arXiv prefix is
    required so a decimal such as ``2020.1234`` is not treated as an identifier.
    """
    if not value:
        return None
    text = unquote(unicodedata.normalize("NFKC", str(value))).strip()
    text = re.sub(r"\.pdf(?=\s|$|[?#.,;:)])", "", text, flags=re.I)
    match = _ARXIV.search(text)
    if not match:
        return None
    bare = text.strip(" \t\n.,;:()[]{}")
    has_prefix = bool(re.search(r"arxiv\s*[:.]|arxiv\.org/(?:abs|pdf)/", text, re.I))
    if not has_prefix and match.group() != bare:
        return None
    identifier = match.group("id").casefold()
    # YYMM must contain a real month for both modern and historic identifiers.
    date_digits = identifier.split("/")[-1].split(".")[0]
    if len(date_digits) >= 4 and not 1 <= int(date_digits[2:4]) <= 12:
        return None
    return identifier


def normalize_title(value: str | None) -> str:
    """Normalize only the comparison copy; callers retain the original text."""
    text = re.sub(r"(?<=\w)[-\u00ad]\s*\n\s*(?=\w)", "", str(value or ""))
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def _identifiers(record: dict[str, Any], scheme: str) -> set[str]:
    normalizer = normalize_doi if scheme == "doi" else normalize_arxiv
    values: list[Any] = [record.get(scheme), record.get(scheme.upper())]
    identifiers = record.get("identifiers") or []
    if isinstance(identifiers, dict):
        values.extend([identifiers.get(scheme), identifiers.get(scheme.upper())])
    else:
        for item in identifiers:
            if not isinstance(item, dict):
                continue
            item_scheme = str(item.get("scheme") or item.get("type") or "").casefold()
            if item_scheme == scheme:
                values.append(item.get("value_normalized") or item.get("value")
                              or item.get("value_raw") or item.get("identifier"))
    return {normalized for value in values if (normalized := normalizer(value))}


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    left, right = set(a.split()), set(b.split())
    jaccard = len(left & right) / max(1, len(left | right))
    sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return 0.55 * sequence + 0.45 * jaccard


def _first_author(record: dict[str, Any]) -> str:
    authors = record.get("authors") or []
    if isinstance(authors, str):
        author: Any = authors
    elif authors:
        author = authors[0]
    else:
        return ""
    if isinstance(author, dict):
        surname = author.get("family_name") or author.get("lastName")
        if surname:
            return normalize_title(surname)
        author = author.get("display_name") or author.get("name") or ""
    name = str(author)
    if "," in name:
        return normalize_title(name.split(",", 1)[0])
    parts = normalize_title(name).split()
    return parts[-1] if parts else ""


def _year(value: Any) -> int | None:
    match = re.search(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", str(value or ""))
    return int(match.group()) if match else None


def _conflicts(reference: dict[str, Any], paper: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    for scheme in ("doi", "arxiv"):
        left, right = _identifiers(reference, scheme), _identifiers(paper, scheme)
        if left and right and not left & right:
            conflicts.append(f"conflicting {scheme} identifiers")
    a, b = normalize_title(reference.get("title")), normalize_title(paper.get("title"))
    if len(a) >= 12 and len(b) >= 12 and _title_similarity(a, b) < 0.48:
        conflicts.append("substantially different titles")
    y1, y2 = _year(reference.get("year")), _year(paper.get("year"))
    if y1 and y2 and abs(y1 - y2) > 2:
        conflicts.append("incompatible publication years")
    a1, a2 = _first_author(reference), _first_author(paper)
    if a1 and a2 and len(a1) > 2 and len(a2) > 2 and a1 != a2:
        conflicts.append("different first authors")
    t1 = normalize_title(reference.get("publication_type") or reference.get("type"))
    t2 = normalize_title(paper.get("publication_type") or paper.get("type"))
    if t1 and t2 and ("preprint" in t1) != ("preprint" in t2):
        conflicts.append("preprint versus published-version identity requires review")
    if reference.get("_needs_review"):
        conflicts.append("reference boundary or parsing requires review")
    return conflicts


def _fingerprint_score(reference: dict[str, Any], paper: dict[str, Any]) -> float:
    if reference.get("title"):
        return 0.0
    author = _first_author(reference)
    year = _year(reference.get("year"))
    if not author or not year or author != _first_author(paper) or year != _year(paper.get("year")):
        return 0.0
    matches = 0
    for keys in (("venue", "publicationTitle"), ("volume",), ("pages", "article_number")):
        left = next((reference.get(k) for k in keys if reference.get(k)), None)
        right = next((paper.get(k) for k in keys if paper.get(k)), None)
        if left and right:
            if keys[0] == "pages":
                left, right = str(left).split("-")[0], str(right).split("-")[0]
            if normalize_title(str(left)) != normalize_title(str(right)):
                return 0.0
            matches += 1
    return 0.90 if matches >= 2 else 0.0


def match_reference(parsed: dict[str, Any], papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return up to eight candidates, never silently choosing an ambiguous ID.

    Papers may expose DOI/arXiv directly or in ``identifiers`` records. A
    bibliographic fingerprint and exact/fuzzy titles always require review.
    """
    candidates: list[dict[str, Any]] = []
    reference_title = normalize_title(parsed.get("title"))
    for paper in papers:
        paper_id = paper.get("id") or paper.get("paper_id")
        if not paper_id:
            continue
        strong = [scheme for scheme in ("doi", "arxiv")
                  if _identifiers(parsed, scheme) & _identifiers(paper, scheme)]
        conflicts = _conflicts(parsed, paper)
        if strong:
            method, score = strong[0], 1.0
            reason = f"Matched {' and '.join(strong)}"
        else:
            title = normalize_title(paper.get("title"))
            similarity = _title_similarity(reference_title, title)
            fingerprint = _fingerprint_score(parsed, paper)
            if reference_title and reference_title == title:
                method, score, reason = "title-exact", 0.96, "Normalized titles agree; manual confirmation required"
            elif len(reference_title) >= 12 and len(title) >= 12 and similarity >= 0.68:
                method, score, reason = "title-fuzzy", min(0.94, similarity), "Similar titles; manual confirmation required"
            elif fingerprint:
                method, score, reason = "bibliographic-fingerprint", fingerprint, "Author, year and publication fields agree; manual confirmation required"
            else:
                continue
        if conflicts:
            reason += "; " + "; ".join(conflicts)
        candidates.append({"paper_id": str(paper_id), "method": method,
                           "score": round(score, 6), "safe": bool(strong) and not conflicts,
                           "reason": reason})
    strong_candidates = [c for c in candidates if c["method"] in {"doi", "arxiv"}]
    if len(strong_candidates) > 1:
        for candidate in strong_candidates:
            candidate["safe"] = False
            candidate["reason"] += "; identifier resolves to multiple papers"
    candidates.sort(key=lambda candidate: (-candidate["score"], candidate["paper_id"]))
    return candidates[:8]

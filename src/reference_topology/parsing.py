"""Local PDF bibliography extraction with explicit, conservative failure states.

The rule parser is an independent baseline, not a port of Zotero plugin source.
It retains text and page evidence so uncertain segmentation can be reviewed.
No external service is contacted unless ``engine='grobid-local'`` is selected.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import math
import re
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .matching import normalize_arxiv, normalize_doi, normalize_title


PARSER_VERSION = "python-rules-1.0"
GROBID_ADAPTER_VERSION = "grobid-local-1.0"
_YEAR = re.compile(r"(?<![\w./])((?:18|19|20|21)\d{2})[a-z]?(?![\w./])")
_REFERENCE_HEADING = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+)?"
    r"(?:references|bibliography|works\s+cited|literature\s+cited|参考文献|引用文献)"
    r"\s*[:：]?\s*$", re.I
)
_STOP_HEADING = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*\.?)\s+)?(?:appendix|appendices|acknowledg(?:e)?ments?"
    r"|supplementary\s+material|supplemental\s+material|附录|致谢)(?:\b|\s|[A-Z一二三四])", re.I
)
_LABEL = re.compile(
    r"^\s*(?P<label>\[(?:\d{1,4}|[A-Za-z][A-Za-z0-9+_:-]{1,20})\]"
    r"|\(\d{1,4}\)|\d{1,4}[.)])(?:\s+|(?=\S)|$)(?P<body>.*)$"
)
_BARE_LABEL = re.compile(r"^\s*(?P<label>\d{1,3})\s+(?P<body>[A-ZÀ-ÖØ-Þ\u4e00-\u9fff].*)$")
_BODY_HEADING = re.compile(r"^(?:\d+[.\s]+)?(?:abstract|introduction|摘要|引言)\b", re.I)
_GENERIC_TITLES = {"untitled", "anonymous", "document", "microsoft word", "latex", "unknown"}
_TEI = {"t": "http://www.tei-c.org/ns/1.0"}


class ParseError(RuntimeError):
    """Expected parser failure that can be safely surfaced by the worker."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Line:
    text: str
    page: int
    bbox: tuple[float, float, float, float] | None = None
    font_size: float = 0.0
    width: float = 0.0
    height: float = 0.0


def _check_time(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ParseError("parse_timeout", "PDF parsing exceeded the configured time limit.")


def _warning(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _is_reference_heading(text: str) -> bool:
    value = _clean(text)
    return bool(_REFERENCE_HEADING.fullmatch(value))


def _label(text: str) -> tuple[str, str] | None:
    match = _LABEL.match(text) or _BARE_LABEL.match(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group("label"))
    if digits and 1800 <= int(digits) <= 2199:
        return None
    return match.group("label"), match.group("body")


def _looks_like_authors(text: str) -> bool:
    text = text.strip(" \t.,;:()[]")
    if not text or len(text) > 160 or len(text.split()) > 20:
        return False
    if re.search(r"\b(?:university|institute|department|abstract|journal|proceedings|doi|http|pp|vol)\b|[@\d]", text, re.I):
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}(?:[、，,;；\s]+[\u4e00-\u9fff]{2,4})*", text):
        return True
    if re.search(r"\b[A-ZÀ-ÖØ-Þ]\.\s*|[A-ZÀ-ÖØ-Þ][\w’'-]+,\s*[A-ZÀ-ÖØ-Þ]", text):
        return True
    words = [word for word in re.split(r"[\s,;&]+", text) if word and word.casefold() not in {"and", "et", "al"}]
    return 2 <= len(words) <= 8 and all(word[0].isupper() for word in words)


def _author_year_start(text: str) -> bool:
    if _label(text):
        return False
    year = _YEAR.search(text[:190])
    return bool(year and _looks_like_authors(text[:year.start()].rstrip(" (.")))


def _authors(value: str | list[Any] | None) -> list[dict[str, str]]:
    if not value:
        return []
    values = value if isinstance(value, list) else re.split(r"\s*;\s*|\s+and\s+|\s*&\s*|、|；", value)
    result: list[dict[str, str]] = []
    for entry in values:
        if isinstance(entry, dict):
            name = entry.get("display_name") or entry.get("name")
            pieces = [str(name or "")]
        else:
            # Preserve comma-separated surname/given-name pairs.
            name = str(entry).strip()
            if re.match(r"^[^,]+,\s*[A-ZÀ-ÖØ-Þ]\.", name):
                pieces = re.split(r",\s*(?=[A-ZÀ-ÖØ-Þ][\w’'-]+,\s*[A-ZÀ-ÖØ-Þ])", name)
            elif re.match(r"^[A-ZÀ-ÖØ-Þ]\.", name):
                pieces = re.split(r",\s*(?=[A-ZÀ-ÖØ-Þ]\.)", name)
            else:
                chunks = [chunk.strip() for chunk in name.split(",")]
                pieces = chunks if len(chunks) > 1 and all(len(chunk.split()) >= 2 for chunk in chunks) else [name]
        for piece in pieces:
            name = _clean(piece).strip(" ,;()")
            name = re.sub(r"\s*(?:et\s+al\.?|[†‡*]+)$", "", name).strip()
            if name and name.casefold() not in {"anonymous", "unknown"}:
                result.append({"display_name": name})
    return result


def _metadata() -> dict[str, Any]:
    return {"title": None, "authors": [], "year": None, "doi": None, "arxiv": None, "venue": None}


def _join_reference(lines: list[_Line]) -> str:
    # Store raw text with line boundaries. Only parsing uses a repaired copy.
    return "\n".join(line.text.rstrip() for line in lines).strip()


def _parsing_copy(text: str) -> str:
    text = re.sub(r"(?<=\w)[-\u00ad]\s*\n\s*(?=\w)", "", text)
    return _clean(text)


def _sentences(text: str) -> list[str]:
    protected = re.sub(r"\b([A-ZÀ-ÖØ-Þ])\.", lambda match: match.group(1) + "\ue000", text)
    return [part.replace("\ue000", ".").strip(" .;,()")
            for part in re.split(r"[.!?]\s+(?=[A-ZÀ-ÖØ-Þ\u4e00-\u9fff])", protected)
            if part.strip(" .;,()")]


def _usable_title(title: str) -> bool:
    if not 8 <= len(title) <= 700 or re.fullmatch(r"(?:[A-Z]\.?\s*)+[A-Z][a-z]+", title):
        return False
    return not bool(re.match(r"^(?:doi|https?://|arxiv|vol\.|pp\.|\d+[(:,])", title, re.I))


def parse_reference_text(raw_text: str) -> dict[str, Any]:
    """Parse one manually supplied or extracted entry without inventing fields."""
    result = _metadata()
    text = _parsing_copy(raw_text)
    label = _label(text)
    if label:
        text = label[1]
    result["doi"] = normalize_doi(text)
    result["arxiv"] = normalize_arxiv(text)
    if result["arxiv"]:
        version = re.search(re.escape(result["arxiv"]) + r"v(\d+)", text, re.I)
        result["arxiv_version"] = int(version.group(1)) if version else None
    # DOI paths and URLs are not evidence for a publication year.
    without_identifiers = re.sub(r"https?://\S+|10\.\d{4,9}/\S+|arxiv\s*:\s*\S+", "", text, flags=re.I)
    years = list(_YEAR.finditer(without_identifiers))
    if years:
        result["year"] = int(years[0].group(1))
    quoted = re.search(r"[\"“‘]([^\"”’]{8,700})[\"”’]", text)
    author_prefix = ""
    if quoted:
        title = quoted.group(1).strip(" ,.;")
        if _usable_title(title):
            result["title"] = title
        author_prefix = text[:quoted.start()].rstrip(" ,;")
    else:
        year = _YEAR.search(without_identifiers)
        if year and year.start() < 170 and _looks_like_authors(without_identifiers[:year.start()].rstrip(" (.")):
            author_prefix = without_identifiers[:year.start()].rstrip(" (")
            tail = without_identifiers[year.end():].lstrip(" ).,;:")
            sentences = _sentences(tail)
            if sentences and _usable_title(sentences[0]):
                result["title"] = sentences[0]
            if len(sentences) >= 2:
                result["venue"] = sentences[1].strip()
        else:
            sentences = _sentences(without_identifiers)
            if len(sentences) >= 2 and _looks_like_authors(sentences[0]):
                author_prefix = sentences[0]
                if _usable_title(sentences[1]):
                    result["title"] = sentences[1]
                if len(sentences) >= 3:
                    result["venue"] = sentences[2]
    if author_prefix and _looks_like_authors(author_prefix):
        result["authors"] = _authors(author_prefix)
    locator = re.search(r"\b(?:pp?\.?\s*)(\d+(?:\s*[-–]\s*\d+)?)", text, re.I)
    volume = re.search(r"\bvol\.?\s*(\d+)", text, re.I)
    if locator:
        result["pages"] = re.sub(r"\s+", "", locator.group(1))
    if volume:
        result["volume"] = volume.group(1)
    return result


def _order_page(lines: list[_Line], warnings: list[str]) -> list[_Line]:
    if not lines or any(line.bbox is None for line in lines):
        return lines
    width = lines[0].width
    midpoint = width / 2
    left = [line for line in lines if line.bbox[2] < midpoint + width * 0.025]
    right = [line for line in lines if line.bbox[0] > midpoint - width * 0.025]
    wide = [line for line in lines if line not in left and line not in right]
    overlap = False
    if len(left) >= 2 and len(right) >= 2:
        overlap = min(max(x.bbox[3] for x in left), max(x.bbox[3] for x in right)) > max(min(x.bbox[1] for x in left), min(x.bbox[1] for x in right))
    top_down = lambda items: sorted(items, key=lambda line: (-line.bbox[3], line.bbox[0]))
    if not overlap:
        return top_down(lines)
    _warning(warnings, f"two_column_layout: page {lines[0].page}; column order inferred from coordinates.")
    # Full-width headings divide vertical bands; within a band read the left
    # column completely, followed by the right. This also preserves title blocks.
    ordered: list[_Line] = []
    remaining = list(left + right)
    for barrier in top_down(wide):
        above = [line for line in remaining if line.bbox[3] > barrier.bbox[3] + 1]
        ordered.extend(top_down([line for line in above if line in left]))
        ordered.extend(top_down([line for line in above if line in right]))
        remaining = [line for line in remaining if line not in above]
        ordered.append(barrier)
    ordered.extend(top_down([line for line in remaining if line in left]))
    ordered.extend(top_down([line for line in remaining if line in right]))
    return ordered


def _extract_lines(path: Path, reader: Any, deadline: float, warnings: list[str]) -> list[_Line]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTChar, LTContainer, LTTextLine

    pages: dict[int, list[_Line]] = {}

    def collect(container: Any, page: int, width: float, height: float) -> list[_Line]:
        found: list[_Line] = []
        if isinstance(container, LTTextLine):
            text = container.get_text().strip("\r\n")
            if text.strip():
                sizes = [float(char.size) for char in container if isinstance(char, LTChar)]
                found.append(_Line(text, page, tuple(float(x) for x in container.bbox),
                                   max(sizes, default=0.0), width, height))
        elif isinstance(container, LTContainer):
            for child in container:
                found.extend(collect(child, page, width, height))
        return found

    try:
        for page, layout in enumerate(extract_pages(str(path), laparams=LAParams(boxes_flow=None)), 1):
            _check_time(deadline)
            pages[page] = _order_page(collect(layout, page, layout.width, layout.height), warnings)
    except ParseError:
        raise
    except Exception as exc:
        _warning(warnings, f"layout_extraction_failed: {type(exc).__name__}; remaining pages use text-only extraction.")
    for index, page in enumerate(reader.pages, 1):
        _check_time(deadline)
        if index in pages and pages[index]:
            continue
        try:
            text = page.extract_text() or ""
            pages[index] = [_Line(line, index, width=float(page.mediabox.width), height=float(page.mediabox.height))
                            for line in text.splitlines() if line.strip()]
        except Exception as exc:
            pages.setdefault(index, [])
            _warning(warnings, f"page_extraction_failed: page {index}, {type(exc).__name__}.")
        if not pages[index]:
            _warning(warnings, f"page_without_text: page {index}; it may be blank or require OCR.")
    return [line for page in sorted(pages) for line in pages[page]]


def _remove_running_headers(lines: list[_Line], page_count: int) -> list[_Line]:
    def signature(line: _Line) -> str:
        return re.sub(r"\d+", "#", normalize_title(line.text))

    margin = [line for line in lines if line.bbox and (line.bbox[1] > line.height * .92 or line.bbox[3] < line.height * .08)]
    occurrences: dict[str, set[int]] = {}
    for line in margin:
        occurrences.setdefault(signature(line), set()).add(line.page)
    repeated = {key for key, pages in occurrences.items() if len(pages) >= max(2, (page_count + 1) // 2)}
    return [line for line in lines if not (
        line in margin and not _is_reference_heading(line.text)
        and (re.fullmatch(r"\s*(?:\d+|[ivx]+)\s*", line.text, re.I)
             or signature(line) in repeated)
    )]


def _outline_reference_page(reader: Any) -> int | None:
    def visit(nodes: list[Any]) -> int | None:
        for node in nodes:
            if isinstance(node, list):
                result = visit(node)
                if result is not None:
                    return result
            elif _is_reference_heading(getattr(node, "title", "")):
                page = reader.get_destination_page_number(node)
                if page is not None and page >= 0:
                    return page + 1
        return None
    try:
        return visit(reader.outline)
    except Exception:
        return None


def _find_reference_section(lines: list[_Line], reader: Any, warnings: list[str]) -> tuple[int | None, bool, str]:
    outline_page = _outline_reference_page(reader)
    headings = [index for index, line in enumerate(lines) if _is_reference_heading(line.text)]
    if headings:
        matching = [index for index in headings if outline_page == lines[index].page]
        start = (matching or headings)[0] + 1
        return start, False, "outline-and-heading" if matching else "heading"
    if outline_page:
        for index, line in enumerate(lines):
            if line.page >= outline_page and (_label(line.text) or _author_year_start(line.text)):
                _warning(warnings, "reference_section_from_outline: text heading unavailable; verify the inferred boundary.")
                return index, True, "outline"
    # A missing heading is not evidence that the article has no references.
    # Require two numbered entries with bibliographic evidence near the end.
    minimum_page = max(1, int(len(reader.pages) * .45))
    possible = [(index, line) for index, line in enumerate(lines)
                if line.page >= minimum_page and _label(line.text)]
    for offset, (index, line) in enumerate(possible[:-1]):
        next_index, next_line = possible[offset + 1]
        if next_line.page > line.page + 1 or next_index - index > 30:
            continue
        text = " ".join(item.text for item in lines[index:next_index + 1])
        if _YEAR.search(text) or normalize_doi(text) or normalize_arxiv(text):
            _warning(warnings, "reference_section_inferred: numbered entries without a heading require review.")
            return index, True, "numbered-fallback"
    _warning(warnings, "reference_section_not_found: specify a section or enter references manually.")
    return None, True, "not-found"


def _document_metadata(reader: Any, lines: list[_Line], section_start: int | None,
                       warnings: list[str]) -> tuple[dict[str, Any], dict[str, str]]:
    result, sources = _metadata(), {}
    info = reader.metadata or {}
    title = _clean(info.get("/Title"))
    if title and title.casefold() not in _GENERIC_TITLES and not title.casefold().startswith("microsoft word"):
        result["title"], sources["title"] = title, "pdf-info"
    result["authors"] = _authors(info.get("/Author"))
    if result["authors"]:
        sources["authors"] = "pdf-info"
    identifier_values = [str(info.get(key) or "") for key in ("/DOI", "/doi", "/Identifier")]
    try:
        xmp = reader.xmp_metadata
        if xmp:
            titles = getattr(xmp, "dc_title", None) or {}
            if titles:
                chosen = titles.get("x-default") or next(iter(titles.values()), "")
                if _clean(chosen).casefold() not in _GENERIC_TITLES and _clean(chosen):
                    result["title"], sources["title"] = _clean(chosen), "xmp-dc-title"
            creators = getattr(xmp, "dc_creator", None)
            if creators:
                result["authors"], sources["authors"] = _authors(creators), "xmp-dc-creator"
            identifiers = getattr(xmp, "dc_identifier", None) or []
            identifier_values.extend(identifiers if isinstance(identifiers, list) else [str(identifiers)])
    except Exception as exc:
        _warning(warnings, f"xmp_unreadable: {type(exc).__name__}; PDF Info and page text remain available.")
    body = lines[:max(0, (section_start - 1))] if section_start is not None else lines
    first_page = [line for line in body if line.page == 1]
    body_heading = next((index for index, line in enumerate(first_page) if _BODY_HEADING.match(_clean(line.text))), len(first_page))
    header = first_page[:body_heading]
    if body_heading == len(first_page):
        header = [line for line in first_page[:18] if line.bbox is None or line.bbox[1] > line.height * .65]
    footer = [line for line in first_page if line.bbox and line.bbox[3] < line.height * .13]
    identifier_lines = [line for line in header + footer if not _label(line.text)]
    for scheme, normalizer in (("doi", normalize_doi), ("arxiv", normalize_arxiv)):
        metadata_ids = {identifier for value in identifier_values if (identifier := normalizer(value))}
        page_ids = {identifier for line in identifier_lines
                    if (scheme == "arxiv" or re.search(r"doi\s*:|doi\.org/", line.text, re.I))
                    and (identifier := normalizer(line.text))}
        selected = metadata_ids or page_ids
        if len(selected) == 1:
            result[scheme] = next(iter(selected))
            sources[scheme] = "pdf-metadata" if metadata_ids else "first-page-header-or-footer"
        elif len(selected) > 1:
            _warning(warnings, f"ambiguous_document_{scheme}: multiple identifiers; no identifier selected.")
    if not result["title"]:
        candidates = [line for line in header if 8 <= len(_clean(line.text)) <= 300
                      and not _is_reference_heading(line.text) and not _BODY_HEADING.match(line.text)
                      and not re.search(r"doi\s*:|https?://|arxiv|university|department|@", line.text, re.I)
                      and not re.search(r"\b[A-Z]\.\s+[A-Z][a-z]", line.text)]
        if candidates:
            chosen = max(candidates, key=lambda line: (line.font_size, -first_page.index(line)))
            position = header.index(chosen)
            title_lines = [chosen]
            for extra in header[position + 1:position + 3]:
                if not chosen.font_size or abs(extra.font_size - chosen.font_size) > .6 or _looks_like_authors(extra.text):
                    break
                if chosen.bbox and extra.bbox and title_lines[-1].bbox[1] - extra.bbox[3] > chosen.font_size * 1.6:
                    break
                if _usable_title(extra.text):
                    title_lines.append(extra)
            result["title"] = _clean(" ".join(line.text for line in title_lines))
            sources["title"] = "first-page-title-candidate"
            if not result["authors"]:
                following = header[position + len(title_lines):position + len(title_lines) + 3]
                names = [line.text for line in following if _looks_like_authors(line.text)]
                if names:
                    result["authors"] = _authors("; ".join(names))
                    sources["authors"] = "first-page-author-candidate"
    years: set[int] = set()
    for line in header + footer:
        if re.search(r"published|copyright|proceedings|conference|journal|©|出版", line.text, re.I):
            years.update(int(match.group(1)) for match in _YEAR.finditer(line.text))
    if len(years) == 1:
        result["year"], sources["year"] = next(iter(years)), "first-page-publication-context"
    return result, sources


def _reference_entries(lines: list[_Line], start: int, uncertain: bool,
                       warnings: list[str]) -> tuple[list[dict[str, Any]], bool]:
    section: list[_Line] = []
    for line in lines[start:]:
        if len(line.text) <= 100 and _STOP_HEADING.match(_clean(line.text)):
            break
        if not _is_reference_heading(line.text):
            section.append(line)
    numbered = any(_label(line.text) for line in section)
    groups: list[list[_Line]] = []
    current: list[_Line] = []
    unassigned: list[_Line] = []
    for line in section:
        starts = bool(_label(line.text)) if numbered else _author_year_start(line.text)
        if starts:
            if current:
                groups.append(current)
            current = [line]
        elif current:
            current.append(line)
        else:
            unassigned.append(line)
    if current:
        groups.append(current)
    if not groups and section:
        groups = [section]
        uncertain = True
        _warning(warnings, "reference_boundaries_unknown: section retained as one reviewable block.")
    elif unassigned:
        uncertain = True
        _warning(warnings, "reference_preamble_unassigned: text preceding the first detected entry requires review.")
        # Retain unassigned text as a separate block rather than losing it.
        groups.insert(0, unassigned)
    references: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for ordinal, group in enumerate(groups, 1):
        raw = _join_reference(group)
        label = _label(group[0].text)
        entry_uncertain = uncertain
        if label and label[0] in seen_labels:
            entry_uncertain = True
            _warning(warnings, "duplicate_reference_labels: verify bibliography segmentation.")
        if label:
            seen_labels.add(label[0])
        parsed = parse_reference_text(raw)
        if entry_uncertain:
            parsed["_needs_review"] = True
        locations = []
        for line in group:
            location: dict[str, Any] = {"page": line.page}
            if line.bbox:
                location["bbox"] = [round(value, 3) for value in line.bbox]
                location["coordinate_system"] = "pdf-points-bottom-left"
            if location not in locations:
                locations.append(location)
        fingerprint = hashlib.sha256(normalize_title(raw).encode("utf-8")).hexdigest()
        references.append({"ordinal": ordinal, "label": label[0] if label else None,
                           "raw_text": raw, "parsed": parsed, "locations": locations,
                           "fingerprint": fingerprint})
    return references, uncertain or any(ref["parsed"].get("_needs_review") for ref in references)


def _python_rules(path: Path, reader: Any, deadline: float) -> dict[str, Any]:
    warnings: list[str] = []
    lines = _extract_lines(path, reader, deadline, warnings)
    raw_pages = [{"page": page, "text": "\n".join(line.text for line in lines if line.page == page)}
                 for page in range(1, len(reader.pages) + 1)]
    lines = _remove_running_headers(lines, len(reader.pages))
    text_size = sum(sum(c.isalnum() for c in line.text) for line in lines)
    start, uncertain, method = _find_reference_section(lines, reader, warnings) if text_size >= 12 else (None, True, "no-text")
    metadata, sources = _document_metadata(reader, lines, start, warnings)
    references: list[dict[str, Any]] = []
    if text_size < 12:
        status = "needs_ocr"
        _warning(warnings, "no_extractable_text: OCR or manual input is required; absence of text does not mean absence of references.")
    else:
        if start is not None:
            references, uncertain = _reference_entries(lines, start, uncertain, warnings)
        if not references:
            _warning(warnings, "no_reference_entries: automatic extraction is incomplete, not proof of an empty bibliography.")
        degraded = any(message.startswith(("layout_extraction_failed", "page_extraction_failed")) for message in warnings)
        status = "partial" if uncertain or not references or degraded else "succeeded"
    if not metadata.get("title"):
        _warning(warnings, "document_title_missing: review the document metadata.")
    _check_time(deadline)
    versions = []
    for package in ("pypdf", "pdfminer.six"):
        try:
            versions.append(f"{package}={importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"status": status, "metadata": metadata, "metadata_sources": sources,
            "references": references, "warnings": warnings, "page_count": len(reader.pages),
            "engine": "python-rules", "engine_version": PARSER_VERSION + ";" + ";".join(versions),
            "raw_output": {"format": "page-text", "pages": raw_pages, "reference_section_method": method}}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise ParseError("grobid_redirect_refused", "The local GROBID service attempted to redirect the PDF request.")


def _grobid_base(url: str) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.casefold() == "localhost"
    if parsed.scheme not in {"http", "https"} or not loopback or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ParseError("grobid_not_local", "grobid-local accepts only a loopback HTTP(S) service URL.")
    return url.rstrip("/")


def _multipart(path: Path, parameters: dict[str, str]) -> tuple[bytes, str]:
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ParseError("grobid_input_too_large", "The local GROBID adapter accepts PDFs up to 128 MiB.")
    boundary = "reference-topology-" + uuid.uuid4().hex
    body: list[bytes] = []
    for key, value in parameters.items():
        body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="input"; filename="document.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode())
    body.extend([path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
    return b"".join(body), f"multipart/form-data; boundary={boundary}"


def _xml_text(node: ET.Element | None) -> str:
    return _clean("".join(node.itertext())) if node is not None else ""


def _tei_metadata(node: ET.Element | None) -> dict[str, Any]:
    result = _metadata()
    if node is None:
        return result
    analytic = node.find("t:analytic", _TEI)
    monogr = node.find("t:monogr", _TEI)
    main = analytic if analytic is not None else monogr
    if main is None:
        main = node
    result["title"] = _xml_text(main.find("t:title", _TEI)) or None
    if monogr is not None and analytic is not None:
        result["venue"] = _xml_text(monogr.find("t:title", _TEI)) or None
    for author in main.findall("t:author", _TEI):
        name = author.find("t:persName", _TEI)
        if name is not None:
            display = " ".join(_xml_text(piece) for piece in list(name) if _xml_text(piece))
        else:
            display = _xml_text(author)
        if display:
            result["authors"].append({"display_name": display})
    for identifier in node.findall(".//t:idno", _TEI):
        kind, value = identifier.get("type", "").casefold(), _xml_text(identifier)
        if kind == "doi":
            result["doi"] = normalize_doi(value)
        elif kind == "arxiv":
            result["arxiv"] = normalize_arxiv(value)
    date = node.find(".//t:imprint/t:date", _TEI)
    if date is not None:
        match = _YEAR.search(date.get("when", "") or _xml_text(date))
        if match:
            result["year"] = int(match.group(1))
    for scope in node.findall(".//t:biblScope", _TEI):
        unit = scope.get("unit")
        if unit in {"volume", "issue"}:
            result[unit] = _xml_text(scope) or None
        elif unit == "page":
            result["pages"] = _xml_text(scope) or "-".join(filter(None, [scope.get("from"), scope.get("to")])) or None
    return result


def _grobid(path: Path, reader: Any, base_url: str, deadline: float) -> dict[str, Any]:
    base = _grobid_base(base_url)
    parameters = {"consolidateHeader": "0", "consolidateCitations": "0", "consolidateFunders": "0",
                  "includeRawCitations": "1", "teiCoordinates": "biblStruct"}
    body, content_type = _multipart(path, parameters)
    opener = build_opener(_NoRedirect())
    request = Request(base + "/api/processFulltextDocument", data=body,
                      headers={"Content-Type": content_type, "Accept": "application/xml"}, method="POST")
    warnings: list[str] = []
    try:
        with opener.open(request, timeout=max(.1, deadline - time.monotonic())) as response:
            if response.status == 204:
                raise ParseError("grobid_no_content", "GROBID returned no structured content; OCR or manual review may be needed.")
            data = response.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ParseError("grobid_output_too_large", "GROBID output exceeded 64 MiB.")
    except HTTPError as exc:
        raise ParseError("grobid_http_error", f"Local GROBID returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ParseError("grobid_unavailable", f"Local GROBID request failed ({type(exc).__name__}).") from exc
    _check_time(deadline)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ParseError("grobid_unsafe_xml", "GROBID XML declarations containing entities are not accepted.")
    try:
        tree = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ParseError("grobid_invalid_xml", "GROBID did not return valid TEI XML.") from exc
    if tree.tag != "{http://www.tei-c.org/ns/1.0}TEI":
        raise ParseError("grobid_invalid_tei", "GROBID response lacks the expected TEI namespace and root.")
    header = tree.find("t:teiHeader/t:fileDesc/t:sourceDesc/t:biblStruct", _TEI)
    metadata = _tei_metadata(header)
    if not metadata.get("title"):
        metadata["title"] = _xml_text(tree.find("t:teiHeader/t:fileDesc/t:titleStmt/t:title", _TEI)) or None
    references = []
    for index, node in enumerate(tree.findall("t:text/t:back//t:listBibl/t:biblStruct", _TEI), 1):
        raw_node = node.find("t:note[@type='raw_reference']", _TEI)
        raw = "".join(raw_node.itertext()).strip() if raw_node is not None else ""
        parsed = _tei_metadata(node)
        if not raw:
            raw = _xml_text(node)
            parsed["_needs_review"] = True
            _warning(warnings, "grobid_raw_reference_missing: structured text retained, but original reference text needs review.")
        locations = []
        for fragment in node.get("coords", "").split(";"):
            try:
                page, x, y, width, height = [float(value) for value in fragment.split(",")]
                page_number = int(page)
                if (not all(math.isfinite(value) for value in (page, x, y, width, height))
                        or page != page_number or not 1 <= page_number <= len(reader.pages) or width < 0 or height < 0):
                    continue
                page_height = float(reader.pages[page_number - 1].mediabox.height)
                locations.append({"page": page_number, "bbox": [x, page_height - y - height, x + width, page_height - y],
                                  "coordinate_system": "pdf-points-bottom-left"})
            except (ValueError, TypeError):
                continue
        references.append({"ordinal": index, "label": node.get("n"), "raw_text": raw,
                           "parsed": parsed, "locations": locations,
                           "fingerprint": hashlib.sha256(normalize_title(raw).encode()).hexdigest()})
    version = "unknown"
    try:
        _check_time(deadline)
        with opener.open(base + "/api/version", timeout=min(3.0, max(.1, deadline - time.monotonic()))) as response:
            version = response.read(4096).decode("utf-8", "replace").strip() or "unknown"
    except (URLError, TimeoutError, OSError, ParseError):
        _warning(warnings, "grobid_version_unavailable: adapter version and parameters are retained.")
    if not references:
        _warning(warnings, "no_reference_entries: GROBID did not identify a bibliography; review required.")
    status = "partial" if not references or any(ref["parsed"].get("_needs_review") for ref in references) else "succeeded"
    return {"status": status, "metadata": metadata, "metadata_sources": {key: "grobid-tei-header" for key, value in metadata.items() if value},
            "references": references, "warnings": warnings, "page_count": len(reader.pages),
            "engine": "grobid-local", "engine_version": f"{GROBID_ADAPTER_VERSION};service={version}",
            "raw_output": {"format": "tei", "text": data.decode("utf-8", "replace"), "parameters": parameters, "service_version": version}}


def parse_pdf(path: Path, engine: str = "python-rules", grobid_url: str = "http://127.0.0.1:8070",
              timeout: float = 120, max_pages: int = 1000) -> dict[str, Any]:
    """Parse a local file into metadata, reviewable entries and page evidence.

    Operational failures raise :class:`ParseError`; an incomplete bibliography
    returns ``partial`` and an image-only document returns ``needs_ocr``. Call in
    a worker process to enforce a hard timeout around third-party PDF libraries.
    """
    from pypdf import PdfReader

    path = Path(path)
    if engine not in {"python-rules", "grobid-local"}:
        raise ParseError("unknown_engine", "Unsupported PDF parsing engine.")
    if not math.isfinite(timeout) or timeout <= 0 or max_pages < 1:
        raise ParseError("invalid_limits", "Timeout and page limit must be positive.")
    if not path.is_file():
        raise ParseError("file_not_found", "The selected PDF file is not available.")
    deadline = time.monotonic() + timeout
    try:
        with path.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise ParseError("invalid_pdf", "The selected file does not contain a PDF header.")
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("encrypted_pdf", "Password-protected PDF files need an unlocked copy.")
        page_count = len(reader.pages)
        if page_count < 1:
            raise ParseError("empty_pdf", "The PDF contains no pages.")
        if page_count > max_pages:
            raise ParseError("page_limit_exceeded", f"PDF has {page_count} pages; configured limit is {max_pages}.")
        _check_time(deadline)
        if engine == "grobid-local":
            return _grobid(path, reader, grobid_url, deadline)
        return _python_rules(path, reader, deadline)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("pdf_parse_failed", f"PDF processing failed ({type(exc).__name__}).") from exc

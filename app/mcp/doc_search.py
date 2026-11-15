"""Search inside one documentation site using the index the site itself publishes.

There is no general web search. Each configured source names an index:

- ``sphinx``: ``searchindex.js`` written by Sphinx (``Search.setIndex({...})``).
  Searched like Sphinx's own search page: API object names, section titles,
  and (stemmed) words in titles and pages.
- ``mkdocs``: ``search/search_index.json`` written by MkDocs (title + text per
  page section).
- ``llms_txt``: an ``llms.txt`` file, a Markdown list of ``[title](url): note``
  links that many documentation sites publish for tools like this one.

Sphinx stores stemmed words ("rotation" -> "rotat"). Instead of a full Porter
stemmer, a query word matches an index term when one is a prefix of the other
(with a minimum length), which covers the common suffixes.
"""

from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin

STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how", "i", "if", "in", "into", "is", "it", "of", "on", "or", "should", "the", "to", "use", "using", "what", "when", "where", "which", "why", "with"]
)
MAX_QUERY_LENGTH = 200


class IndexFormatError(Exception):
    """The index could not be parsed."""


@dataclass
class SearchResult:
    title: str
    url: str
    kind: str  # api | section | page
    snippet: str
    score: float


def query_words(query: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9_]+", query.lower()) if w not in STOPWORDS]
    return list(dict.fromkeys(words))


def _word_pattern(word: str) -> re.Pattern[str]:
    """Matches ``word`` at the start of a word, allowing common suffixes ("token" ~ "tokens")."""
    stem = word if len(word) <= 4 else word[: max(4, len(word) - 3)]
    return re.compile(r"(?<![a-z0-9_])" + re.escape(stem))


def _rank(scored: dict[str, tuple[float, int]], needed: int) -> list[tuple[str, float]]:
    """Sort by (matched every word, score). ``scored`` maps key -> (score, words matched)."""
    items = sorted(scored.items(), key=lambda kv: (kv[1][1] >= needed, kv[1][1], kv[1][0]), reverse=True)
    return [(key, value[0]) for key, value in items]


def _snippet(text: str, patterns: list[re.Pattern[str]], width: int = 220) -> str:
    low = text.lower()
    positions = [m.start() for p in patterns if (m := p.search(low))]
    if not positions:
        return text[:width].strip() + ("..." if len(text) > width else "")
    start = max(min(positions) - 60, 0)
    piece = text[start : start + width].strip()
    return ("..." if start else "") + piece + ("..." if start + width < len(text) else "")


# ---------------------------------------------------------------------- Sphinx
class SphinxIndex:
    def __init__(self, data: dict[str, Any], base_url: str, page_suffix: str = ".html") -> None:
        for key in ("docnames", "titles", "terms"):
            if key not in data:
                raise IndexFormatError(f"Sphinx index has no {key!r}")
        self.base_url = base_url
        self.page_suffix = page_suffix
        self.docnames: list[str] = data["docnames"]
        self.titles: list[str] = data["titles"]
        self.terms: dict[str, Any] = data.get("terms") or {}
        self.titleterms: dict[str, Any] = data.get("titleterms") or {}
        self.alltitles: dict[str, list] = data.get("alltitles") or {}
        self.objnames: dict[str, list] = data.get("objnames") or {}
        self.objects = list(self._flatten_objects(data.get("objects") or {}))
        self._sorted_terms = sorted(self.terms)
        self._sorted_titleterms = sorted(self.titleterms)

    @classmethod
    def parse(cls, text: str, base_url: str, page_suffix: str = ".html") -> SphinxIndex:
        start, end = text.find("("), text.rfind(")")
        if start < 0 or end <= start:
            raise IndexFormatError("not a Sphinx searchindex.js file")
        try:
            data = json.loads(text[start + 1 : end])
        except json.JSONDecodeError as exc:
            raise IndexFormatError(f"unsupported searchindex.js format (expected JSON inside Search.setIndex): {exc}") from exc
        if not isinstance(data, dict):
            raise IndexFormatError("unsupported searchindex.js format")
        return cls(data, base_url, page_suffix)

    def _flatten_objects(self, objects: dict[str, Any]):
        """Yield (fullname, docindex, objtype index, priority, anchor) for both index layouts."""
        for prefix, entries in objects.items():
            if isinstance(entries, list):  # Sphinx >= 5: [doc, type, prio, anchor, name]
                for entry in entries:
                    if isinstance(entry, list) and len(entry) >= 5:
                        doc, typ, prio, anchor, name = entry[:5]
                        yield (f"{prefix}.{name}" if prefix else str(name)), doc, typ, prio, anchor
            elif isinstance(entries, dict):  # older: {name: [doc, type, prio, anchor]}
                for name, entry in entries.items():
                    if isinstance(entry, list) and len(entry) >= 4:
                        doc, typ, prio, anchor = entry[:4]
                        yield (f"{prefix}.{name}" if prefix else str(name)), doc, typ, prio, anchor

    def page_url(self, doc: int, anchor: str | None = None) -> str:
        name = self.docnames[doc]
        if self.page_suffix == "/":
            page = "" if name == "index" else (name[: -len("/index")] + "/" if name.endswith("/index") else name + "/")
        else:
            page = name + self.page_suffix
        url = urljoin(self.base_url, page)
        return f"{url}#{anchor}" if anchor else url

    def _docs_for(self, mapping: dict[str, Any], sorted_keys: list[str], word: str) -> dict[int, bool]:
        """Documents whose index terms match ``word``. Value: True for an exact match."""
        found: dict[int, bool] = {}

        def add(entry: Any, exact: bool) -> None:
            for doc in entry if isinstance(entry, list) else [entry]:
                if isinstance(doc, int):
                    found[doc] = found.get(doc, False) or exact

        if word in mapping:
            add(mapping[word], True)
        # index terms that are a stem of the word ("rotat" for "rotation")
        for size in range(max(4, len(word) - 4), len(word)):
            if word[:size] in mapping:
                add(mapping[word[:size]], True)
        # index terms that extend the word ("tokens" for "token")
        if len(word) >= 4:
            i = bisect.bisect_left(sorted_keys, word)
            for key in sorted_keys[i : i + 200]:
                if not key.startswith(word):
                    break
                add(mapping[key], False)
        return found

    def search(self, query: str, limit: int) -> list[SearchResult]:
        words = query_words(query)
        if not words:
            return []
        results: dict[str, SearchResult] = {}
        q = " ".join(query.lower().split())

        # 1. API objects ("secrets.compare_digest", "pytest.raises"). For a multi-word query the
        #    score is scaled by how many of the words appear in the object's name or page title.
        object_terms = {t for t in [q.replace(" ", ""), *q.split()] if len(t) >= 3}
        patterns = [_word_pattern(w) for w in words]
        for fullname, doc, typ, prio, anchor in self.objects:
            low = fullname.lower()
            last = low.rsplit(".", 1)[-1]
            score, identifier = 0.0, False
            for term in object_terms:
                value = 30 if (low == term or last == term) else 12 if term in last else 8 if (term in low and "." in term) else 0
                if value > score:
                    score, identifier = value, ("." in term or "_" in term)
            if not score or not isinstance(doc, int) or doc >= len(self.docnames):
                continue
            score += {0: 6, 1: 2, 2: -2}.get(prio, 0) if isinstance(prio, int) else 0
            if len(words) > 1:
                context = f"{low} {self.titles[doc].lower()}"
                coverage = sum(1 for p in patterns if p.search(context)) / len(words)
                # an identifier the user typed ("subprocess.run") stays relevant; one plain word does not
                score *= (0.5 + 0.5 * coverage) if identifier else coverage
            objname = self.objnames.get(str(typ), ["", "", "object"])
            anchor_text = fullname if anchor == "" else (f"{objname[1]}-{fullname}" if anchor == "-" else anchor)
            url = self.page_url(doc, anchor_text)
            result = SearchResult(fullname, url, "api", f"{objname[2] if len(objname) > 2 else 'object'} in \"{self.titles[doc]}\"", score)
            if url not in results or results[url].score < score:
                results[url] = result

        # 2. Section titles
        for title, places in self.alltitles.items():
            low = title.lower()
            matched = sum(1 for p in patterns if p.search(low))
            if matched < len(words) or not isinstance(places, list):
                continue
            for place in places[:3]:
                if not isinstance(place, list) or not place or not isinstance(place[0], int) or place[0] >= len(self.docnames):
                    continue
                doc, anchor = place[0], (place[1] if len(place) > 1 else None)
                url = self.page_url(doc, anchor or None)
                score = 20 + (5 if low == q else 0) - min(len(low) / 40, 3)
                if url not in results or results[url].score < score:
                    page_title = self.titles[doc]
                    results[url] = SearchResult(title, url, "section" if anchor else "page", f"section of \"{page_title}\"" if anchor else "page", score)

        # 3. Words in page titles and text
        scored: dict[int, list[float]] = {}
        for word in words:
            per_doc: dict[int, float] = {}
            for doc, exact in self._docs_for(self.terms, self._sorted_terms, word).items():
                per_doc[doc] = max(per_doc.get(doc, 0), 5 if exact else 2)
            for doc, exact in self._docs_for(self.titleterms, self._sorted_titleterms, word).items():
                per_doc[doc] = max(per_doc.get(doc, 0), 15 if exact else 7)
            for doc, value in per_doc.items():
                entry = scored.setdefault(doc, [0.0, 0])
                entry[0] += value
                entry[1] += 1
        ranked = _rank({str(doc): (s, n) for doc, (s, n) in scored.items()}, len(words))
        for key, score in ranked[: limit * 3]:
            doc = int(key)
            if doc >= len(self.docnames):
                continue
            matched_all = scored[doc][1] >= len(words)
            url = self.page_url(doc)
            score = score / max(len(words), 1) + (10 if matched_all else -5)
            if url not in results:
                results[url] = SearchResult(self.titles[doc], url, "page", "page" if matched_all else "page (matches some of the words)", score)

        return sorted(results.values(), key=lambda r: r.score, reverse=True)[:limit]


# ---------------------------------------------------------------------- MkDocs
class MkDocsIndex:
    def __init__(self, docs: list[dict[str, Any]], base_url: str) -> None:
        self.base_url = base_url
        self.entries: list[tuple[str, str, str, str]] = []  # (title, location, text, lowercase blob)
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            title = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", str(doc.get("title") or "")))).strip()
            text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", str(doc.get("text") or "")))).strip()
            self.entries.append((title, str(doc.get("location") or ""), text, f"{title}\n{text}".lower()))

    @classmethod
    def parse(cls, text: str, base_url: str) -> MkDocsIndex:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IndexFormatError(f"not a MkDocs search_index.json file: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
            raise IndexFormatError("MkDocs index has no 'docs' list")
        return cls(data["docs"], base_url)

    def search(self, query: str, limit: int) -> list[SearchResult]:
        words = query_words(query)
        if not words:
            return []
        patterns = [_word_pattern(w) for w in words]
        scored: dict[str, tuple[float, int]] = {}
        for i, (title, _location, _text, blob) in enumerate(self.entries):
            title_low = title.lower()
            score, matched = 0.0, 0
            for p in patterns:
                in_title = bool(p.search(title_low))
                hits = len(p.findall(blob))
                if hits:
                    matched += 1
                    score += (10 if in_title else 0) + min(hits, 5)
            if matched:
                scored[str(i)] = (score, matched)
        results = []
        for key, score in _rank(scored, len(words))[:limit]:
            title, location, text, _ = self.entries[int(key)]
            url = urljoin(self.base_url, location)
            results.append(SearchResult(title or location or "(untitled)", url, "section" if "#" in location else "page", _snippet(text, patterns), score))
        return results


# ---------------------------------------------------------------------- llms.txt
_LINK = re.compile(r"^\s*[-*+]\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)\)\s*(?:[:\u2013\u2014-]\s*)?(?P<note>.*)$")


class LlmsTxtIndex:
    def __init__(self, entries: list[tuple[str, str, str, str]], base_url: str) -> None:
        self.entries = entries  # (title, url, note, section)
        self.base_url = base_url

    @classmethod
    def parse(cls, text: str, base_url: str) -> LlmsTxtIndex:
        entries: list[tuple[str, str, str, str]] = []
        section = ""
        for line in text.splitlines():
            heading = re.match(r"^#{2,6}\s+(.+)$", line)
            if heading:
                section = heading.group(1).strip()
                continue
            match = _LINK.match(line)
            if match:
                entries.append((match["title"].strip(), urljoin(base_url, match["url"]), (match["note"] or "").strip(), section))
        if not entries:
            raise IndexFormatError("llms.txt contains no [title](url) links")
        return cls(entries, base_url)

    def search(self, query: str, limit: int) -> list[SearchResult]:
        words = query_words(query)
        if not words:
            return []
        patterns = [_word_pattern(w) for w in words]
        scored: dict[str, tuple[float, int]] = {}
        for i, (title, url, note, section) in enumerate(self.entries):
            fields = ((title.lower(), 10), (note.lower(), 3), (section.lower(), 2), (url.lower().replace("-", " ").replace("/", " "), 2))
            score, matched = 0.0, 0
            for p in patterns:
                best = max((weight for text, weight in fields if p.search(text)), default=0)
                if best:
                    matched += 1
                    score += best
            if matched:
                scored[str(i)] = (score, matched)
        results = []
        for key, score in _rank(scored, len(words))[:limit]:
            title, url, note, section = self.entries[int(key)]
            snippet = note or (f"in section \"{section}\"" if section else "")
            results.append(SearchResult(title, url, "page", snippet[:240], score))
        return results


def parse_index(kind: str, text: str, base_url: str, page_suffix: str = ".html"):
    if kind == "sphinx":
        return SphinxIndex.parse(text, base_url, page_suffix)
    if kind == "mkdocs":
        return MkDocsIndex.parse(text, base_url)
    if kind == "llms_txt":
        return LlmsTxtIndex.parse(text, base_url)
    raise IndexFormatError(f"unknown index type {kind!r}")

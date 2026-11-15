"""Turn a documentation page into compact, readable text (standard library only).

- Keeps the main content: ``[role=main]``, else the first ``<article>``, else
  ``<main>``, else ``<body>``.
- Drops scripts, styles, forms, navigation, sidebars and hidden elements.
- Keeps structure that matters for docs: headings (``#``), lists, code blocks
  (fenced), inline code (backticks), tables (``|``-separated) and definition
  lists (API signatures in Sphinx pages).
- Records the text offset of every element ``id`` so a URL ``#fragment`` can
  jump straight to that section, and collects links for navigation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
    "form", "button", "input", "select", "textarea", "dialog", "head", "nav", "aside", "footer",
}
SKIP_CLASSES = {
    "headerlink", "sphinxsidebar", "related", "md-sidebar", "md-header", "md-footer", "md-source-file",
    "md-top", "toc-drawer", "visually-hidden", "sr-only", "skip-link", "copybtn", "linenos", "toctree-wrapper",
}
BLOCK = {
    "p", "div", "section", "article", "main", "header", "figure", "figcaption", "blockquote", "details",
    "summary", "address", "dl", "center", "body", "html",
}
CLOSES_P = BLOCK | {"h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre", "table", "hr", "li", "dt", "dd"}
MAX_DEPTH = 150


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.parent = parent

    def find_first(self, match) -> Node | None:
        stack: list[Node] = [self]
        while stack:  # depth-first, document order
            node = stack.pop()
            if match(node):
                return node
            stack.extend(reversed([c for c in node.children if isinstance(c, Node)]))
        return None

    def text(self) -> str:
        out: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                out.append(item)
            elif item.tag not in SKIP:
                stack.extend(reversed(item.children))
        return "".join(out)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.stack: list[Node] = [self.root]

    @property
    def top(self) -> Node:
        return self.stack[-1]

    def _close_until(self, tags: set[str], boundary: set[str]) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[i].tag
            if tag in boundary:
                return
            if tag in tags:
                del self.stack[i:]
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self._close_until({"li"}, {"ul", "ol"})
        elif tag in ("dt", "dd"):
            self._close_until({"dt", "dd"}, {"dl"})
        elif tag == "tr":
            self._close_until({"tr"}, {"table", "thead", "tbody", "tfoot"})
        elif tag in ("td", "th"):
            self._close_until({"td", "th"}, {"tr", "table"})
        elif tag == "option":
            self._close_until({"option"}, {"select"})
        if tag in CLOSES_P and self.top.tag == "p":
            self.stack.pop()
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.top)
        self.top.children.append(node)
        if tag not in VOID and len(self.stack) < MAX_DEPTH:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.top.tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.top.children.append(data)


@dataclass
class Document:
    title: str
    text: str
    anchors: dict[str, int] = field(default_factory=dict)
    links: list[tuple[str, str, int]] = field(default_factory=list)  # (label, absolute URL, text offset)


class _Writer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.tail = ""

    def _append(self, s: str) -> None:
        self.parts.append(s)
        self.length += len(s)
        self.tail = (self.tail + s)[-2:]

    def _trim_trailing_spaces(self) -> None:
        while self.parts and self.parts[-1].endswith(" "):
            stripped = self.parts[-1].rstrip(" ")
            self.length -= len(self.parts[-1]) - len(stripped)
            self.parts[-1] = stripped
            if not stripped:
                self.parts.pop()
        self.tail = "".join(self.parts[-2:])[-2:]

    def text(self, s: str) -> None:
        if not s:
            return
        if (self.length == 0 or self.tail.endswith("\n") or self.tail.endswith(" ")) and s.startswith(" "):
            s = s.lstrip(" ")
        if s:
            self._append(s)

    def raw(self, s: str) -> None:
        if s:
            self._append(s)

    def newline(self) -> None:
        if self.length == 0:
            return
        self._trim_trailing_spaces()
        if not self.tail.endswith("\n"):
            self._append("\n")

    def blank_line(self) -> None:
        if self.length == 0:
            return
        self.newline()
        if not self.tail.endswith("\n\n"):
            self._append("\n")

    def value(self) -> str:
        return "".join(self.parts).strip()


class _Renderer:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.out = _Writer()
        self.anchors: dict[str, int] = {}
        self.links: list[tuple[str, str, int]] = []
        self.list_stack: list[list[int]] = []  # [ordered(0/1), counter]
        self.skip_header = False

    def skipped(self, node: Node) -> bool:
        if node.tag in SKIP or (node.tag == "header" and self.skip_header):
            return True
        if "hidden" in node.attrs or node.attrs.get("aria-hidden") == "true":
            return True
        classes = set(node.attrs.get("class", "").split())
        return bool(classes & SKIP_CLASSES)

    def render(self, node: Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                self.out.text(re.sub(r"\s+", " ", child))
            else:
                self.element(child)

    def element(self, node: Node) -> None:
        if self.skipped(node):
            return
        tag = node.tag
        for key in ("id", "name") if tag == "a" else ("id",):
            anchor = node.attrs.get(key)
            if anchor and anchor not in self.anchors:
                self.anchors[anchor] = self.out.length
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.blank_line()
            self.out.raw("#" * int(tag[1]) + " ")
            self.render(node)
            self.out.blank_line()
        elif tag == "pre":
            self.out.blank_line()
            self.out.raw("```\n" + node.text().strip("\n") + "\n```")
            self.out.blank_line()
        elif tag == "code" or tag == "kbd" or tag == "samp":
            code = re.sub(r"\s+", " ", node.text()).strip()
            if code:
                self.out.raw(f"`{code}`")
        elif tag in ("ul", "ol"):
            self.out.newline() if self.list_stack else self.out.blank_line()
            self.list_stack.append([1 if tag == "ol" else 0, 0])
            self.render(node)
            self.list_stack.pop()
            self.out.newline() if self.list_stack else self.out.blank_line()
        elif tag == "li":
            self.out.newline()
            depth = max(len(self.list_stack) - 1, 0)
            marker = "- "
            if self.list_stack and self.list_stack[-1][0]:
                self.list_stack[-1][1] += 1
                marker = f"{self.list_stack[-1][1]}. "
            self.out.raw("  " * depth + marker)
            self.render(node)
            self.out.newline()
        elif tag == "dt":
            self.out.blank_line()
            self.render(node)
            self.out.newline()
        elif tag == "dd":
            self.out.newline()
            self.render(node)
            self.out.blank_line()
        elif tag == "table":
            self.out.blank_line()
            self.render(node)
            self.out.blank_line()
        elif tag == "tr":
            self.out.newline()
            cells = [c for c in node.children if isinstance(c, Node) and c.tag in ("td", "th")]
            self.out.raw(" | ".join(re.sub(r"\s+", " ", c.text()).strip() for c in cells))
            self.out.newline()
        elif tag == "br":
            self.out.newline()
        elif tag == "hr":
            self.out.blank_line()
            self.out.raw("---")
            self.out.blank_line()
        elif tag == "img":
            return
        elif tag == "a":
            href = node.attrs.get("href", "").strip()
            start = self.out.length
            self.render(node)
            label = re.sub(r"\s+", " ", node.text()).strip()
            if href and label and not href.startswith(("#", "javascript:", "mailto:")) and self.out.length > start:
                absolute = urljoin(self.base_url, href)
                if absolute.startswith(("https://", "http://")):
                    self.links.append((label[:120], absolute, start))
        elif tag in BLOCK or tag in ("thead", "tbody", "tfoot"):
            self.out.blank_line()
            self.render(node)
            self.out.blank_line()
        else:
            self.render(node)


def _main_content(root: Node) -> tuple[Node, bool]:
    """The node holding the page's main content, and whether it is a narrowed region."""
    for match in (
        lambda n: n.attrs.get("role") == "main",
        lambda n: n.tag == "article",
        lambda n: n.tag == "main",
    ):
        found = root.find_first(match)
        if found is not None:
            return found, True
    body = root.find_first(lambda n: n.tag == "body")
    return (body or root), False


def html_to_document(html: str, base_url: str) -> Document:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    root = builder.root
    title_node = root.find_first(lambda n: n.tag == "title")
    title = re.sub(r"\s+", " ", unescape(title_node.text() if title_node else "")).strip()
    main, narrowed = _main_content(root)
    renderer = _Renderer(base_url)
    renderer.skip_header = not narrowed  # page-level header is chrome; a header inside an article is content
    renderer.element(main) if main.tag != "#root" else renderer.render(main)
    text = renderer.out.value()
    if not title:
        heading = re.search(r"^# (.+)$", text, re.MULTILINE)
        title = heading.group(1).strip() if heading else ""
    seen: set[str] = set()
    links: list[tuple[str, str, int]] = []
    page = urldefrag(base_url).url
    for label, url, offset in renderer.links:
        target = urldefrag(url).url
        if target != page and target not in seen:
            seen.add(target)
            links.append((label, url, offset))
    return Document(title=title, text=text, anchors=renderer.anchors, links=links)


def slugify(heading: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", heading.lower()).strip()
    return re.sub(r"[\s]+", "-", slug)


def text_to_document(text: str, base_url: str) -> Document:
    """Markdown or plain text: keep as is; headings become anchors (GitHub-style slugs)."""
    text = text.replace("\r\n", "\n").strip()
    anchors: dict[str, int] = {}
    title = ""
    in_code = False
    for match in re.finditer(r"^.*$", text, re.MULTILINE):
        line = match.group(0)
        if line.startswith("```"):
            in_code = not in_code
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading and not in_code:
            anchors.setdefault(slugify(heading.group(2)), match.start())
            if not title and len(heading.group(1)) == 1:
                title = heading.group(2).strip()
    if not title and text.startswith("---\n"):
        end = text.find("\n---", 4)
        front = re.search(r"^title:\s*(.+)$", text[4:end] if end > 0 else "", re.MULTILINE)
        title = front.group(1).strip().strip("'\"") if front else ""
    links = [(m.group(1)[:120], urljoin(base_url, m.group(2)), m.start()) for m in re.finditer(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+|/[^)\s]*)\)", text)]
    return Document(title=title, text=text, anchors=anchors, links=links)

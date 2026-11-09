"""Content pruning: selectolax Pruning → markdownify.

Follows the blueprint crawl4ai PruningContentFilter idea (noise stripping +
dropping empty-text leaf nodes) plus the HtmlToMarkdown step of the
docs-mcp-server middleware chain. Pure functions, zero networking.
"""

from __future__ import annotations

from urllib.parse import urljoin

from markdownify import markdownify as md
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from libref.text.normalize import normalize_markdown

# Noise tags stripped outright
_STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "aside", "form", "header")
# Common main-content containers, picked by priority
_MAIN_CANDIDATES = ("main", "article")
# Preserved wholesale during pruning: table structure, code blocks, and img self-count as non-empty
_PRESERVE_TAGS = ("pre", "code", "table", "tbody", "thead", "tfoot", "tr", "td", "th", "img", "br", "hr")
# Suspected client-side rendering markers: static fetch yields no content and a hit counts as a skip, not a failure
_JS_MARKERS = ("id=\"root\"", "id=\"app\"", "__NEXT_DATA__", "__NUXT__", "ng-app", "data-reactroot", "__INITIAL_STATE__")


def extract_to_markdown(html: str, base_url: str | None = None) -> str:
    """HTML → main-content Markdown; return an empty string when nothing remains after pruning (empty outcome)."""
    tree = HTMLParser(html)
    if base_url:
        _absolutize_links(tree, base_url)
    for tag in _STRIP_TAGS:
        for node in list(tree.css(tag)):
            node.decompose()
    for node in list(tree.css('[role="navigation"]')):
        node.decompose()

    root = _pick_main_root(tree)
    _prune_empty_nodes(root)

    inner = root.html or ""
    markdown = md(inner, heading_style="ATX", strip=["img"])
    return normalize_markdown(markdown)


def _absolutize_links(tree: HTMLParser, base_url: str) -> None:
    for node in tree.css("a[href]"):
        href = node.attributes.get("href", "")
        if href and not href.startswith(("http://", "https://", "mailto:", "#", "data:")):
            node.attributes["href"] = urljoin(base_url, href)
    for node in tree.css("img[src]"):
        src = node.attributes.get("src", "")
        if src and not src.startswith(("http://", "https://", "data:")):
            node.attributes["src"] = urljoin(base_url, src)


def looks_like_js_rendered(html: str, markdown: str) -> bool:
    """Treat as a rendered page when static extraction yields no content and a rendering marker is present; the caller counts a skip."""
    if len(markdown.strip()) >= 200:
        return False
    lowered = html.lower()
    return any(marker.lower() in lowered for marker in _JS_MARKERS)


def _pick_main_root(tree: HTMLParser):
    for tag in _MAIN_CANDIDATES:
        nodes = tree.css(tag)
        if nodes:
            return max(nodes, key=lambda n: len(n.text(strip=True)))
    dense = _densest_container(tree)
    if dense is not None:
        return dense
    body = tree.css_first("body")
    return body if body is not None else tree.root


def _densest_container(tree: HTMLParser):
    """Fall back to the densest block container when candidates are missing, avoiding picking the whole page on nav-heavy pages."""
    best = None
    best_score = -1.0
    for node in tree.css("div,section,article,main"):
        text = node.text(strip=True) or ""
        length = len(text)
        if length < 50:
            continue
        tags = sum(1 for _ in node.iter()) + 1
        score = length / max(1, tags)
        if score > best_score:
            best_score = score
            best = node
    return best


def _prune_empty_nodes(root) -> None:
    """Two-pass pruning: collect empty leaf nodes first, then delete them together, avoiding skipped nodes from structural changes during traversal."""
    doomed = []
    for node in list(root.iter()):
        tag = node.tag or ""
        if tag in _PRESERVE_TAGS:
            continue
        if node.text(strip=True):
            continue
        if any((c.tag or "") in ("img", "pre", "code", "table") for c in node.iter()):
            continue
        if not node.child():
            doomed.append(node)
    for node in doomed:
        node.decompose()




"""Content pruning: selectolax Pruning → markdownify.

Follows the blueprint crawl4ai PruningContentFilter idea (composite
text/link/tag scoring with a fixed threshold plus empty-leaf cleanup) and the
LXMLWebScrapingStrategy idea of returning links and metadata alongside the
cleaned content. Pure functions, zero networking.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from markdownify import markdownify as md
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from libref.text.normalize import normalize_markdown

# Noise tags stripped outright
_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "footer",
    "aside",
    "form",
    "header",
)
# Common main-content containers, picked by priority
_MAIN_CANDIDATES = ("main", "article")
# Preserved wholesale during pruning: table structure, code blocks, and img self-count as non-empty
_PRESERVE_TAGS = (
    "pre",
    "code",
    "table",
    "tbody",
    "thead",
    "tfoot",
    "tr",
    "td",
    "th",
    "img",
    "br",
    "hr",
)
# Suspected client-side rendering markers: static fetch yields no content and a hit counts as a skip, not a failure
_JS_MARKERS = (
    'id="root"',
    'id="app"',
    "__NEXT_DATA__",
    "__NUXT__",
    "ng-app",
    "data-reactroot",
    "__INITIAL_STATE__",
)

# Composite pruning weights, mirroring the blueprint scoring math
_FIXED_THRESHOLD = 0.48
_TAG_WEIGHTS = {
    "div": 0.5,
    "p": 1.0,
    "article": 1.5,
    "section": 1.0,
    "span": 0.3,
    "li": 0.5,
    "ul": 0.5,
    "ol": 0.5,
    "h1": 1.2,
    "h2": 1.1,
    "h3": 1.0,
    "h4": 0.9,
    "h5": 0.8,
    "h6": 0.7,
}
_TAG_IMPORTANCE = {
    "article": 1.5,
    "main": 1.4,
    "section": 1.3,
    "p": 1.2,
    "h1": 1.4,
    "h2": 1.3,
    "h3": 1.2,
    "div": 0.7,
    "span": 0.6,
}
_NEGATIVE_RE = re.compile(
    r"nav|footer|header|sidebar|ads|comment|promo|advert|social|share", re.IGNORECASE
)
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


@dataclass
class ScrapeResult:
    """Structured extraction: cleaned markdown plus the link graph and page metadata."""

    markdown: str = ""
    cleaned_html: str = ""
    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    title: str = ""
    description: str = ""
    canonical: str = ""
    noindex: bool = False
    nofollow: bool = False


def extract_to_markdown(html: str, base_url: str | None = None) -> str:
    """HTML → main-content Markdown; return an empty string when nothing remains after pruning (empty outcome)."""
    return scrape_html(html, base_url).markdown


def scrape_html(
    html: str,
    base_url: str | None = None,
    *,
    threshold: float = _FIXED_THRESHOLD,
    threshold_type: str = "fixed",
    min_word_threshold: int | None = None,
    preserve_tags: tuple[str, ...] = (),
    preserve_classes: tuple[str, ...] = (),
) -> ScrapeResult:
    """HTML → structured result; never raises on malformed input, returns empties instead."""
    if not html or not isinstance(html, str):
        return ScrapeResult()
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001 — malformed markup yields an empty outcome
        return ScrapeResult()
    meta = _extract_metadata(tree, base_url)
    if base_url:
        _absolutize_links(tree, base_url)
    for tag in _STRIP_TAGS:
        for node in list(tree.css(tag)):
            node.decompose()
    for node in list(tree.css('[role="navigation"]')):
        node.decompose()

    root = _pick_main_root(tree)
    _prune_scored(
        root,
        threshold=threshold,
        threshold_type=threshold_type,
        min_word_threshold=min_word_threshold,
        preserve_tags=set(preserve_tags),
        preserve_classes=set(preserve_classes),
    )
    _prune_empty_nodes(root)

    internal, external = _collect_links(root, base_url)
    inner = root.html or ""
    try:
        markdown = md(inner, heading_style="ATX", strip=["img"])
    except Exception:  # noqa: BLE001 — converter failure yields an empty outcome
        markdown = ""
    return ScrapeResult(
        markdown=normalize_markdown(markdown),
        cleaned_html=inner,
        internal_links=internal,
        external_links=external,
        title=meta["title"],
        description=meta["description"],
        canonical=meta["canonical"],
        noindex=meta["noindex"],
        nofollow=meta["nofollow"],
    )


def convert_links_to_citations(markdown: str, base_url: str = "") -> tuple[str, str]:
    """Rewrite markdown links as numbered citations with a References section; pure formatting helper."""
    link_map: dict[str, tuple[int, str]] = {}
    url_cache: dict[str, str] = {}
    parts: list[str] = []
    last_end = 0
    counter = 1
    for match in _LINK_RE.finditer(markdown or ""):
        parts.append(markdown[last_end : match.start()])
        text, url = match.group(1), match.group(2)
        if base_url and not url.startswith(("http://", "https://", "mailto:")):
            if url not in url_cache:
                url_cache[url] = urljoin(base_url, url)
            url = url_cache[url]
        if url not in link_map:
            desc = f": {text}" if text and text != url else ""
            link_map[url] = (counter, desc)
            counter += 1
        num = link_map[url][0]
        parts.append(
            f"{text}⟨{num}⟩" if not match.group(0).startswith("!") else match.group(0)
        )
    parts.append((markdown or "")[last_end:])
    converted = "".join(parts)
    if not link_map:
        return converted, ""
    references = ["\n\n## References\n\n"]
    references.extend(
        f"⟨{num}⟩ {url}{desc}\n"
        for url, (num, desc) in sorted(link_map.items(), key=lambda kv: kv[1][0])
    )
    return converted, "".join(references)


def is_data_table(node) -> bool:
    """Heuristic data-table check: header cells or at least two rows with two cells."""
    try:
        rows = list(node.css("tr"))
    except Exception:  # noqa: BLE001 — selector failure means not a data table
        return False
    if any(True for _ in node.css("th")):
        return len(rows) >= 1
    if len(rows) < 2:
        return False
    cells = [len(list(r.css("td,th"))) for r in rows[:4]]
    return sum(1 for c in cells if c >= 2) >= 2


def _absolutize_links(tree: HTMLParser, base_url: str) -> None:
    # attributes is a read-only snapshot; attrs is the mutable mapping.
    for node in tree.css("a[href]"):
        href = node.attrs.get("href", "")
        if href and not href.startswith(
            ("http://", "https://", "mailto:", "#", "data:")
        ):
            node.attrs["href"] = urljoin(base_url, href)
    for node in tree.css("img[src]"):
        src = node.attrs.get("src", "")
        if src and not src.startswith(("http://", "https://", "data:")):
            node.attrs["src"] = urljoin(base_url, src)


def _extract_metadata(tree: HTMLParser, base_url: str | None) -> dict:
    meta: dict = {
        "title": "",
        "description": "",
        "canonical": "",
        "noindex": False,
        "nofollow": False,
    }
    try:
        title_node = tree.css_first("title")
        if title_node is not None:
            meta["title"] = (title_node.text(strip=True) or "").strip()
        for node in tree.css("meta"):
            attrs = {k.lower(): v for k, v in (node.attributes or {}).items()}
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = (attrs.get("content") or "").strip()
            if not content:
                continue
            if (
                name in ("description", "og:description", "twitter:description")
                and not meta["description"]
            ):
                meta["description"] = content
            if name == "robots":
                lowered = content.lower()
                meta["noindex"] = "noindex" in lowered
                meta["nofollow"] = "nofollow" in lowered
        canon = tree.css_first('link[rel="canonical"]')
        if canon is not None:
            href = ((canon.attributes or {}).get("href") or "").strip()
            if href:
                meta["canonical"] = urljoin(base_url, href) if base_url else href
    except Exception:  # noqa: BLE001, S110 — metadata is best-effort, never blocks extraction
        pass
    return meta


def _collect_links(root, base_url: str | None) -> tuple[list[str], list[str]]:
    internal: list[str] = []
    external: list[str] = []
    seen: set[str] = set()
    host = urlsplit(base_url).netloc.lower() if base_url else ""
    try:
        nodes = list(root.css("a[href]"))
    except Exception:  # noqa: BLE001 — link collection is best-effort
        return [], []
    for node in nodes:
        href = (node.attributes or {}).get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "data:", "javascript:")):
            continue
        if href in seen:
            continue
        seen.add(href)
        if host and urlsplit(href).netloc.lower() not in ("", host):
            external.append(href)
        else:
            internal.append(href)
    return internal, external


def _metrics_for(node) -> tuple[int, int, int, int]:
    """Cached-friendly single read of text length, markup length, direct-link length, word count."""
    try:
        text = node.text(strip=True) or ""
    except Exception:  # noqa: BLE001 — unreadable node counts as empty
        return 0, 0, 0, 0
    try:
        tag_len = len(node.html or "")
    except Exception:  # noqa: BLE001 — unreadable markup counts as zero length
        tag_len = 0
    link_len = 0
    try:
        for anchor in _element_children(node):
            if (anchor.tag or "").lower() != "a":
                continue
            try:
                s = anchor.text(strip=True) or ""
            except Exception:  # noqa: BLE001 — unreadable link counts as zero
                s = ""
            link_len += len(s)
    except Exception:  # noqa: BLE001, S110 — link measurement is best-effort
        pass
    words = text.count(" ") + 1 if text else 0
    return len(text), tag_len, link_len, words


def _composite_score(
    tag: str,
    text_len: int,
    tag_len: int,
    link_len: int,
    words: int,
    node,
    min_word_threshold: int | None,
) -> float:
    if min_word_threshold is not None and words < min_word_threshold:
        return -1.0
    text_density = text_len / tag_len if tag_len > 0 else 0.0
    link_density = 1.0 - (link_len / text_len if text_len > 0 else 0.0)
    tag_score = _TAG_WEIGHTS.get(tag, 0.5)
    class_score = 0.0
    try:
        attrs = node.attributes or {}
        classes = attrs.get("class", "")
        element_id = attrs.get("id", "")
        if classes and _NEGATIVE_RE.search(str(classes)):
            class_score -= 0.5
        if element_id and _NEGATIVE_RE.search(str(element_id)):
            class_score -= 0.5
    except Exception:  # noqa: BLE001, S110 — attribute read failure keeps a neutral score
        pass
    length_score = math.log(text_len + 1)
    total = 0.4 + 0.2 + 0.2 + 0.1 + 0.1
    return (
        0.4 * text_density
        + 0.2 * link_density
        + 0.2 * tag_score
        + 0.1 * max(0.0, class_score)
        + 0.1 * length_score
    ) / total


def _is_preserved(node, preserve_tags: set[str], preserve_classes: set[str]) -> bool:
    tag = (node.tag or "").lower()
    if tag in _PRESERVE_TAGS or tag in preserve_tags:
        return True
    if preserve_classes:
        try:
            classes = str((node.attributes or {}).get("class", "")).split()
            if set(classes) & preserve_classes:
                return True
        except Exception:  # noqa: BLE001 — attribute failure means not preserved
            return False
    if tag == "table":
        try:
            return is_data_table(node)
        except Exception:  # noqa: BLE001 — table check failure falls through
            return False
    return False


def _prune_scored(
    root,
    *,
    threshold: float,
    threshold_type: str,
    min_word_threshold: int | None,
    preserve_tags: set[str],
    preserve_classes: set[str],
) -> None:
    """Top-down composite prune over once-cached metrics; preserved subtrees are kept whole.

    Node wrappers are re-created on every access, so identity keys are invalid;
    one own walk records parent-child index pairs and metrics are cached by index.
    """
    order: list = []
    children_of: list[list[int]] = []
    try:
        _record_subtree(root, order, children_of)
    except Exception:  # noqa: BLE001 — untraversable tree skips scoring
        return
    if not order:
        return
    metrics = [_metrics_for(node) for node in order]
    try:
        text_len, tag_len, link_len, words = metrics[0]
        if (
            _composite_score(
                (root.tag or "").lower(),
                text_len,
                tag_len,
                link_len,
                words,
                root,
                min_word_threshold,
            )
            < threshold
        ):
            return
    except Exception:  # noqa: BLE001, S110 — root scoring failure keeps the tree
        pass
    stack = [0]
    while stack:
        parent_idx = stack.pop()
        for child_idx in children_of[parent_idx]:
            child = order[child_idx]
            try:
                if _is_preserved(child, preserve_tags, preserve_classes):
                    continue
                text_len, tag_len, link_len, words = metrics[child_idx]
                score = _composite_score(
                    (child.tag or "").lower(),
                    text_len,
                    tag_len,
                    link_len,
                    words,
                    child,
                    min_word_threshold,
                )
                limit = threshold
                if threshold_type == "dynamic":
                    importance = _TAG_IMPORTANCE.get((child.tag or "").lower(), 0.7)
                    text_ratio = text_len / tag_len if tag_len > 0 else 0.0
                    link_ratio = link_len / text_len if text_len > 0 else 1.0
                    if importance > 1:
                        limit *= 0.8
                    if text_ratio > 0.4:
                        limit *= 0.9
                    if link_ratio > 0.6:
                        limit *= 1.2
                if score < limit:
                    child.decompose()
                else:
                    stack.append(child_idx)
            except Exception:  # noqa: BLE001, S112 — per-node failure skips that node only
                continue


def _record_subtree(node, order: list, children_of: list[list[int]]) -> int:
    """Pre-order record of element nodes with direct-child index pairs."""
    idx = len(order)
    order.append(node)
    children_of.append([])
    for child in _element_children(node):
        children_of[idx].append(_record_subtree(child, order, children_of))
    return idx


def _element_children(parent) -> list:
    """Direct element children via the sibling chain; text and comment nodes are skipped."""
    out: list = []
    try:
        child = parent.child
    except Exception:  # noqa: BLE001 — unreadable node has no children
        return out
    while child is not None:
        try:
            tag = child.tag or ""
            nxt = child.next
        except Exception:  # noqa: BLE001 — unreadable sibling ends the walk
            break
        if tag and not tag.startswith("-"):
            out.append(child)
        child = nxt
    return out


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
    for node in list(root.traverse()):
        if node == root:
            continue
        tag = node.tag or ""
        if tag in _PRESERVE_TAGS:
            continue
        if node.text(strip=True):
            continue
        try:
            rich = bool(node.css("img,pre,code,table"))
        except Exception:  # noqa: BLE001 — selector failure counts as no rich content
            rich = False
        if rich:
            continue
        if not _element_children(node):
            doomed.append(node)
    for node in doomed:
        node.decompose()

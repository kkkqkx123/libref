"""正文净化：selectolax Pruning → markdownify。

蓝本 crawl4ai PruningContentFilter 思路（剥噪 + 空文本叶节点丢弃）+
docs-mcp-server 中间件链的 HtmlToMarkdown 环节。纯函数、零网络。
"""

from __future__ import annotations

from markdownify import markdownify as md
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from urllib.parse import urljoin

# 直接剔除的噪点标签
_STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "aside", "form", "header")
# 常见正文容器，按优先级选取
_MAIN_CANDIDATES = ("main", "article")


def extract_to_markdown(html: str, base_url: str | None = None) -> str:
    """HTML → 正文 Markdown；净化后无正文返回空串（empty 结局）。"""
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
    return _normalize(markdown)


def _absolutize_links(tree: HTMLParser, base_url: str) -> None:
    for node in tree.css("a[href]"):
        href = node.attributes.get("href", "")
        if href and not href.startswith(("http://", "https://", "mailto:", "#", "data:")):
            node.attributes["href"] = urljoin(base_url, href)
    for node in tree.css("img[src]"):
        src = node.attributes.get("src", "")
        if src and not src.startswith(("http://", "https://", "data:")):
            node.attributes["src"] = urljoin(base_url, src)


def _pick_main_root(tree: HTMLParser):
    for tag in _MAIN_CANDIDATES:
        nodes = tree.css(tag)
        if nodes:
            return max(nodes, key=lambda n: len(n.text(strip=True)))
    body = tree.css_first("body")
    return body if body is not None else tree.root


def _prune_empty_nodes(root) -> None:
    """丢弃无文本且无代码/图片内容的叶节点（Pruning 思路：文本密度过滤的轻量版）。"""
    doomed = []
    for node in list(root.iter()):
        tag = node.tag or ""
        if tag in ("pre", "code", "td", "th", "br", "hr", "table"):
            continue
        if node.text(strip=True) or any(c.tag in ("img", "pre", "code") for c in node.iter()):
            continue
        if not node.child():
            doomed.append(node)
    for node in doomed:
        node.decompose()


def _normalize(markdown: str) -> str:
    lines = [ln.rstrip() for ln in markdown.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln == "":
            blank += 1
            if blank > 2:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()

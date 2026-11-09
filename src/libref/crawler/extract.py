"""正文净化：selectolax Pruning → markdownify。

蓝本 crawl4ai PruningContentFilter 思路（剥噪 + 空文本叶节点丢弃）+
docs-mcp-server 中间件链的 HtmlToMarkdown 环节。纯函数、零网络。
"""

from __future__ import annotations

from urllib.parse import urljoin

from markdownify import markdownify as md
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from libref.text.normalize import normalize_markdown

# 直接剔除的噪点标签
_STRIP_TAGS = ("script", "style", "noscript", "nav", "footer", "aside", "form", "header")
# 常见正文容器，按优先级选取
_MAIN_CANDIDATES = ("main", "article")
# 剪枝时整体保留：表格结构、代码块、图片自身不计为空
_PRESERVE_TAGS = ("pre", "code", "table", "tbody", "thead", "tfoot", "tr", "td", "th", "img", "br", "hr")
# 疑似客户端渲染标记：静态抓取无正文且命中则计为跳过而非失败
_JS_MARKERS = ("id=\"root\"", "id=\"app\"", "__NEXT_DATA__", "__NUXT__", "ng-app", "data-reactroot", "__INITIAL_STATE__")


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
    """静态文档无正文且命中渲染标记时视为渲染页，调用方计为跳过。"""
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
    """候选缺失时回退到正文密度最高的块容器，避免长导航页误取整页。"""
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
    """两遍剪枝：先收集空叶节点再统一删除，避免遍历中修改结构跳过。"""
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




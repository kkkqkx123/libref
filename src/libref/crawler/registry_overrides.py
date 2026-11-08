"""注册表人工覆盖：少量高价值规则，随代码发布。

键为生态加归一化后名称精确匹配，值为强制文档地址、来源与核验日期。
覆盖永远优先于注册表推导，只低于用户显式地址。过期不降级，由测试门禁提醒核验。
"""

from __future__ import annotations

# (ecosystem, normalized_name) -> {docs_url, source, verified_at}
# verified_at 为 YYYY-MM-DD 字符串。
OVERRIDES: dict[tuple[str, str], dict[str, str]] = {}


def get_override(ecosystem: str, normalized_name: str) -> dict[str, str] | None:
    """精确匹配覆盖规则，命中返回条目，否则返回空。"""
    return OVERRIDES.get((ecosystem, normalized_name))

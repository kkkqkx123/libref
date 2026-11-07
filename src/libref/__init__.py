"""libref 包入口：脚本 `libref` 指向 cli.main。"""

from libref.cli import main


def __getattr__(name: str):  # 惰性导出，避免 import libref 时拉起重依赖
    if name == "main":
        return main
    raise AttributeError(name)

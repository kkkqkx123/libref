"""最小 Python 样本：异步加装饰器加别名，贴近爬虫框架写法。"""

from __future__ import annotations

import functools

import numpy as np


@functools.lru_cache(maxsize=1)
async def fetch(url):
    await get(url)
    return np.array([1])


class Worker:
    def run(self):
        self.step()
        np.zeros(3)

    def step(self):
        pass

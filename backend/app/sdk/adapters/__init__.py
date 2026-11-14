"""网盘 adapter 集合。

每加一个网盘，在这里 import 一次即可（import 会触发 @register）。
业务层永远不 import 这个包。
"""

from . import baidu, c139, guangya, quark, xunlei  # noqa: F401

__all__ = ["quark", "baidu", "xunlei", "guangya", "c139"]

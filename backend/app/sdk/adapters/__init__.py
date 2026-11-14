"""网盘 adapter 集合。

每加一个网盘，往 _MODULES 里加一行即可（import 会触发 @register）。
业务层永远不 import 这个包。

**逐个 import，而不是一行打包 import** —— 这是踩过的坑：
139 依赖 cryptography，服务器上只 `git pull` 没装包时，打包 import 会把
ModuleNotFoundError 冒到包这一层，于是五个盘**全部**注册失败。
线上现象是"所有点击都回退原链接、后台测试报不支持该平台"，
而根因只是少装一个包，极难往这个方向想。改成逐个兜住后，
缺依赖的那个盘自己缺席，其余照常工作。
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger("pantrace.sdk")

_MODULES = ("quark", "baidu", "xunlei", "guangya", "c139")

#: 盘名 → 加载失败原因；加载成功的记空串。后台「测试」靠它解释为什么用不了
LOAD_ERRORS: dict[str, str] = {}

for _name in _MODULES:
    try:
        importlib.import_module(f".{_name}", __name__)
        LOAD_ERRORS[_name] = ""
    except Exception as exc:  # noqa: BLE001
        LOAD_ERRORS[_name] = f"{type(exc).__name__}: {exc}"
        log.warning("网盘 %s 未能加载，该盘暂不可用（其余网盘不受影响）：%s", _name, exc)

__all__ = [*_MODULES, "LOAD_ERRORS"]

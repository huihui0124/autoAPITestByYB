# -*- coding: utf-8 -*-
"""
xlcompat.py —— openpyxl 读旧/新格式 xlsx 的兼容补丁

问题
    user 的「含结果」「开发冒烟自测」文件里，`<dataValidation>` 带了 openpyxl 3.1.5
    的 `DataValidation.__init__` 不认识的属性（如 `id=`），一 `load_workbook` 就
        TypeError: DataValidation.__init__() got an unexpected keyword argument 'id'
    连 `read_only=True` 也会在流式解析到该节点时炸（只在读完整表时才触发，读首行不触发——
    所以"先试读表头能过"会给人错觉）。

做法
    给 DataValidation.__init__ 打个补丁：按签名过滤掉不认识的属性，其余照旧。
    比"删文件里的节点"安全（不动原文件），比升 openpyxl 风险小（升级可能改变写出结果，
    而我们的格式是逐格卡死过的）。

副作用
    无。被丢弃的只是 openpyxl 本就不支持的可选属性，不影响单元格取值与样式读取。
"""
from __future__ import annotations

import inspect

_applied = False


def patch_openpyxl() -> None:
    global _applied
    if _applied:
        return
    from openpyxl.worksheet.datavalidation import DataValidation

    orig = DataValidation.__init__
    try:
        allowed = set(inspect.signature(orig).parameters)
    except (TypeError, ValueError):        # pragma: no cover
        allowed = set()

    def tolerant_init(self, *args, **kwargs):
        if allowed:
            kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        return orig(self, *args, **kwargs)

    DataValidation.__init__ = tolerant_init
    _applied = True


patch_openpyxl()

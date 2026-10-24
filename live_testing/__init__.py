# -*- coding: utf-8 -*-
"""live_testing —— 接口存活性测试模块（OpenAPI 自动生成 + pytest 执行）。

目标只有一条：**接口可达、不报 HTTP 5xx / 网络错误**。
不验证业务正确性；业务链路断言仍放在仓库根目录的 test_*.py 里。

入口：
    python -m live_testing.generator          # OpenAPI → live_testing/cases/*.json
    MIGU_LIVE=1 python -m pytest live_testing -m live

默认只跑 GET；写接口/危险接口需要显式打开闸门。详见 live_testing/README.md。
"""

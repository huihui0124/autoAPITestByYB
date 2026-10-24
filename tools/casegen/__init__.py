# -*- coding: utf-8 -*-
"""
casegen —— 「需求文档 → 规范测试用例 → 环境就绪」的流水线

    docs/PRD/技术设计
        │  (作者：人 或 智能体；产出可评审的 Markdown 规格)
        ▼
    用例规格.md  ──casegen build──▶  用例.xlsx（严格按 16 列模版，逐格校验）
        │
        └──casegen plan/provision──▶  每条用例的「环境要求」落到 catalog 里的具体
                                     项目/场次/票档；缺的项目在 B 端建好并登记

模块
    template.py  模版格式权威：16 列定义 + 写入 + 逐格校验
    spec.py      Markdown 规格 ⇄ Suite 对象
    resolve.py   「环境要求」→ 商品清单目标（含缺什么项目的诊断）
    factory.py   缺项目 → 克隆骨架在 B 端建好 + 登记进 catalog.json
    cli.py       命令入口
"""
from . import factory, resolve, spec, template  # noqa: F401

__all__ = ["template", "spec", "resolve", "factory"]

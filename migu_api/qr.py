# -*- coding: utf-8 -*-
"""
qr.py —— 二维码渲染小工具

用途：真实支付必须人工扫码，所以脚本要把 wapPayParam 变成"能扫的东西"。
两种输出：
    - render_ascii(): 直接打在终端（适合快速扫）
    - save_png():     存成 PNG 文件（适合在屏幕上放大扫，或做 Allure 附件）

依赖 qrcode + pillow，已在 .venv 里（pip install qrcode pillow）。
"""
from pathlib import Path


def render_ascii(url: str) -> str:
    """把 URL 渲染成终端可打印的 ASCII 二维码。"""
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)   # invert=True：黑底白块，终端深色主题下更清楚
    return ""


def save_png(url: str, out_path: str | Path, box_size: int = 10) -> Path:
    """把 URL 存成 PNG 二维码，返回文件路径（父目录自动创建）。"""
    import qrcode

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(box_size=box_size, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(out)
    return out

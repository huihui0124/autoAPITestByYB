# -*- coding: utf-8 -*-
"""
paypage.py —— 把 /v1/miguPay/pay 返回的 paymentParams 变成"人能付的页面"

两种形态（实测 2026-09-29）：
    微信 WX → "https://wx.tenpay.com/cgi-bin/mmpayweb-bin/checkmweb?prepay_id=..." 
              纯链接，出二维码扫即可
    支付宝 AP → "<![CDATA[<form ... action="https://openapi.alipay.com/gateway.do?..."
              method=alipay.trade.wap.pay ... <script>document.forms[0].submit()</script>]]>"
              是 HTML 表单，必须让浏览器加载它（脚本自动提交 → 支付宝收银台）

所以这里做两件事：
    1. strip_cdata()：剥掉 Ruby 网关套的 CDATA 外壳
    2. to_pay_url()：把 POST 表单还原成等价的可 GET 链接
       （关键是 biz_content 也要进 url，签名本身就覆盖它，实测支付宝接受 GET）
"""
from pathlib import Path
from urllib.parse import urlencode


def _unescape_attrs(s: str) -> str:
    """只还原 HTML 属性里真正需要的实体。

    ⚠️ 千万别用 html.unescape 一刀切！支付宝 action 里有 &timestamp=，
    而 &times 是合法 HTML 实体（×），unescape 会把它变成 "×tamp="，
    参数名直接坏掉、支付宝报签名错误。这里只做定向替换。
    """
    for entity, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                       ("&#39;", "'"), ("&apos;", "'")):
        s = s.replace(entity, ch)
    return s.replace("&amp;", "&")          # amp 必须最后处理


def strip_cdata(raw: str) -> str:
    """剥掉 <![CDATA[ ... ]]> 外壳（Ruby 网关 XML 转 JSON 留下的）。"""
    s = (raw or "").strip()
    if s.startswith("<![CDATA["):
        s = s[len("<![CDATA["):]
    if s.endswith("]]>"):
        s = s[:-3]
    return s.strip()


def is_link(params: str) -> bool:
    """微信形态：纯链接。"""
    return strip_cdata(params).startswith("http")


def is_form(params: str) -> bool:
    """支付宝形态：HTML 表单。"""
    return "<form" in strip_cdata(params).lower()


def extract_form_action(params: str) -> str:
    """从 HTML 表单里抠出 action（已 unescape &amp;）。"""
    import re

    html = strip_cdata(params)
    m = re.search(r'action="([^"]+)"', html, re.I)
    if not m:
        raise ValueError("表单里没有 action，支付宝返回结构变了？")
    return _unescape_attrs(m.group(1))


def extract_hidden_fields(params: str) -> dict:
    """抠出所有 <input type="hidden"> 的 name/value（value 里的 &quot; 已还原）。"""
    import re

    html = strip_cdata(params)
    fields = {}
    for tag in re.findall(r"<input[^>]*>", html, re.I):
        if 'type="hidden"' not in tag.lower():
            continue
        name = re.search(r'name="([^"]*)"', tag, re.I)
        value = re.search(r'value="([^"]*)"', tag, re.I)
        if name:
            fields[_unescape_attrs(name.group(1))] = _unescape_attrs(value.group(1)) if value else ""
    return fields


def to_pay_url(params: str) -> str:
    """把支付宝 POST 表单还原成等价的可 GET 链接，用于出二维码。"""
    action = extract_form_action(params)
    fields = extract_hidden_fields(params)
    if not fields:
        return action

    base, _, query = action.partition("?")
    merged = query + ("&" if query else "") + urlencode(fields, quote_via=_quote)
    return f"{base}?{merged}"


def _quote(string, safe="", encoding=None, errors=None):
    """urlencode 用的编码器：空格必须编成 %20（不能是 +，否则签名不匹配）。"""
    from urllib.parse import quote

    return quote(string, safe=safe, encoding=encoding or "utf-8", errors=errors or "strict")


def save_form_html(params: str, out_path: str | Path) -> Path:
    """把表单 HTML 落盘，方便浏览器直接打开（会自己 submit 到支付宝）。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(strip_cdata(params), encoding="utf-8")
    return out


def describe(params: str) -> str:
    """给日志用的一句话形态描述。"""
    if is_link(params):
        return "微信 H5 链接"
    if is_form(params):
        return "支付宝 HTML 表单"
    return "未知形态"

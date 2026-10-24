"""精确扫描：定位配置字段归属模型 + 展开候选配置模型全字段。"""
import json
import re
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

client = BAdminClient()
doc = client.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()
SCHEMAS = doc["components"]["schemas"]

# 用正则而非裸子串，避免 message/image 命中 age
PATS = {
    "售卖时间": r"(fixedsale|salestart|saleend|sellstart|sellend|onsalestart|onsaleend|saletime|onsaletime)",
    "限购": r"(purchaselimit|buylimit|limitcount|limitnum|maxbuy|peruserlimit|purchasecount|purchasenum|ordernumlimit|每人|限购)",
    "年龄限制": r"(^age$|agelimit|agegroup|minage|maxage)",
    "实名": r"(realname|needrealname)",
    "座位": r"(selectseat|seat)",
    "核销": r"(verifycount|maxverify|thirdverification|consume)",
    "库存": r"(stock)",
    "退款": r"(refund)",
    "证件": r"(certificate|credential|idcard|idno|idtype)",
    "票档": r"(ticketcategory|tickettype|skucategory)",
}
print("=" * 84)
print("精确关键词 → 字段归属")
print("=" * 84)
res = defaultdict(list)
for sname, sch in SCHEMAS.items():
    for fname, val in (sch.get("properties") or {}).items():
        for tag, pat in PATS.items():
            if re.search(pat, fname, re.I):
                res[tag].append((sname, fname, val.get("type", "?"),
                                 (val.get("description") or "").strip().replace("\n", " ")[:55]))

for tag in PATS:
    rows = res[tag]
    print(f"\n【{tag}】{len(rows)} 处")
    for sname, fname, typ, desc in rows[:40]:
        print(f"    {sname}.{fname}: {typ}  {desc}")

print("\n" + "=" * 84)
print("候选配置模型全字段")
print("=" * 84)
for name in ["SkuSelfAdditionDto", "ProductExtDataDTO", "ProductSkuInfoDto",
             "SkuAgeLimitDTO", "SkuAgeLimitConfigDTO", "SkuAgeLimitRuleDTO",
             "TicketPriceInfo", "TicketEditWebReq", "TicketCategoryDTO"]:
    sch = SCHEMAS.get(name)
    if not sch:
        print(f"\n<{name}> 不存在")
        continue
    print(f"\n<{name}>")
    for fname, val in (sch.get("properties") or {}).items():
        enum = val.get("enum")
        en = f" 枚举={enum}" if enum else ""
        desc = (val.get("description") or "").strip().replace("\n", " ")[:66]
        print(f"    {fname}: {val.get('type','?')}{en}  {desc}")

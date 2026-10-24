# -*- coding: utf-8 -*-
"""
refund.py —— 退款链路客户端（B 端条件退 / B 端常规退；C 端条件退在 client.py）

★★★ 退款有**三种方式**，别混为一谈（2026-09-30 在生产 888 逐条实测）：

  ① B端条件退  —— 逐票、可选退什么。**只能从单笔订单的「订单详情」发起**。
     支持三种组合：仅退票 / 仅退款 / 退票+退款（= `hasTicketRefund`/`hasMoneyRefund`）。
     本模块 `RefundClient` 的 partial 系列就是这一条：
        GET  /v1/shopTrade/refund/refundable/{unifiedOrderId}   可退性（is 判据）
        POST /v1/shopTrade/refund/calculate                     试算
        POST /v1/shopTrade/refund/partial                       提交 -> 返回整数 refundId
        POST /v1/shopTrade/refund/approval                      审批（approvalStatus=2 同意）
     入口是「订单详情」，所以**只要有单号就能发起，不受场次条件退配置影响**。

  ② B端常规退  —— **只能全量退票+退款**（不能选只退票或只退钱，也不能只退一部分）。
     **必须从「退款中心」发起，再去「退款审批」菜单审批通过**。见本模块 regular 系列：
        GET  /v1/shopTrade/order/refundCenterSearch                退款中心列表（发起入口）
        POST /v2/shopTrade/purchaseOrder/batchRefund               发起 -> 返回字符串 taskId
        POST /v2/shopTrade/purchaseOrder/batchRefund/approve       退款审批（approved: bool）
     实测：发起后订单 11/待核销 → **14/售后中**，审批通过后 → **5/已退款**，
     工单列表 `refundTypeDesc` 恒为「退票+退款」（印证"只能全量"）。

  ③ C端条件退  —— 用户从 C 端自助发起，**无需审批**。见 `client.py` 的
     `refund_pre` / `refund_submit`（`GET/POST /v1/order/refund/{id}/pre|submit`）。
     三个**并列**的硬约束，缺一不可（都实测到原文报错）：
        a. 场次必须配置了条件退 —— 否则 `canShowRefund=false`，refundTip
           「本项目不支持退款」，**C 端根本不出现退款入口**；
        b. 订单不能是终态（已核销/已收货/已完成）—— 否则 `canRefund=false`，
           refundTip「订单已经完成啦～如需退款，联系客服为您处理哦！」；
        c. 退款金额不能为 0 —— 否则 submit 返回
           400「0元单不可退款,请联系客服~」（**0 元夹具因此天然跑不了 C 端条件退**）。

  为什么单独一个模块：
    "退款"不是单个接口，而是一条带审批的工单流。它跟核销同属 B 端
    （同一套 4A 会话 + hm-store-id 鉴权），但语义独立，所以从 badmin 里拆出来，
    核销用例和退款用例互不干扰。

★ ① 与 ② 的工单**共用** `POST /v1/shopTrade/refund/list` 这一张列表：
    条件退的工单 `refundType` 随"退什么"变（1/2/3），常规退恒为 3；
    两者都靠 taskId 查 `GET /v1/shopTrade/refund/detail/{taskId}`。

接口来自 B 端自带的 OpenAPI 文档：`GET {b_api}/v3/api-docs`
    GET  /v1/shopTrade/refund/refundable/{unifiedOrderId}   可退性查询
    POST /v1/shopTrade/refund/calculate                     退款试算（不改数据）
    POST /v1/shopTrade/refund/partialIndex                  选票
    POST /v1/shopTrade/refund/partial                       提交退款申请 -> **返回整数 refundId**
    POST /v1/shopTrade/refund/approval                      审批
    GET  /v1/shopTrade/refund/detail/{taskId}               工单详情
    POST /v1/shopTrade/refund/list                          工单列表
    POST /v2/shopTrade/refund/task/{taskId}/cancel          撤销工单
    POST /v2/shopTrade/refund/batchRetry                    打款失败重试

    —— 以上是 ①；以下是 ②（常规退专属）——
    GET  /v1/shopTrade/order/refundCenterSearch             退款中心列表
    POST /v2/shopTrade/purchaseOrder/batchRefund            发起常规退 -> 返回字符串 taskId
    POST /v2/shopTrade/purchaseOrder/batchRefund/approve    退款审批（approved:bool）
    POST /v2/shopTrade/purchaseOrder/{poId}/miguRefund      咪咕通道（同样配 .../approve）

★ 三个 ID 别搞混（踩过）：
    - `unifiedOrderId`：C 端订单号（就是 C 端 `orderId`，如 61008681）
    - `taskId`：退款**工单号**（19 位字符串，如 "2026092914270835384"）
    - `refundId`：退款**记录 ID**（整数，`/refund/partial` 的**返回值**，也是 detail 的 `id`）
    审批接口要 refundId + taskId **两个都传**，所以提交后必须回查一次工单列表补齐 taskId。

★★ 最坑的一点：`refundType` 有**两套同名枚举**，语义完全不同：
    ① 请求体 `PartialRefundRequest.refundType` = **退款范围**，实测**只接受 2（部分退款）**；
       传 1/3/4/5 一律 500 "退款类型必须为部分退款"。
    ② 工单列表 `PartialRefundDetailVO.refundType`（列表视图）= **退什么**：
       1=退票 / 2=退款 / 3=退票+退款，它其实是 ③ 派生出来的。
    ③ 真正决定"退什么"的是请求体里的 `hasTicketRefund` / `hasMoneyRefund`（0/1）。
    所以：提交永远传 refundType=2，用 hasTicketRefund/hasMoneyRefund 表达退票还是退钱。
    （工单**详情**接口返回的 refundType 又是 ① 那套 = 2/"部分退款"，别拿它当业务类型。）

实测枚举（2026-09-29，测试环境 store 1 + 生产 store 888 双环境验证）：
    taskStatus      1=待审批 / 2=成功 / 5=待处理(新建即此态) / 6=已关闭 / 7=？
                    —— 现网分布 {5:5, 2:82, 6:42, 7:13, 3:8}，2 是终态成功
    approvalStatus  **2 = 同意**（唯一实测可用值；0/1 报"审批状态不合法"，
                    3~9 在工单已处理时报"售后审批任务当前不可处理"）
                    ⚠️ 顾名思义以为 2 是驳回 → 会把"同意"当"拒绝"用（踩过）
    工单 status     2="同意"（审批结果），与 taskStatus 是两个维度
"""
from __future__ import annotations

import time

from .badmin import BAdminClient, BAdminError

# ---- 请求体 refundType：退款「范围」，该接口只接受这一个值 ----
REFUND_TYPE_PARTIAL = 2

# ---- 「退什么」：由 hasTicketRefund / hasMoneyRefund 表达 ----
WHAT_TICKET = "ticket"      # 只退票，钱不退
WHAT_MONEY = "money"        # 只退钱，票不退
WHAT_BOTH = "both"          # 退票 + 退款

_WHAT_FLAGS = {
    WHAT_TICKET: (1, 0),
    WHAT_MONEY: (0, 1),
    WHAT_BOTH: (1, 1),
}

# ---- 审批结果（RefundApprovalRequest.approvalStatus）----
APPROVAL_PASS = 2           # 实测 = 同意
APPROVAL_REJECT = 3         # 未实测到（需在待审批工单上验证）

# ---- 工单状态（taskStatus）----
TASK_PENDING = 1
TASK_SUCCESS = 2
TASK_PROCESSING = 5         # 新建工单即此态
TASK_CLOSED = 6

TASK_STATUS_DESC = {
    1: "待审批",
    2: "成功",
    3: "?",
    5: "待处理",
    6: "已关闭",
    7: "?",
}

# ---- 列表视图的 refundType（退什么），仅用于读断言 ----
LIST_REFUND_TYPE_DESC = {1: "退票", 2: "退款", 3: "退票+退款"}

# ---- 常规退（退款中心）恒为「全量退票+退款」，工单上就是这个值 ----
REGULAR_REFUND_TYPE_FULL = 3

# ---- 订单状态（常规退发起后必经的中间态）----
ORDER_STATUS_AFTER_SALE = 14    # 售后中（发起常规退后、审批通过前）
ORDER_STATUS_REFUNDED = 5       # 已退款（审批通过后的终态）
# 注：client.REFUNDED_STATUS = {5, 14} 是"C 端口径的售后集合"，包含中间态 14；
#     常规退要判"钱真的退了"，只认 5。


def task_status_desc(status) -> str:
    if status is None:
        return "None"
    return f"{status}({TASK_STATUS_DESC.get(status, '未知')})"


def what_desc(has_ticket_refund, has_money_refund) -> str:
    """把 hasTicketRefund/hasMoneyRefund 翻译成人话。"""
    pair = (int(has_ticket_refund or 0), int(has_money_refund or 0))
    return {(1, 0): "退票", (0, 1): "退款", (1, 1): "退票+退款"}.get(pair, f"未知{pair}")


class RefundClient(BAdminClient):
    """B 端退款客户端。继承 BAdminClient 以复用 4A 会话、Cookie 落盘与鉴权头。"""

    # ---------------- 只读：可退性 ----------------

    def refundable(self, order_id) -> dict:
        """GET /v1/shopTrade/refund/refundable/{unifiedOrderId}

        `RefundableItemVO.canRefundTicket` 才是"这张票能不能退"的判据；
        `remainRefundableAmount` 是"还剩多少钱可退"。
        ⚠️ 未支付/不存在的订单会抛 BAdminError("订单不存在或未支付")。
        """
        return self._request(
            "GET", f"/v1/shopTrade/refund/refundable/{order_id}",
            params={"storeId": self.store_id},
        )

    @staticmethod
    def build_items(info: dict, *, what: str = WHAT_BOTH,
                    counts: dict | None = None) -> list[dict]:
        """把 `refundable` 的返回转成 `PartialRefundRequest.refundItems`。

        只取还有剩余可退数量的项（remainCount > 0），避免把已退完的项又提交一遍。
        `what` 决定每项的 refundTicketCount / refundMoneyCount 是否清零。

        `counts` 用于**部分退**：`{orderItemId: 本次要退的份数}`；
        没给到的项默认退满（remainCount）。份数会被裁剪到 [0, remainCount]。

        ★★ 金额契约（2026-09-29 双环境实测，最容易踩的地方）：
            `refundTicketCount` / `refundMoneyCount` **只决定"记几份"**，
            **实际退多少钱完全由 `itemOriginalAmount` 决定** —— 系统把它当
            "本次选中项金额"，不是"单价"。两者不一致会留下脏数据：
                实测把一个 3 份(0.03)的项传 份数=1 + itemAmount=0.03 →
                真退了 0.03，却只记 refundedCount=1、remainCount=2，
                而 `remainRefundableAmount` 已变 0（有钱可退的份数已用尽）。
            所以这里 **itemOriginalAmount = unitPrice × 本次份数**，与份数严格一致。
            反之若照字段名传"单价"，多份订单全额退就会**少退钱**（早期实现的缺陷）。
        """
        want_ticket, want_money = _WHAT_FLAGS[what]
        counts = counts or {}
        items = []
        for it in (info.get("refundableItems") or []):
            iid = it.get("orderItemId")
            remain = int(it.get("remainCount") or 0)
            if remain <= 0:
                continue
            if iid in counts:
                want = counts[iid]
            elif str(iid) in counts:
                want = counts[str(iid)]
            else:
                want = remain
            want = max(0, min(int(want), remain))
            if want <= 0:
                continue
            unit = float(it.get("unitPrice") or 0)
            items.append({
                "orderItemId": iid,
                "skuId": it.get("skuId"),
                "refundTicketCount": want if want_ticket else 0,
                "refundMoneyCount": want if want_money else 0,
                "itemOriginalAmount": round(unit * want, 2),   # ← 份数 × 单价，别传单价
            })
        return items

    @staticmethod
    def items_amount(items: list[dict]) -> float:
        """本次退款金额 = 各项 itemOriginalAmount 之和（要与份数一致）。"""
        return round(sum(float(i.get("itemOriginalAmount") or 0) for i in items), 2)

    # ---------------- 只读：试算与查询 ----------------

    def _refund_payload(self, order_id, items: list[dict], what: str,
                        amount=None, reason: str = "自动化验证",
                        remark: str = "") -> dict:
        want_ticket, want_money = _WHAT_FLAGS[what]
        return {
            "unifiedOrderId": int(order_id),
            "storeId": int(self.store_id),
            "refundType": REFUND_TYPE_PARTIAL,   # ← 只接受 2，别传业务类型
            "refundItems": items,
            "reason": reason,
            "remark": remark,
            "hasTicketRefund": want_ticket,
            "hasMoneyRefund": want_money,
            "requestRefundAmount": amount,
        }

    def calculate(self, order_id, items: list[dict], what: str = WHAT_BOTH,
                  amount=None, reason: str = "自动化验证") -> dict:
        """POST /v1/shopTrade/refund/calculate —— 退款试算（**不改任何数据**）。

        返回 `RefundAmountCalculateVO.recommendedRefundAmount / actualRefundAmount`。
        正式提交前先跑这个，确认"系统认为该退多少"。
        """
        return self._request("POST", "/v1/shopTrade/refund/calculate",
                             json=self._refund_payload(order_id, items, what, amount,
                                                       reason, "calculate"))

    def detail(self, task_id) -> dict:
        """GET /v1/shopTrade/refund/detail/{taskId} —— 工单详情。"""
        return self._request("GET", f"/v1/shopTrade/refund/detail/{task_id}",
                             params={"storeId": self.store_id})

    def list_tasks(self, order_id=None, task_status=None, refund_type=None,
                   page_size: int = 10, page_index: int = 1) -> dict:
        """POST /v1/shopTrade/refund/list —— 工单列表（`data` 就是工单数组）。"""
        payload = {"storeId": int(self.store_id), "pageIndex": page_index,
                   "pageSize": page_size}
        if order_id is not None:
            payload["orderId"] = str(order_id)
        if task_status is not None:
            payload["taskStatus"] = int(task_status)
        if refund_type is not None:
            payload["refundType"] = int(refund_type)
        return self._request("POST", "/v1/shopTrade/refund/list", json=payload)

    def find_task(self, order_id, timeout: float = 25, interval: float = 2.0) -> dict | None:
        """按订单号轮询工单列表，拿最新一条（提交后工单入检索索引有延迟）。"""
        deadline = time.time() + timeout
        while True:
            page = self.list_tasks(order_id=order_id, page_size=20)
            rows = page.get("data") or []
            if rows:
                return rows[0]
            if time.time() >= deadline:
                return None
            time.sleep(interval)

    def wait_task(self, task_id, timeout: float = 60, interval: float = 2.0,
                  want: int = TASK_SUCCESS) -> dict | None:
        """轮询工单直到到达目标状态（默认成功=2），超时返回最后一次快照。"""
        deadline = time.time() + timeout
        snap = None
        while True:
            snap = self.detail(task_id)
            if snap.get("taskStatus") == want:
                return snap
            if time.time() >= deadline:
                return snap
            time.sleep(interval)

    # ---------------- 写操作 ----------------

    def partial_index(self, purchase_order_id, indexes: list[int]) -> list:
        """POST /v1/shopTrade/refund/partialIndex —— 标记要退哪几张票。"""
        return self._request("POST", "/v1/shopTrade/refund/partialIndex", json={
            "purchaseOrderId": int(purchase_order_id),
            "storeId": int(self.store_id),
            "selectedTicketIndexes": list(indexes),
        })

    def submit(self, order_id, items: list[dict], what: str = WHAT_BOTH,
               amount=None, reason: str = "自动化验证",
               remark: str = "auto refund") -> int:
        """POST /v1/shopTrade/refund/partial —— 提交退款申请。

        ⚠️ 返回值是**整数 refundId**（不是 taskId）。审批要 refundId + taskId 两个都传，
        所以提交后必须调 `find_task()` 回查工单拿 taskId。
        """
        return self._request("POST", "/v1/shopTrade/refund/partial",
                             json=self._refund_payload(order_id, items, what, amount,
                                                       reason, remark))

    def approval(self, refund_id, task_id, approval_status: int = APPROVAL_PASS,
                 reject_reason: str = "同意") -> None:
        """POST /v1/shopTrade/refund/approval —— 审批退款工单。

        approvalStatus **2 = 同意**（实测唯一可用值）。别被名字骗了：2 不是驳回。

        ⚠️ 后端 DTO 把 `rejectReason` 标了 @NotBlank，**连"同意"也必须传非空值**，
        否则报 `500 原因不能为空`（字段用途与校验不一致的后端怪癖）。
        所以这里默认给 "同意"，别传空串。
        """
        self._request("POST", "/v1/shopTrade/refund/approval", json={
            "refundId": int(refund_id),
            "taskId": str(task_id),
            "storeId": int(self.store_id),
            "approvalStatus": int(approval_status),
            "rejectReason": reject_reason or "同意",
        })

    def cancel_task(self, task_id) -> None:
        """POST /v2/shopTrade/refund/task/{taskId}/cancel —— 撤销工单。"""
        self._request("POST", f"/v2/shopTrade/refund/task/{task_id}/cancel",
                      params={"storeId": self.store_id})

    def retry_task(self, task_id) -> None:
        """POST /v2/shopTrade/refund/batchRetry —— 打款失败后重试。"""
        self._request("POST", "/v2/shopTrade/refund/batchRetry",
                      json={"taskId": str(task_id), "storeId": int(self.store_id)})

    # ---------------- ② 常规退：退款中心发起 → 退款审批 ----------------
    #
    # 与条件退的差别（产品口径，2026-09-30 生产 888 实测）：
    #   · **只能全量退票+退款**，没有"仅退票/仅退款"的选择余地；
    #   · **必须从退款中心发起**，发起后订单进 14/售后中，**再在退款审批菜单点通过**才真退；
    #   · 不受场次「条件退」配置影响 —— 退款中心里条件退场次(29笔)与非条件退场次(4笔)
    #     的单都列得出来（这正是它与 C 端条件退的根本区别）。

    def center_search(self, page_size: int = 50, page_index: int = 1, **extra) -> dict:
        """GET /v1/shopTrade/order/refundCenterSearch —— 退款中心列表（常规退发起入口）。

        返回 `data[]`，每行带 `orderId`（C 端订单号）、`purchaseOrderId`
        （**发起常规退要的采购单号**）、`eventSpecification`（场次名）、`status` 等。

        实测可用的查询参数（都是平铺传，不用包 webRequest）：
            `keyword=<订单号>` → **精确单条检索**（找刚下的单用这个，别翻页）；
            `eventSpecification=<场次名>` → 按场次过滤（如「非条件退场次」）；
            默认按 `addTime` **倒序**。
        ⚠️ 新下的单进这个索引有延迟（实测 3~10s），刚下单立刻查会查不到 —— 要重试。
        """
        params = {"storeId": int(self.store_id),
                  "pageSize": int(page_size), "pageIndex": int(page_index)}
        params.update(extra)
        return self._request("GET", "/v1/shopTrade/order/refundCenterSearch",
                             params=params)

    def center_find(self, order_id, timeout: float = 30,
                    interval: float = 2.0) -> dict | None:
        """在退款中心按订单号精确检索（带重试，绕过新单索引延迟）。"""
        deadline = time.time() + timeout
        while True:
            page = self.center_search(page_size=5, page_index=1, keyword=str(order_id))
            for row in (page.get("data") or []):
                if str(row.get("orderId")) == str(order_id):
                    return row
            if time.time() >= deadline:
                return None
            time.sleep(interval)

    def regular_submit(self, purchase_order_ids) -> str:
        """POST /v2/shopTrade/purchaseOrder/batchRefund —— 发起常规退（全量退票+退款）。

        ⚠️ 入参是**采购单号** `purchaseOrderId`（B 端订单行里的字段），**不是** `orderId`
        —— 实测传 848（采购单）成功，传 orderId 只会 500。

        返回**字符串 taskId**（如 "2026093017393979514"）。发起后订单进 `14/售后中`
        —— 此时**钱还没退**，必须再走 `regular_approve()`。
        """
        ids = (purchase_order_ids
               if isinstance(purchase_order_ids, (list, tuple, set))
               else [purchase_order_ids])
        return self._request("POST", "/v2/shopTrade/purchaseOrder/batchRefund",
                             json={"storeId": int(self.store_id),
                                   "purchaseOrderIdList": [int(i) for i in ids]})

    def regular_approve(self, task_id, approved: bool = True,
                        reject_reason: str = "同意") -> None:
        """POST /v2/shopTrade/purchaseOrder/batchRefund/approve —— 退款审批菜单。

        ⚠️ 字段是 **`approved`: 布尔**，与条件退审批那条 `/v1/shopTrade/refund/approval`
        的 `approvalStatus: 2` **完全不同**（照抄必错）。
        审批通过后打款由后端触发，实测 3 秒内订单落 `5/已退款`、工单 `taskStatus 2/成功`。
        """
        self._request("POST", "/v2/shopTrade/purchaseOrder/batchRefund/approve",
                      json={"taskId": str(task_id), "approved": bool(approved),
                            "rejectReason": reject_reason or "同意"})

    def wait_order_status(self, order_id, want, timeout: float = 60,
                          interval: float = 2.0) -> dict | None:
        """轮询 B 端订单直到落到目标状态（常规退要盯 14/售后中 → 5/已退款）。"""
        wanted = want if isinstance(want, (list, tuple, set)) else {want}
        deadline = time.time() + timeout
        last = None
        while True:
            last = self.get_order(order_id)
            if last and last.get("status") in wanted:
                return last
            if time.time() >= deadline:
                return last
            time.sleep(interval)

    def regular_refund(self, order_id, *, approve: bool = True,
                       timeout: float = 60) -> dict:
        """一键常规退：订单 -> 采购单号 -> 发起 -> 等售后中 -> 审批 -> 等已退款。

        返回汇总 dict（**不做任何 assert** —— 断言是用例的事）：
            {orderId, purchaseOrderId, taskId, afterSubmit, approved,
             task, order, ok}
        """
        summary: dict = {"orderId": order_id}
        row = self.get_order(order_id)
        if not row:
            summary["note"] = f"B 端查不到订单 {order_id}"
            return summary
        po = row.get("purchaseOrderId")
        summary["purchaseOrderId"] = po
        if not po:
            summary["note"] = "订单行里没有 purchaseOrderId，无法发起常规退"
            return summary

        task_id = self.regular_submit(po)
        summary["taskId"] = task_id

        after = self.wait_order_status(order_id, ORDER_STATUS_AFTER_SALE, timeout=20)
        summary["afterSubmit"] = after and after.get("status")

        if approve:
            self.regular_approve(task_id, approved=True)
            summary["approved"] = True
        else:
            summary["approved"] = False

        task = self.find_task(order_id, timeout=timeout)
        summary["task"] = task
        order = self.wait_order_status(order_id, {ORDER_STATUS_REFUNDED}, timeout=timeout)
        summary["order"] = order
        summary["ok"] = bool(order and order.get("status") == ORDER_STATUS_REFUNDED
                             and (task or {}).get("taskStatus") == TASK_SUCCESS)
        return summary

    # ---------------- 便捷组合 ----------------

    def refund_order(self, order_id, what: str = WHAT_BOTH, counts: dict | None = None,
                     reason: str = "自动化验证", approve: bool = True,
                     task_timeout: float = 90) -> dict:
        """一键：可退性 -> 试算 -> 提交 -> （审批）-> 等成功。

        `counts` 见 `build_items`：给了就是**部分退**（只退其中若干份/若干项），
        不给则整单退满。金额一律由 items 求和得出，不再直接用订单的
        `remainRefundableAmount`（部分退时那个是全额，会退多）。

        返回便于断言/落报告的汇总 dict，**故意不做任何 assert** —— 断言是用例的事。
        关键字段：refundable / items / amount / partial / calc / refundId / taskId / task / approved
        """
        summary: dict = {"orderId": order_id, "what": what}

        info = self.refundable(order_id)
        summary["refundable"] = info
        items = self.build_items(info, what=what, counts=counts)
        summary["items"] = items
        if not items:
            summary["submitted"] = False
            summary["note"] = "没有可退项（remainCount 全为 0）"
            return summary

        amount = self.items_amount(items)
        full_amount = float(info.get("remainRefundableAmount") or 0)
        summary["amount"] = amount
        summary["fullAmount"] = full_amount
        summary["partial"] = abs(amount - full_amount) > 1e-6   # 退的比可退总额少 = 部分退
        summary["calc"] = self.calculate(order_id, items, what=what, amount=amount,
                                         reason=reason)

        refund_id = self.submit(order_id, items, what=what, amount=amount, reason=reason)
        summary["refundId"] = refund_id
        summary["submitted"] = True

        task = self.find_task(order_id)
        if not task:
            summary["note"] = "提交成功但回查不到工单"
            return summary
        summary["taskId"] = task.get("taskId")
        summary["task"] = task

        if approve and task.get("taskStatus") != TASK_SUCCESS:
            try:
                self.approval(refund_id, task["taskId"])
                summary["approved"] = True
            except BAdminError as exc:
                summary["approved"] = False
                summary["approvalError"] = (
                    f"code={getattr(exc, 'code', None)} "
                    f"msg={getattr(exc, 'message', '')}"
                )
        else:
            summary["approved"] = False
            summary["approvalSkipped"] = task_status_desc(task.get("taskStatus"))

        if summary.get("taskId"):
            summary["task"] = self.wait_task(summary["taskId"], timeout=task_timeout)
        return summary


# ---------------- 命令行自测 ----------------
if __name__ == "__main__":
    import json
    import os

    if not os.environ.get("MIGU_REFUND_ORDER"):
        print("只读探测：可退性 + 试算（不会提交任何退款）")
        print("用法：MIGU_REFUND_ORDER=<orderId> python -m migu_api.refund")
        raise SystemExit(0)
    rc = RefundClient()
    order = os.environ["MIGU_REFUND_ORDER"]
    got = rc.refundable(order)
    print(json.dumps(got, ensure_ascii=False, indent=2))
    rows = rc.build_items(got)
    print("拟退项:", json.dumps(rows, ensure_ascii=False))
    print("工单:", json.dumps(
        {k: (rc.find_task(order) or {}).get(k) for k in
         ("taskId", "taskStatus", "refundTypeDesc", "actualRefundAmount", "approverName")},
        ensure_ascii=False))

"""Deterministic QQ Markdown product lists; no model-authored URLs or routing."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .watcher import baseline_view, normalize_item

MAX_PAGE_ITEMS = 10
MAX_PAGE_BYTES = 3500
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#!|>~])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]+")
_ZONE = ZoneInfo("Asia/Shanghai")


def markdown_text(value) -> str:
    """Keep all visible title text while neutralizing markup and fake mentions."""
    text = _CONTROL.sub(" ", str(value or "")).strip()
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN.sub(r"\\\1", text)


def item_url(item_id) -> str:
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,32}", item_id):
        raise ValueError("invalid product id")
    return "https://h5.m.goofish.com/item?id=" + item_id


def product_markdown(row, number=None) -> str:
    """Render one complete item; an upstream URL never enters the link target."""
    if row.get("details_available") is False:
        url = item_url(row.get("item_id"))
        label, price = "详情未恢复（商品 " + row["item_id"] + "）", "价格未知"
    else:
        item = normalize_item(row)
        url = item_url(item["item_id"])
        label, price = item["title"], item["price"]
    prefix = str(number) + ". " if number is not None else ""
    provenance = " · 补查" if row.get("source") == "recovered_current_query" else ""
    return prefix + "[" + markdown_text(label) + "](" + url + ") · " + markdown_text(price) + provenance


def _time(value):
    try:
        return datetime.fromtimestamp(float(value), _ZONE).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "未记录"


def paginate_products(rows, header, *, empty_text="当前没有可展示的商品。"):
    """Pack whole entries and reserve enough room for the final page counter."""
    entries = [product_markdown(row, index) for index, row in enumerate(rows, 1)]
    batches, batch = [], []
    for entry in entries:
        candidate = batch + [entry]
        text = header + "\n第 10000/10000 页\n\n" + "\n\n".join(candidate)
        if len(candidate) > MAX_PAGE_ITEMS or len(text.encode("utf-8")) > MAX_PAGE_BYTES:
            if not batch:
                raise ValueError("product entry exceeds display limit")
            batches.append(batch)
            batch = [entry]
            text = header + "\n第 10000/10000 页\n\n" + entry
            if len(text.encode("utf-8")) > MAX_PAGE_BYTES:
                raise ValueError("product entry exceeds display limit")
        else:
            batch = candidate
    if batch or not batches:
        batches.append(batch)
    pages = []
    for index, entries in enumerate(batches, 1):
        text = header + f"\n第 {index}/{len(batches)} 页\n\n" + ("\n\n".join(entries) or empty_text)
        if len(text.encode("utf-8")) > MAX_PAGE_BYTES:
            raise ValueError("display header exceeds limit")
        pages.append({"text": text, "item_count": len(entries)})
    return pages


def baseline_pages(record, matching_only=True):
    """Read the entire saved snapshot, independently of the model's page size."""
    baseline = baseline_view(record, 0, 10000)
    rows = [row for row in baseline["items"]
            if not matching_only or row.get("details_available") is True and row.get("matches_conditions") is True]
    total, missing = baseline["total"], baseline["details_unavailable_count"]
    state = {"active": "运行中", "awaiting_login": "等待登录", "paused": "已暂停", "cancelled": "已取消"}.get(record.get("state"), "状态未知")
    header = ("闲鱼监控基线清单\n监控 " + str(record["id"]) + " · " + state
              + " · 每 " + str(record.get("interval_minutes")) + " 分钟\n"
              + "已保存 " + str(total) + " 个商品 ID，详情 " + str(total - missing) + " 件；"
              + "当前条件匹配 " + str(baseline["known_matching_count"]) + " 件，详情未恢复 " + str(missing) + " 件。\n")
    if record.get("max_price") is not None:
        header += "条件：标价严格小于 " + markdown_text(record["max_price"]) + " 元；未知或不明确总价不算符合。\n"
    else:
        header += "未设价格上限；有详情的商品按原标价展示，价格面议仍为面议。\n"
    if baseline["required_terms"]:
        header += "品牌/型号条件：标题须包含 " + " / ".join(markdown_text(term) for term in baseline["required_terms"]) + " 中至少一项。\n"
    if baseline.get("product_terms"):
        header += "具体商品条件：标题还须包含 " + " / ".join(markdown_text(term) for term in baseline["product_terms"]) + " 中至少一项；已排除不相关商品 " + str(baseline["relevance_excluded_count"]) + " 件。\n"
    header += "本次完整展示 " + str(len(rows)) + " 件" + ("匹配商品。\n" if matching_only else "基线商品。\n")
    recovered = [row.get("recovered_at") for row in baseline["items"] if row.get("source") == "recovered_current_query"]
    if baseline["legacy"] or recovered:
        stamps = [value for value in recovered if type(value) in {int, float}]
        when = _time(min(stamps)) + (" 至 " + _time(max(stamps)) if min(stamps) != max(stamps) else "") if stamps else "未记录"
        header += "带“补查”的条目为 " + when + " 恢复时数据，无法还原首次名称和价格；缺失 ID 不冒充已匹配。\n"
    else:
        header += "首次快照观察时间：" + _time(baseline["snapshot_at"]) + "；这不是商品发布时间。\n"
    header += "下次检查：" + _time(record.get("next_run_at")) + "；仅本次平台返回范围，不保证全站覆盖。"
    pages = paginate_products(rows, header, empty_text="当前基线没有可展示的匹配商品；缺失详情数量见上方。")
    return {"pages": pages, "total_items": len(rows), "baseline_total": total,
            "known_matching_count": baseline["known_matching_count"], "details_unavailable_count": missing}


def delivery_plan(tool, pages, *, scope, sender, watch_id=None):
    data = [tool, scope, sender, watch_id, pages]
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"version": 1, "tool": tool, "plan_id": digest, "chunk_count": len(pages),
            "total_items": sum(page["item_count"] for page in pages),
            **({"watch_id": watch_id} if watch_id is not None else {})}

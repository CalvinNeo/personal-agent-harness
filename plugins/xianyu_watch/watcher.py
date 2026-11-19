"""Pure item, price and immutable baseline helpers for Xianyu monitoring."""
from __future__ import annotations
import hashlib
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

STATES = {"awaiting_login", "active", "paused", "cancelled"}
_PRICE = re.compile(r"^[¥￥]?\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?)\s*元?$")
_NON_TOTAL = re.compile(r"定金|订金|预付款|尾款|首付|分期|运费|邮费|补邮|补差|差价|起拍|占位|拍下改价|专拍链接|链接专拍")


def normalize_max_price(value):
    """JSON numbers only; store a finite positive bound, exclusive at comparison."""
    if value is None:
        return None
    if type(value) not in {int, float} or not 0 < value <= 1_000_000_000 or not math.isfinite(value):
        raise ValueError("价格上限须为大于0且不超过10亿元的有限数字；条件按严格小于执行。")
    return value


def numeric_price(value) -> Decimal | None:
    """Accept a single literal CNY price, never extract a number from prose."""
    if not isinstance(value, str):
        return None
    match = _PRICE.fullmatch(value.strip())
    if not match:
        return None
    try:
        result = Decimal(match[1].replace(",", ""))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def matches_price(row, max_price=None) -> bool:
    if max_price is None:
        return True
    normalize_max_price(max_price)
    if not isinstance(row, dict) or _NON_TOTAL.search(str(row.get("title", "")) + " " + str(row.get("price", ""))):
        return False
    amount = numeric_price(row.get("price"))
    return amount is not None and amount < Decimal(str(max_price))


def normalize_required_terms(value):
    """Explicit title evidence, with alternative brand/model spellings."""
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ValueError("相关性条件须为1至10个品牌或型号名称；标题至少命中一个。")
    terms = []
    for term in value:
        if not isinstance(term, str) or not 1 <= len(term.strip()) <= 100:
            raise ValueError("相关性名称须为1至100个字符。")
        clean = unicodedata.normalize("NFKC", term).casefold().strip()
        if clean not in terms:
            terms.append(clean)
    return terms


def relevance_terms(record):
    if record.get("required_terms") is not None:
        return normalize_required_terms(record["required_terms"])
    # Legacy watches also reject broad recommendations when every query names
    # the same Latin brand/model. Aliases are explicit, never guessed here.
    groups = [set(re.findall(r"(?<![a-z0-9])[a-z][a-z0-9-]{2,}(?![a-z0-9])",
                             unicodedata.normalize("NFKC", query).casefold()))
              for query in record.get("keywords", [])]
    shared = set.intersection(*groups) if groups else set()
    return [max(shared, key=lambda term: (len(term), term))] if shared else []


def matches_relevance(row, record):
    terms = relevance_terms(record)
    if not terms:
        return True
    title = unicodedata.normalize("NFKC", str(row.get("title") or "")).casefold()
    for term in terms:
        left = r"(?<![a-z0-9])" if re.match(r"[a-z0-9]", term) else ""
        right = r"(?![a-z0-9])" if re.search(r"[a-z0-9]$", term) else ""
        if re.search(left + re.escape(term) + right, title):
            return True
    return False


def matches_conditions(row, record):
    return matches_relevance(row, record) and matches_price(row, record.get("max_price"))


def alert_id(watch_id: str, item_id: str) -> str:
    return hashlib.sha256((watch_id + "\0" + item_id).encode()).hexdigest()[:24]


def normalize_keywords(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValueError("关键词需要1至5个非空词组。")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("关键词必须是文字。")
        text = " ".join(item.split()).strip()
        if not 1 <= len(text) <= 100:
            raise ValueError("每个关键词长度须为1至100个字符。")
        if text in {"闲鱼", "商品", "关键词", "提醒", "监控", "新商品", "搜索", "定期搜索"}:
            raise ValueError("请根据实际购买目标提供具体关键词。")
        if text not in result:
            result.append(text)
    return result


def normalize_item(row: Any) -> dict[str, str]:
    if not isinstance(row, dict):
        raise ValueError("商品数据无效。")
    item_id = str(row.get("item_id") or "").strip()
    title = str(row.get("title") or "").strip()[:300]
    price = str(row.get("price") or "价格面议").strip()[:120]
    image = str(row.get("image_url") or "").strip()
    if not re.fullmatch(r"[0-9]{1,32}", item_id) or not title:
        raise ValueError("商品编号或名称无效。")
    return {"item_id": item_id, "title": title, "price": price or "价格面议",
            "url": "https://h5.m.goofish.com/item?id=" + item_id, "image_url": image}


def baseline_snapshot(row, observed_at, *, recovered=False):
    item = normalize_item(row)
    result = {key: item[key] for key in ("item_id", "title", "price", "url")}
    result.update(observed_at=observed_at, source="recovered_current_query" if recovered else "initial_query")
    if recovered:
        result["recovered_at"] = observed_at
    return result


def baseline_view(record, offset=0, limit=20):
    ids = record.get("baseline_ids", [])
    snapshots = record.get("baseline_snapshots", {})
    unavailable = sum(item_id not in snapshots for item_id in ids)
    known_matching = sum(matches_conditions(row, record) for row in snapshots.values())
    unrelated = sum(not matches_relevance(row, record) for row in snapshots.values())
    rows = []
    for item_id in ids[offset:offset + limit]:
        snapshot = snapshots.get(item_id)
        if snapshot is None:
            rows.append({"item_id": item_id, "details_available": False,
                         "source": "legacy_ids_only", "matches_price": None})
        else:
            rows.append({**snapshot, "details_available": True,
                         "matches_price": matches_price(snapshot, record.get("max_price")),
                         "matches_relevance": matches_relevance(snapshot, record),
                         "matches_conditions": matches_conditions(snapshot, record)})
    return {"total": len(ids), "offset": offset, "limit": limit, "has_more": offset + limit < len(ids),
            "items": rows, "snapshot_at": record.get("baseline_at"), "legacy": record.get("baseline_legacy", False),
            "details_unavailable_count": unavailable, "known_matching_count": known_matching,
            "relevance_excluded_count": unrelated, "required_terms": relevance_terms(record),
            "matching_complete": unavailable == 0,
            "notice": ("旧版只保存已见商品ID，首次名称、价格和链接快照不可用；补回内容标注当前恢复时间，不代表首次快照。"
                       if record.get("baseline_legacy") else "首次成功扫描的完整商品快照；符合价格条件的数量与扫描总数分别记录。")}


def merge_pending(record: dict, rows: list[dict], now: float) -> list[dict]:
    pending = record.setdefault("pending_alerts", [])
    known = {str(item.get("alert_id")) for item in pending}
    fresh = []
    for raw in rows:
        row = normalize_item(raw)
        aid = alert_id(record["id"], row["item_id"])
        if row["item_id"] in record.setdefault("seen_ids", {}):
            continue
        if aid not in known:
            fresh.append({"alert_id": aid, "item": row, "created_at": now, "attempts": 0, "last_error": None})
            known.add(aid)
    if len(pending) + len(fresh) > 1000:
        raise ValueError("待发送商品过多，已暂停监控。")
    pending.extend(fresh)
    return fresh


def mark_alert_delivered(record: dict, aid: str, now: float) -> None:
    pending = record.setdefault("pending_alerts", [])
    found = next((item for item in pending if item.get("alert_id") == aid), None)
    if found is None:
        return
    item_id = str((found.get("item") or {}).get("item_id") or "")
    if item_id and item_id not in record.setdefault("seen_ids", {}) and len(record["seen_ids"]) >= 10000:
        raise ValueError("已见商品过多，已暂停监控。")
    if item_id:
        record["seen_ids"][item_id] = now
    record["pending_alerts"] = [item for item in pending if item is not found]


def compact_record(record: dict) -> dict:
    result = {key: record.get(key) for key in ("id", "keywords", "interval_minutes", "max_results", "max_price", "state",
               "created_at", "last_run", "next_run_at", "last_error", "last_query", "metadata_refreshed_at")}
    baseline = baseline_view(record, 0, 0)
    result.update(price_comparison="exclusive_lt", pending_count=len(record.get("pending_alerts") or []),
                  required_terms=relevance_terms(record), relevance_evidence="title",
                  baseline_relevance_excluded_count=baseline["relevance_excluded_count"],
                  seen_count=len(record.get("seen_ids") or {}), baseline_total=baseline["total"],
                  baseline_known_matching_count=baseline["known_matching_count"],
                  baseline_matching_complete=baseline["matching_complete"],
                  baseline_details_unavailable_count=baseline["details_unavailable_count"])
    return result

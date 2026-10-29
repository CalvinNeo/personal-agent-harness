"""Owner-scoped task identifiers and bounded QQ text status boards."""
from __future__ import annotations

import hashlib
import html
import re
import secrets
import unicodedata
from datetime import datetime, timezone

TERMINAL = frozenset({"completed", "failed", "timed_out", "interrupted"})
STATES = {"queued": "排队中", "running": "执行中", "completed": "已完成", "failed": "失败", "timed_out": "超时", "interrupted": "已中断"}
PHASES = {"queued": "等待执行", "starting": "启动 Codex", "running": "分析任务", "tool_running": "调用工具", "finalizing": "整理结果", **{k: STATES[k] for k in TERMINAL}}
ID_RE = re.compile(r"^[a-f0-9]{8,16}$")

# These fast paths cover explicit routing and tasks that clearly need an
# execution agent.  Ambiguous requests are left to the normal LLM/tool route;
# neither a keyword nor the length of a message alone can create a job.
_CODEX = r"(?<![A-Za-z0-9_])codex(?![A-Za-z0-9_])"
_TASK_ACTION = re.compile(r"(?:分析|研究|调研|检索|搜索|查(?:一下|下|看|找|询|查)|检查|核对|验证|比较|对比|评估|审查|审计|诊断|排查|定位|找出|修复|修改|解决|优化|实现|开发|编写|写(?:一个|个|一段|段|代码|程序|脚本|测试)|生成|制作|整理|总结|设计|规划|计算|处理|完成|执行|跑(?:一下|下)?|测(?:试|一下)|看(?:一下|下|看)|解答|回答|做(?:一个|个|一份|份)|标注|标记|注释|批注|圈出|框选|高亮|贴标签|加标签|\b(?:annotate|annotation|label|tag|mark|highlight)\b|work\s+on|analy[sz]e|research|search|review|audit|debug|investigate|fix|implement|develop|write|generate|create|build|compare|test|run|check|solve|find|summari[sz]e|explain)", re.I)
_CODE_DOMAIN = re.compile(r"(?:代码|程序|脚本|函数|算法|仓库|项目|系统|依赖|漏洞|堆栈|调用栈|报错|异常|单元测试|测试用例|性能|数据库|接口|服务|部署|日志|网页|网站|应用|页面|小程序|组件|爬虫|\b(?:code|repo(?:sitory)?|project|script|function|algorithm|stack\s*trace|bug|pull\s+request|pr|python|javascript|typescript|sql|api|html|css|react|vue|docker|rust|go|java|c\+\+)\b)", re.I)
_CODE_ACTION = re.compile(r"(?:审查|审计|排查|诊断|定位|修复|实现|开发|编写|重构|调试|优化|补(?:齐|充)|生成|写(?:一个|个|一段|段|代码|程序|脚本|测试)|搭建|构建|跑(?:一下|下)?|执行|\b(?:review|audit|debug|investigate|fix|implement|develop|write|generate|build|refactor|test|run)\b)", re.I)
_WRITE_MODE_NAME = r"(?:工作区写入模式|可写(?:工作区)?模式|workspace-write(?:\s+mode)?|write\s+mode)"


def instruction_text(text: str) -> str:
    """Only direct prose participates in routing, never quoted/fenced payloads."""
    # QQ clients and copied messages sometimes leave HTML entities such as
    # ``&#x20;`` in the visible text. Decode before intent matching so a
    # harmless entity cannot change the route. NFKC also folds full-width
    # Codex and punctuation variants.
    text = html.unescape(str(text or ""))
    text = unicodedata.normalize("NFKC", text).strip()
    # A leading mention label (for example ``@calvin的智能体``) is addressing
    # the bot, not the task. AstrBot normally removes At components, but some
    # QQ clients serialize them into Plain text; tolerate both forms.
    text = re.sub(r"^(?:(?:@[\w\u4e00-\u9fff.-]+(?:的(?:智能体|机器人))?)|(?:\[At:[^\]]+\]))\s*", "", text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"(?m)^\s*>.*$", " ", text)
    text = re.sub(r"[“‘「『][\s\S]*?[”’」』]", " ", text)
    text = re.sub(r'"[^"\n]*"', " ", text)
    # A literal `codex` is a common way to name the engine, not an instruction
    # from a code sample. Other inline code is not used for intent decisions.
    text = re.sub(r"`([^`\n]*)`", lambda m: "codex" if re.fullmatch(_CODEX, m[1], re.I) else " ", text)
    return re.sub(r"\s+", " ", text).strip()


def codex_opt_out(text: str) -> bool:
    text = instruction_text(text)
    return bool(re.search(r"(?:不要|别|不用|无需|不必|禁止|不想|不愿|不需要|不希望|避免|无需)(?:再|给我|帮我|让|调用|使用|用|通过|交给|交由|委托|启动|转交|\s){0,12}" + _CODEX, text, re.I)
                or re.search(r"\b(?:do\s+not|don't|never|without|avoid)\s+(?:(?:want|need)\s+)?(?:(?:use|using|call|calling|ask|asking|run|running|invoke|invoking)\s+)?" + _CODEX, text, re.I))



def quoted_job_id(text: str) -> str | None:
    """Recognize only our full board marker; quoted prose never becomes a task."""
    matches = set(re.findall(r"【Codex 任务 #([a-fA-F0-9]{16})】", str(text or "")))
    return next(iter(matches)).lower() if len(matches) == 1 else None


def new_codex_conversation(text: str) -> bool:
    """Only explicit conversation controls opt out of a quoted continuation."""
    direct = instruction_text(text)
    return bool(re.match(
        r"(?:(?:请|麻烦|帮我|这次|先)\s*)*(?:新建|开启|开始|创建|另开|重新开)(?:一个|一条|个|条)?(?:新的?|全新)?\s*codex\s*(?:任务|会话)|"
        r"(?:(?:请|麻烦|帮我|这次|先)\s*)*(?:新建|开启|开始|创建|另开)(?:一个|一条|个|条)?(?:新的?|全新)(?:任务|会话)(?:交给|让|用)\s*codex|"
        r"(?:please\s+)?(?:start|create|open)\s+(?:a\s+)?new\s+codex\s+(?:task|session|conversation)\b",
        direct, re.I))


def continuation_eligible(text: str) -> bool:
    direct = instruction_text(text)
    return bool(direct and len(str(text or "")) <= 12000 and not direct.startswith("/")
                and not codex_opt_out(direct) and not new_codex_conversation(direct))

def _local_task(text: str) -> bool:
    # Reminder creation must remain in the reminder plugin, even if its future
    # reminder text happens to mention Codex, code, or a complex workflow.
    return bool(re.search(r"(?:提醒我|叫我|通知我|提醒一下我|设置.{0,12}提醒|(?:创建|建立|安排|定个).{0,20}提醒|\bremind\s+me\b)", text, re.I))


def _direct_request(text: str, *, named_engine=False) -> bool:
    """An action must be requested now, not just mentioned in a discussion."""
    text = text.strip()
    if re.match(r"^(?:(?:我|你|他|她|我们|他们|有人)(?:昨天|前天|之前|刚才|已经|曾经)|(?:昨天|前天|之前|已经|曾经))", text):
        return False
    # Only use this guard for ordinary topic questions. An explicit Codex
    # request can contain the same words as its deliverable, for example
    # "用 Codex 处理这张图，标注到底都是什么东西". The narrower capability
    # checks in natural_codex_request handle actual questions about Codex.
    if (not named_engine and re.search(
        r"(?:有什么用|是干什么的|是什么|怎么(?:用|运行|实现)|如何(?:用|运行|实现)|(?:需要|应该|是否).{0,20}(?:吗|么)[?？]?$)",
        text,
    ) and not re.match(r"^(?:请|帮我|帮忙|麻烦|能否|能不能|可不可以|你能|你可以|我想|我希望|我需要)", text)):
        return False
    # Peel direct-address/politeness words so "please don't fix" remains a
    # prohibition. A negative clause inside the requested analysis is kept.
    value = re.sub(r"^(?:(?:请|麻烦|劳驾|拜托)(?:你)?|帮我|替我|帮忙|给我|你(?:能不能|能否|可不可以|可以|能)|(?:能不能|能否|可不可以)|我(?:想让你|希望你|需要你|要你))\s*", "", text, flags=re.I)
    value = re.sub(r"^(?:请|麻烦|帮我|帮忙|替我|给我)\s*", "", value)
    if re.match(r"^(?:不要|不用|别|无需|不必|先别|暂时别|暂停|停止|取消|\b(?:do\s+not|don't|never|stop|cancel)\b)", value, re.I):
        return False
    if not named_engine and re.match(r"^(?:解释|介绍|讲讲|说说|告诉我|科普|学习|了解|\b(?:explain|describe|tell\s+me|what|why|how)\b)", value, re.I):
        return False
    addressed = value != text
    if addressed:
        return True
    if re.match(r"^(?:请|麻烦|帮我|帮忙|替我|给我|我(?:想|需要|要)|先|\b(?:please|can\s+you|could\s+you|would\s+you|help\s+me|i\s+(?:want|need))\b)", text, re.I):
        return True
    if named_engine:
        return bool(re.match(r"^(?:用|使用|调用|让|交给|交由|转交给|委托|启动|找|叫|(?:这个|这项|这件|这次|把).{0,40}(?:交给|交由|让|用)|" + _CODEX + r"|\b(?:use|ask|call|invoke|run)\b)", text, re.I))
    return bool(re.match(r"^(?:分析|研究|调研|检索|搜索|查找|查阅|收集|审查|审计|排查|诊断|定位|修复|实现|开发|编写|重构|调试|优化|补齐|生成|写|搭建|构建|跑|执行|\b(?:research|investigate|search|collect|review|audit|debug|fix|implement|develop|write|generate|build|refactor|test|run)\b)", text, re.I))


def _requested_mode(text: str) -> str:
    """Only an explicit routing preamble can select workspace write access.

    A mode name in the task body is data, not a permission request. Explicit
    read-only or a prohibition on writing always wins over a positive selector.
    The submission method independently enforces the sender's write allowlist.
    """
    if re.search(r"只读|\bread[-\s]only\b", text, re.I):
        return "read-only"
    if re.search(r"(?:不要|别|不用|无需|不必|禁止|不允许|不需要|不希望|不使用|不启用|避免)[^。！？!?;；]{0,24}(?:写入|可写|修改文件|改动文件|workspace-write|write\s+mode)", text, re.I):
        return "read-only"
    if re.search(r"\b(?:do\s+not|don't|never|without|avoid|no)\b[^.!?;]{0,50}\b(?:writ(?:e|ing)|workspace-write|modif(?:y|ying))\b", text, re.I):
        return "read-only"
    engine = re.search(_CODEX, text, re.I)
    if engine is None:
        return "read-only"
    # Direct forms: "用 Codex 的可写模式 ...", "让 Codex 切换到可写模式 ...",
    # "Use Codex in workspace-write mode ...". Do not scan after the task starts.
    suffix = text[engine.end():].lstrip(" ,，:：")
    selection = r"(?:(?:的|以|用|使用|采用|开启|启用|切换到|切到|进入|在|选择|设置为|运行于)\s*|(?:in|with|using|under|into|to|switch\s+to|run\s+in)\s+)?"
    selected = re.match(selection + _WRITE_MODE_NAME + r"(?![A-Za-z0-9_-])", suffix, re.I)
    if selected:
        # "Codex 的可写模式说明文档" names reference material, whereas
        # "Codex 的可写模式修复..." selects an execution mode.
        following = suffix[selected.end():].lstrip()
        if not re.match(r"(?:的|相关|说明|文档|机制|设计|风险|为什么|如何|怎么|有什么|documentation\b|risks?\b|description\b)", following, re.I):
            return "workspace-write"
    # Also allow a mode selected before the engine name. Keep the entire
    # prefix grammatical so "分析使用可写模式的风险" cannot count as a selector.
    prefix = text[:engine.start()].strip()
    polite = r"(?:(?:请|麻烦|帮我|帮忙|替我)\s*)*"
    before_zh = polite + r"(?:以|用|使用|采用)\s*" + _WRITE_MODE_NAME + r"\s*(?:来|下)?\s*(?:调用|运行|启动|使用)?\s*"
    before_en = r"(?:please\s+)?(?:use|run|invoke|start)\s+" + _WRITE_MODE_NAME + r"\s+(?:for|with|to\s+(?:run|invoke|start))\s+"
    if re.fullmatch(before_zh, prefix, re.I) or re.fullmatch(before_en, prefix, re.I):
        return "workspace-write"
    return "read-only"


def natural_codex_request(text: str, *, auto_complex: bool = True):
    """Return (mode, original task) for direct requests, otherwise None.

    Read-only is the default even for a request to fix code. Workspace write
    mode is a separate, explicit request and is checked again by the caller.
    """
    original = str(text or "").strip()
    value = instruction_text(original)
    if not value or value.startswith("/") or codex_opt_out(value) or _local_task(value):
        return None
    # Describing someone else's instruction or asking how to phrase a command
    # is not an instruction to execute it now.
    if re.match(r"^(?:转发|引用|他说|她说|有人说|例如|比如|示例|假如|假设|如果|教程|说明书|他说过|原话|\b(?:quote|example|suppose|if)\b)", value, re.I):
        return None
    explicit = bool(re.search(_CODEX, value, re.I))
    if explicit:
        if not _direct_request(value, named_engine=True):
            return None
        # Capabilities, usage and status are handled as chat/status, not jobs.
        if re.search(r"(?:什么是|什么叫|如何使用|怎么使用|怎么用|如何用|怎么调用|如何调用|介绍(?:一下)?|解释(?:一下)?).{0,8}" + _CODEX, value, re.I):
            return None
        if re.search(_CODEX + r"\s*(?:是(?:什么|啥)|是什么|能做什么|可以做什么|有什么(?:用|功能)|怎么用|如何使用|的(?:功能|作用|区别|价格|用法)|\b(?:is\s+what|capabilities|pricing)\b)", value, re.I):
            return None
        if re.search(r"\b(?:what\s+is|how\s+(?:do\s+i|to)\s+use|tell\s+me\s+about|explain)\s+" + _CODEX, value, re.I):
            return None
        route_verb = re.search(r"(?:用|使用|调用|利用|通过|让|请|交给|交由|转交给|委托|启动|找|叫|\b(?:use|using|ask|call|invoke|run|with|via|to)\b)\s*(?:一下|下)?\s*" + _CODEX, value, re.I)
        addressed = re.match(_CODEX + r"(?:\s|[,，:：]|帮|请|来|给|分析|研究|检查|审查|修复|写|做|执行|处理|跑|查|看)", value, re.I)
        # Also accept the engine named at the end of an otherwise direct task.
        if (route_verb or addressed) and _TASK_ACTION.search(re.sub(_CODEX, "", value, flags=re.I)):
            return _requested_mode(value), original
        return None
    if not auto_complex:
        return None
    if not _direct_request(value):
        return None
    # Ordinary image questions use the vision provider and recent-image cache.
    if re.search(r"(?:图片|照片|截图|这张图|那张图|刚才.{0,4}图|图.{0,5}(?:热量|卡路里)|识图|识别图片|看图)", value) and not _CODE_ACTION.search(value):
        return None
    # Asking about a topic is different from asking for an engineering task.
    if re.match(r"^(?:什么是|什么叫|如何|怎么|为什么|为啥|解释|介绍|讲讲|说说|请问|\b(?:what\s+is|how\s+(?:does|do)|why|explain|tell\s+me\s+about)\b)", value, re.I):
        return None
    if _CODE_ACTION.search(value) and _CODE_DOMAIN.search(value):
        return "read-only", original
    # A research job needs multiple kinds of work, not merely "search" or a
    # long message. This covers investigation -> comparison -> deliverable.
    research = bool(re.search(r"(?:调研|研究|检索|搜索|查找|查阅|收集|\b(?:research|investigate|search|collect)\b)", value, re.I))
    evaluate = bool(re.search(r"(?:对比|比较|交叉验证|核实|验证|评估|分析|\b(?:compare|verify|evaluate|analy[sz]e)\b)", value, re.I))
    deliver = bool(re.search(r"(?:报告|表格|方案|建议|来源|引用|结论|证据|\b(?:report|table|recommendation|sources|citations|evidence)\b)", value, re.I))
    multi_step = bool(re.search(r"(?:先.{2,}再|然后|最后|分别|逐一|多(?:个|家|种|篇)|三家|两家|\b(?:then|each|multiple|several)\b)", value, re.I))
    if research and evaluate and (deliver or multi_step):
        return "read-only", original
    return None


def owner_key(umo: str, sender: str) -> str:
    return hashlib.sha256((umo + "\0" + sender).encode()).hexdigest()


def job_key(umo: str, sender: str, message_id: str) -> str:
    if not message_id:
        return secrets.token_hex(8)
    return hashlib.sha256((umo + "\0" + sender + "\0" + message_id).encode()).hexdigest()[:16]


def stamp(value) -> float | None:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp() if str(value).endswith("Z") else datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


def safe_text(value, limit=5500) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}", r"\1[已隐藏]", text)
    text = re.sub(r'(?i)((?:api[_-]?key|appsecret|access[_-]?token|secret[_-]?key)\s*[=:]\s*[\"\']?)[^\s\"\',;}]{6,}', r'\1[已隐藏]', text)
    return text if len(text) <= limit else text[:limit] + "\n[结果较长，已截断]"


def board(job: dict, now: float, *, accepted=False) -> str:
    jid = str(job.get("id", ""))[:16]
    state = str(job.get("state", ""))
    created = stamp(job.get("created_at")) or now
    finished = stamp(job.get("finished_at")) or now
    active = stamp(job.get("last_event_at"))
    heartbeat = stamp(job.get("heartbeat_at"))
    elapsed = duration((finished if state in TERMINAL else now) - created)
    lines = []
    if accepted:
        lines.append("已受理 Codex " + ("图片任务（独立临时目录）" if job.get("execution_scope") == "isolated-image-task" else "工作区写入任务" if job.get("mode") == "workspace-write" else "只读任务"))
    lines.extend([f"【Codex 任务 #{jid}】", f"状态：{STATES.get(state, '查询中')} · 阶段：{PHASES.get(str(job.get('phase')), '处理中')}", f"已用时：{elapsed}"])
    conversation_id = str(job.get("conversation_id", ""))
    parent_id = str(job.get("parent_job_id", ""))
    if re.fullmatch(r"[a-f0-9]{16}", conversation_id):
        if re.fullmatch(r"[a-f0-9]{16}", parent_id):
            lines.append(f"同会话续接：#{conversation_id} · 上一任务 #{parent_id}")
        else:
            lines.append(f"新会话：#{conversation_id}")
    image_count = job.get("context_images") or job.get("input_images")
    if image_count:
        lines.append(f"会话原图：{image_count} 张。" if parent_id else f"已附原图：{image_count} 张。")
        if job.get("artifact_status") == "ready":
            lines.append(f"结果图片：{len(job.get('artifacts') or [])} 张。")
        elif job.get("artifact_status") == "none":
            lines.append("结果图片：本次未生成，只有文字结果。")
        elif job.get("artifact_status") == "rejected":
            lines.append("结果图片：输出校验失败，未回传不合规图片。")
        elif state not in TERMINAL:
            lines.append("生成的结果图片将自动回传。")
    if active:
        lines.append("最近活动：" + duration(now - active) + "前")
    elif state == "queued":
        lines.append("最近活动：等待可用执行位")
    if state == "running" and heartbeat:
        lines.append("执行心跳：" + duration(now - heartbeat) + "前")
    if state in TERMINAL:
        delivery = {"pending": "等待回传", "delivered": "已回传", "failed": "自动回传失败，可手动取结果"}.get(job.get("delivery"))
        if delivery:
            lines.append("回传：" + delivery)
        lines.append(f"查看结果：/codex result {jid[:8]}")
    else:
        lines.append(f"查进度：/codex status {jid[:8]}")
        lines.append("约 1 分钟后自动更新；完成后回传结果。" if accepted else "后台继续执行，完成后回传结果。")
    return "\n".join(lines)


def query(text: str):
    """Only explicit command verbs; never reinterpret arbitrary task text."""
    match = re.fullmatch(r"(status|状态|进度|tasks|列表|任务列表|result|结果)(?:\s+([a-fA-F0-9]{8,16}))?", text.strip(), re.I)
    if not match:
        return None
    verb, jid = match.groups()
    op = "list" if verb.lower() in {"tasks", "列表", "任务列表"} else "result" if verb.lower() in {"result", "结果"} else "status"
    return op, jid.lower() if jid else None


def natural_query(text: str, quote_text: str = ""):
    text = re.sub(r"\s+", "", instruction_text(text)).strip("?？。！!")
    if not text or text.startswith("/") or codex_opt_out(text):
        return None
    # Natural queries must name Codex, or explicitly reply to a status board.
    explicit = bool(re.search(r"codex", text, re.I))
    quoted = bool(re.search(r"(?:已受理\s*Codex|Codex\s*任务\s*#)", quote_text, re.I))
    if not explicit and not quoted:
        return None
    match = re.search(r"(?<![a-fA-F0-9])#?([a-fA-F0-9]{8,16})(?![a-fA-F0-9])", text)
    jid = match.group(1).lower() if match else None
    if match:
        text = text[:match.start()] + text[match.end():]
    if jid is None:
        match = re.search(r"Codex\s*任务\s*#([a-fA-F0-9]{8,16})", quote_text, re.I)
        jid = match.group(1).lower() if match else None
    value = re.sub("codex", "", text, flags=re.I)
    value = re.sub(r"(?:请|帮我|麻烦|现在|当前|这个|这条|那个|刚才|刚刚|的|任务)", "", value)
    if re.fullmatch(r"(?:看(?:一下|下|看)?|查(?:一下|下|询)?|查查)?(?:状态|进度)(?:怎么样|如何|呢|到哪了|到哪儿了)?|(?:完成|结束|好了|做完|跑完)(?:了)?(?:吗|么|没|没有)|(?:跑|执行|运行)到哪(?:了|儿了)|(?:怎么样|如何|还在(?:跑|执行|运行)吗)(?:了)?|status|progress", value, re.I):
        return "status", jid
    if value.lower() in {"结果", "看下结果", "查看结果", "看看结果", "结果呢", "结果怎么样", "result", "results"}:
        return "result", jid
    if value.lower() in {"列表", "最近列表", "我列表", "我最近列表", "我的列表", "我的最近列表", "有哪些", "我有哪些", "我的有哪些", "最近有哪些", "我最近有哪些", "我的最近有哪些", "tasks", "list"}:
        return "list", None
    return None

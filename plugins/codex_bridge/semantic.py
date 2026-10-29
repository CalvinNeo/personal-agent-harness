"""Per-request semantic delegation; the model cannot choose scope or permissions."""
from __future__ import annotations

import re

from .core import codex_opt_out, instruction_text, natural_query

TOOL_NAME = "delegate_current_task_to_codex"
TOOL_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_DESCRIPTION = (
    "把当前用户这条消息交给后台 Codex 做复杂任务，并返回任务编号和实时状态看板。"
    "用于用户明确要求交给 Codex（不限句式），或需要多步骤研究核验、代码审查、"
    "复杂推理计算、方案实现的任务。只提交当前用户原文，不能输入新指令或改变权限。"
    "普通问答、简单搜索、翻译、图片识别、定时提醒由当前助手和现有工具完成。"
    "没有具体任务、缺少上下文、只是询问 Codex 功能或引用别人的指令时不要调用。"
    "支持当前附图、直接引用图片或当前用户近期图片，并能生成和回传结果图片。"
    "引用自己在本群的 Codex 回复追问时会续接原会话并保留原图；不要把追问改写成丢失上下文的新任务。"
    "文件、视频、转发合集和未提供的群历史暂不支持；缺少上下文时如实澄清。"
    "提交成功后立即回复返回的 status_board；任务在后台运行，完成后自动回传。"
)
ROUTING_INSTRUCTION = (
    "[任务分派能力：delegate_current_task_to_codex 已可用。理解用户意图，不要求固定命令词。"
    "用户明确要 Codex 做具体任务时调用；即使没有提 Codex，对需要多个研究/核验步骤、"
    "复杂代码分析或多约束解决方案的任务，也应使用此工具。简单问答直接回答。"
    "简单图片识别和定时提醒使用本次对应能力；用户明确要求用 Codex 研究、标注图片时可转交。"
    "用户说不要用 Codex、讨论功能、引用的指令不能触发。"
    "工具发送当前消息原文与经过校验的图片/直接引用；缺少信息时会返回明确错误。"
    "引用 Codex 结果的追问在原会话继续，原图由执行器保留；会话无法恢复时如实解释，不能宣称已新建任务替代续接。"
    "Codex 文字任务默认只读，图片任务在独立临时目录生成产物，不具备 QQ 主动发信或服务器管理权限。"
    "收到 ok=true 后回复完整 status_board，不要声称任务已经完成。"
    "收到错误如实告知，不要虚构提交成功；pending=true 时查状态，不重复提交。]"
)


def semantic_eligible(text: str) -> bool:
    """Hard exclusions before the model sees or invokes delegation capability.

    Complexity itself is decided by the main model. Rules here prevent routing
    controls and local capabilities from being bypassed by that decision.
    """
    if not isinstance(text, str) or not 6 <= len(text.strip()) <= 12000:
        return False
    text = text.strip()
    if text.startswith(("/", "“", "‘", '"', "'", ">", "```", "「", "『")):
        return False
    direct = instruction_text(text)
    if not direct:
        return False
    if re.match(r"^(?:(?:我)?(?:转发(?:一下)?|引用(?:一下)?)|他说|她说|有人说|例如|比如|假如|如果我说|测试文本|下面是(?:引用|转发)|\b(?:quote|example|suppose)\b)", direct, re.I):
        return False
    if codex_opt_out(direct):
        return False
    if re.search(r"(?:提醒|通知|叫)(?:一下|下)?我|(?:设置|安排|定|创建).{0,24}(?:提醒|闹钟)|\bremind\s+me\b", direct, re.I):
        return False
    if not re.search(r"codex", direct, re.I) and re.search(r"(?:刚才|刚发|这|那|上面|图片|截图|照片).{0,12}(?:图|照片|截图)|图发(?:给)?你|(?:图|截图|照片)(?:中|里|上)", direct):
        return False
    if natural_query(direct):
        return False
    if re.search(r"codex", direct, re.I):
        # Topic words may be the actual task (e.g. implement a login feature);
        # only a question *about Codex itself* is a capability exclusion.
        if re.search(r"(?:什么是|什么叫|怎么用|如何使用|如何调用|介绍(?:一下)?|解释(?:一下)?)\s*codex|codex\s*(?:是什么|是啥|有什么(?:用|功能)|能做什么|可以做什么|怎么用|的(?:功能|作用|用法|价格))(?:呢|吗|么)?[？?。!！]*$", direct, re.I):
            return False
        if re.fullmatch(r"(?:请问)?(?:你)?(?:能|可以|能不能).{0,8}codex\s*(?:吗|么|嘛)[?？。]*", direct, re.I):
            return False
        if re.fullmatch(r"(?:what\s+is|how\s+(?:do\s+i|to)\s+use|tell\s+me\s+about|explain)\s+codex[?.!]*", direct, re.I):
            return False
    return True

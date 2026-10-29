# AstrBot 本地提醒插件

该插件在 AstrBot 进程内解析、持久化和调度提醒，不调用 Codex。到期时通过 QQ 官方适配器发送群主动消息，并使用创建者的 QQ OpenID 生成 `@`。

## 部署

将本目录复制到服务器的 `/var/lib/astrbot/data/plugins/reminder/`，并创建 `/var/lib/astrbot/data/config/reminder_config.json`。`allowed_umos` 必须填已批准群的完整 UMO，例如：

```json
{
  "allowed_umos": ["private_qq_bot:GroupMessage:<群 OpenID>"],
  "allowed_senders": []
}
```

空的 `allowed_umos` 会拒绝所有请求。重启 `astrbot.service` 后插件会从 `reminders.json` 恢复未到期任务。提醒文件由插件原子写入并设置为 `0600`。

## 用法

在允许的 QQ 群里 @机器人，可以直接说：

```text
两分钟后提醒我喝水
请在半小时后提醒我开会
提醒我明天下午三点开会
明天早上八点叫我起床
```

时间统一按北京时间解释；含糊的“一会儿”、没有说明上午/下午的“三点”会要求补充。只支持自己的一次性提醒，最长七天。否定、引用、讨论提醒功能不会自动创建任务。以“已设置提醒（ID）”和具体时间的回复为准。普通聊天仍走原来的模型。配合 `intent_router` 的 `mode=light` 时，规划器只提供候选规范化文本，最终仍由本插件确定性解析和时间/权限校验；失败时回到当前正则路径。

原有命令继续可用：

```text
/提醒 2分钟后提醒我喝水
/提醒 2026-10-28 18:30 开会
/提醒列表
/提醒取消 <ID>
```

QQ 开放平台必须对目标群开启机器人主动发言/主动推送权限；用户关闭主动消息时，QQ 可能返回 `40034105`，插件会在 AstrBot 日志中记录失败。

审计：`journalctl -u astrbot.service --since '30 minutes ago' --no-pager`，查找 `Reminder created id=`、`Failed to send reminder`、`scheduler started`。持久化记录在 `reminders.json`；发送成功后删除。群白名单、发送者限制和每人/每群数量限制对自然语言和命令一致生效。

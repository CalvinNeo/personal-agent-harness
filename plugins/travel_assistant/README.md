# 出行助手

QQ 群内 @ 机器人、已批准管理员私聊可用。所有工具只读，不下单、不支付、不抢票，不索取身份证、密码或票务 Cookie。

## 能力

- `travel_search_tickets`：查询 12306 同车直达班次、指定席别余票与部分核价；查询 Google Flights 公开的单程、1 成人、经济舱报价。
- `travel_filter_tickets`：在当前群/私聊中，继续筛选当前发送者自己的最近查询。条件和排序逐次保留，20 分钟后失效，不自动刷新票价或延长缓存有效期。换日期、路线或席别需重新查询。
- `travel_map`：生成高德/百度官方地点搜索、驾车、公交、步行、骑行导航入口。不是实时路况 API；不读取用户定位、不编造耗时和距离。
- `travel_capabilities`：列出真实能力与限制。

## 使用示例

直接发自然语言，必要时 @ 机器人：

> 帮我找明天上海虹桥到北京南，二等座 700 以内，8 点后出发，优先耗时短。
>
> 查 11 月 7 日上海到北京，单程经济舱，2000 内直飞，不要浦东，当天 18 点前到。
>
> 再把预算降到 600，其他条件保持不变。
>
> 从南京南站到南京禄口机场，公交怎么走？

没有明确出发地、目的地、日期时先澄清。相对日期按 `Asia/Shanghai` 解释，航班出发/到达时刻是当地时间。到达时间限制若指当天，必须同时设置 `arrival_date`，跨日到达不能当作当天。

不依赖模型的命令（`/出行` 等同 `/travel`）：

```text
/travel help
/travel train 2026-11-07 上海虹桥 北京南 max_price=700 depart_after=08:00 seat=二等座 sort=duration
/travel flight 2026-11-07 上海 北京 max_price=2000 nonstop=true exclude_from_airport=PVG arrive_before=18:00 arrival_date=2026-11-07
/travel filter max_price=600
/travel map 南京南站 city=南京
/travel route 南京南站 南京禄口机场 city=南京 mode=transit
```

日期仅为示例，使用时换成实际出行日。空格分隔命令参数；含空格的地名请使用自然语言或机场三字码。

## 筛选与数据边界

- 预算为每人单程人民币。可设出发/到达时间窗、到达日期、时长、价格/耗时/出发时间排序；高铁席别、有票、数量；机票直飞/中转、允许/排除航司、允许/排除机场、行李和退改要求。
- `matching` 表示满足已设置且可核实的条件；`needs_verification` 必须单列说明缺少哪些证据，不能说成完全符合。明确不符合任一条件的结果直接排除。
- 机票价格是公开页面起价，不是锁价或支付总价承诺；票量、托运行李、退改、多人同价通常未知。用户要求这些硬条件时列入待核实。当前不支持往返、多成人或其他舱位报价，不得静默改成单人单程。
- 12306 按通常 15 天预售期验证；未开售不是无票。默认仅 G/D/C、二等座、明确有票。城市名可能映射主要高铁站，结果明确披露，不代表搜遍全市车站。
- 每次最多对 24 个满足非价格条件的列车班次核价，3 并发；剩余无价结果不算符合预算，不保证全量最低价。最多缓存 600 个结果，截断时披露。
- 航班页面字段或机场无法核实就拒绝当成匹配；没有班次数据/被限流不等于无票。遇 403/429、验证码、异常流量或不支持页即停止，不切换出口或绕过。
- 缓存绑定群/私聊和发送者，权限空白名单默认拒绝。结果原查询时间必须显示；无购票执行能力。

## 配置和文件

功能源码保存在工作区 `utils/plugins/travel_assistant/`，AstrBot 可通过只读 `/opt/agent-bot-plugins/travel_assistant/` 兼容入口加载。`data/plugins/travel_assistant/` 只放加载链接，工作区布局见 `docs/WORKSPACE.md`。

`data/config/travel_assistant_config.json`：

- `allowed_umos`：精确批准会话白名单；空列表拒绝全部。部署只沿用原 QQ 群和管理员私聊，不扩大范围。
- `allowed_senders`：可选发送者限制。
- `flight_proxy`：空，或现有 `http://127.0.0.1:7890`；只用于公开机票页面，不改全局代理、不影响 12306。
- `data_dir`：服务可写数据路径，模板为 `/var/lib/astrbot/data/travel_assistant`，集中工作区中对应 `data/astrbot/data/travel_assistant/`。

数据目录只缓存公开车站目录与最长期限 20 分钟的去身份查询结果，不保存票务账号或原始页面/订票密钥。文件 0600、目录 0700。结果访问仍按当前身份验证。过期记录在后续读取/写入时失效或淘汰，不会作为有效结果返回。

发布源码/校验清单在 `utils/source/travel-integration-20261101/`；维护测试在 `utils/tests/travel-integration-20261101/`；脱敏验收结果在 `data/astrbot/data/validation/travel-integration-20261101/`；原文件和回滚清单在 `data/backups/travel-integration-20261101/`。不修改旧发布的校验清单。

部署只重启 `astrbot`，其他服务不重启。先验证基线哈希、无排队/执行任务，保存原文件；新插件加载/QQ 连接失败自动恢复旧文件并撤销新插件与配置。

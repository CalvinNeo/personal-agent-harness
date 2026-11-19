# 闲鱼登录、搜索与新商品监控

这是可选功能，由 `plugins/xianyu_watch/` 和独立的 `services/xianyu_server.py` 组成。插件只访问经过鉴权的回环 API；账号 Cookie 留在独立 `xianyu` 服务账户下，Codex 通过当前 QQ 事件绑定的工具调用搜索和监控，不读取 profile 或服务 token。

## 安装

以下命令从仓库根目录执行，适用于已有 AstrBot 标准目录的 Linux/systemd 安装。已有账号、凭据或状态不能被空文件覆盖。

1. 创建不能交互登录的服务账户和受限共享组。`astrbot` 仅通过组读取本机 API token；不要把 `codexrunner` 加入该组。

   ```sh
   sudo groupadd --system xianyu-secret
   sudo useradd --system --home-dir /var/lib/xianyu --shell /usr/sbin/nologin xianyu
   sudo usermod -a -G xianyu-secret xianyu
   sudo usermod -a -G xianyu-secret astrbot
   sudo install -d -m 0755 -o root -g root /opt/xianyu-watch
   sudo install -d -m 0750 -o root -g xianyu-secret /etc/xianyu
   sudo install -d -m 0700 -o xianyu -g xianyu /var/lib/xianyu /var/lib/xianyu/data
   ```

2. 在 root 管理的 `/opt/astrbot/venv` 安装仓库固定版本 `requirements.txt`，其中包含 Playwright、aiohttp 和 Pillow。安装 Chromium 程序及系统依赖，可复用 [抖音说明](DOUYIN.md) 的只读 `/opt/astrbot-douyin-browser/chrome` **程序文件**。闲鱼拥有独立 profile，不复用其他服务的 Cookie。不同浏览器路径需同步修改单元中的 `XIANYU_BROWSER` 和 `ReadOnlyPaths`。不要把浏览器二进制或用户 profile 提交到仓库。

3. 安装服务源码并在服务器本地生成 token。以下操作拒绝覆盖现有 token，不打印其值：

   ```sh
   sudo install -m 0644 -o root -g root services/xianyu_server.py /opt/xianyu-watch/server.py
   sudo python3 - <<'PY'
   import grp, os, secrets
   path = '/etc/xianyu/api-token'
   descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
   try:
       os.fchmod(descriptor, 0o640)
       os.fchown(descriptor, 0, grp.getgrnam('xianyu-secret').gr_gid)
       os.write(descriptor, (secrets.token_urlsafe(36) + '\n').encode())
       os.fsync(descriptor)
   finally:
       os.close(descriptor)
   print('Created restricted local API token; value not printed')
   PY
   sudo install -m 0644 -o root -g root deploy/systemd/xianyu-mcp.service /etc/systemd/system/xianyu-mcp.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now xianyu-mcp.service
   ```

4. 将 `plugins/xianyu_watch/` 的源码复制到 `/var/lib/astrbot/data/plugins/xianyu_watch/`，把 `deploy/examples/xianyu_watch_config.json.example` 复制到私有配置目录并去掉 `.example`。配置为 `astrbot:astrbot 0600`，插件状态目录 `astrbot:astrbot 0700`。精确填写 `allowed_umos`、可选 `allowed_senders`、以及允许获取二维码的 `login_umos`；保持 AstrBot 主白名单。示例列表全空，因此未配置时全部拒绝。

5. 重启 `astrbot.service` 载入新增组和插件。确认端口 `18070` 仅监听 `127.0.0.1`，未携带 token 的 `GET /api/v1/login/status` 返回 401；不得给外网开放此端口。通过授权 QQ 会话完成扫码和后续真实查询验收。

`deploy/systemd/xianyu-mcp.service.d/workspace.conf` 仅用于已自行准备上述标准路径持久挂载的集中目录安装。现有 `migrate_workspace.py` 不自动迁移闲鱼的服务、profile 或 token；未准备挂载时不用安装该 drop-in。

## 共享权限与登录

`shared_login_members=true` 是公开模板的行为默认值，但仍要求精确匹配 `login_umos`。在已批准的群聊或私聊内，成员可说“闲鱼登录”“咸鱼登录”或 `/闲鱼 login` 获取二维码；群聊需 @ 或唤醒机器人。关闭共享成员模式时，登录还必须精确匹配 `login_senders`，空列表拒绝。允许查询的会话成员可以只读检查共享登录状态。

用闲鱼 App 扫码并确认，随后说“闲鱼登录状态”或 `/闲鱼 status`。最近有登录交互，或引用机器人二维码／状态回复时，“扫好了”“好了么”只查询当前会话状态。它们不会新建二维码、转成图片拆解任务，或借用其他群的登录交互上下文。

二维码本地等待上限 240 秒，闲鱼可能更早判定过期。服务区分待扫码、待 App 确认、正在完成授权、确认后授权未完成、状态暂时无法检查及已登录。扫码确认自身不算登录成功，只有平台身份接口返回真实身份才确认登录并保存恢复快照。登录态由闲鱼实时决定，没有保证的固定天数；服务重启从保留的 profile／私密快照恢复后仍要重新校验。

服务仅在当前确认流程内、短时间、当前登录页放行精确的官方令牌交换及成功响应返回的第一方 SSO 回调；普通查询不因此获得发布、下单、付款或聊天发送权限。浏览器网络经过公网 DNS 校验和固定地址连接，禁止私网、非标准端口、WebSocket 等额外通道。不要把完整服务 API 或 Cookie 注册成模型工具。

## 搜索与监控

`xianyu_search(query, limit)` 返回当前实际商品名称、标价和规范商品链接。搜索使用新发布排序第一页，最多查询平台返回的 30 条，不保证覆盖所有新上架商品或全站最低价。运费、定金、缺件及最终到手价需要核实；服务失败不能表述成没有商品。

明确要求定期监控时，Codex 根据购买目标构造 1 至 5 个具体词组；缺少目标时先读当前聊天或询问。`create_xianyu_watch` 默认每 30 分钟执行，最短 5 分钟。用户要求预算时必须实际传入 `max_price`，比较为严格小于上限，不含同价；未传入时不能在回复中承诺已启用过滤。价格按 Decimal 比较，只认一个明确的人民币标价，例如 `¥19.99`、`￥19.99`、`19.99元`。面议、范围、起拍、多价、定金、尾款、分期、运费及其他不确定总价不视为符合预算。

首次成功查询保存扫描到的全部商品 ID、名称、标价、规范链接和观察时间；扫描总数与符合预算的数量分别显示。后续仅提醒首次发现且符合条件的 ID。超出预算或价格不明的 ID 也记为已见，后续降价或补价不会变成“新商品”通知。提醒沿用原会话；创建人记录保留，所有已授权会话的当前操作人可共享列表、详情和管理。当前操作人须通过白名单，原监控的来源会话也必须仍在 `allowed_umos` 中；读取或管理不会重定向提醒。

`list_xianyu_watches` 返回价格条件、已见数、基线总数及已知符合数、待发送数、上次和下次运行时间、错误及每个关键词的查询覆盖。`read_xianyu_watch(watch_id, offset, limit)` 分页读取完整首次基线；每页最多 50 条，返回真实总数与 `has_more`。扫描总数不能表述为全部符合预算，也不表示全站商品总数。

`manage_xianyu_watch(action="update", ...)` 可更新 `max_price`、`interval_minutes` 和 `max_results`；显式 `max_price=null` 取消预算限制，省略则保留原值。更新不清空已见 ID、不重建首次基线、不重新发送旧商品。仅更新预算保持原下次执行时间；更新间隔才按上次执行和新间隔重新计算。待发送队列在条件变更和实际发送前都重新检查预算，不发送已不符合条件的旧队列项。

公开模板 `send_product_images=false`：即时查询与新商品提醒只返回名称、标价和链接，二维码仍以原生图片发送。若管理员明确启用商品图片，仅下载本次搜索登记的受限 CDN 图片，失败时仍保留真实商品文字；不会把其他图片当作原商品图。

监控保存在 `watches.json`，包括已见商品和待发送队列。发送失败保留同一个待发送记录，成功后才记为已提醒；重启继续处理。暂停／取消与正在发送的消息串行处理，返回成功后不再发后续通知。撤回会话权限后不会继续搜索或投递。存储损坏／无法写入时停止监控，不能覆盖原状态或声称已建立成功基线。

旧版记录只有已见 ID 和时间戳，无法还原首次扫描时真实名称和价格。读取时返回 `legacy=true`、详情不可用的数量及占位 ID，不编造明细。明确要求补回时调用 `read_xianyu_watch(..., refresh_metadata=true)`：按原关键词做一次受控当前查询，只为原 ID 中缺失的明细补数据；未找到的继续标注不可用，当前新 ID 不加入原基线。补回项标记 `source=recovered_current_query`、`observed_at` 和 `recovered_at`，因此不能当作首次快照。此操作不发送提醒、不改变已见 ID、待发送队列或下次检查时间，不增加新的后台任务。

## 维护与验收

`systemctl start|stop|restart xianyu-mcp.service` 只控制独立浏览器服务；查询、提醒和自然语言入口随 AstrBot 插件运行。升级时保留 `/var/lib/xianyu/data/profile`、`profile.login-state.json` 和插件 `watches.json`，保留 profile 目录 0700、快照和状态文件 0600。API token 保持 root:xianyu-secret 0640；不能放到 QQ、模型配置、命令参数或 Git。

离线回归：

```sh
python3 -m unittest discover -s plugins/xianyu_watch -p 'test_*.py' -v
python3 -m unittest discover -s services -p 'test_xianyu*.py' -v
python3 -m unittest discover -s tests -p 'test_portable_defaults.py' -v
```

真实 Chromium 回归需显式提供程序路径，页面完全由本地合成 fixture 拦截，不读取账号或访问闲鱼：

```sh
XIANYU_TEST_BROWSER=/path/to/chrome python3 -m unittest discover -s services -p 'test_xianyu*.py' -v
```

真实 AstrBot 组件回归使用固定版本 AstrBot venv，并使用合成会话、模拟 HTTP 和禁止 QQ 传输；不调用模型、不连接生产服务、不加载账号。临时状态全部在系统临时目录：

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/astrbot/venv/bin/python tests/runtime_xianyu.py -v
```

上线验收另在授权会话进行“二维码→App 确认→状态已登录→即时搜索→服务重启后状态→首次基线→新商品通知”闭环。不能用离线回归替代真实账号登录和商品可读性的结论；验收日志、二维码、Cookie 和商品页面响应只留在私有环境。

## 商品相关性与手机链接

指定品牌或型号时，创建、更新和即时查询可传入 `required_terms`，例如 `["infantino", "婴蒂诺"]`。它们是同一目标的已确认名称或别名，标题至少命中一项。英文按完整词边界匹配，并统一大小写与全角字符；不把平台推荐结果自动视为相关。无相关性证据的条目从默认基线展示和提醒中排除，旧 ID 仍保留。待发送队列在条件更新和发送前也会重新检查。

商品名称作为 Markdown 超链接，目标固定生成为 `https://h5.m.goofish.com/item?id=<数字ID>`，不使用搜索响应中的任意 URL 或第三方短链。即时查询、历史基线和新商品提醒均使用该入口。

2026-11-19 核对的官方移动入口：

- `https://h5.m.goofish.com/item?id=1` 返回移动商品页面；合成 ID 仅用来检查路由，不代表真实商品可购买。
- `https://h5.m.goofish.com/.well-known/apple-app-site-association` 声明了 `com.taobao.fleamarket` 的应用关联，路径覆盖 `*`。
- `https://h5.m.goofish.com/.well-known/assetlinks.json` 声明了 Android `com.taobao.idlefish` 的 `delegate_permission/common.handle_all_urls` 关联。

这使手机系统在支持并允许关联跳转时可以直接打开已安装的闲鱼 App；没有安装或浏览器未交给系统时，HTTPS 页面仍可作为回退。QQ 内置浏览器是否允许自动唤起，取决于客户端、系统和用户设置，不能保证每次直接跳转。如停留网页，可尝试页面的打开闲鱼入口，或从 QQ 菜单选择在系统浏览器打开。

保留官方应用关联、网页回退和简洁的商品名称链接，无需自建跳转页或直接发送自定义 scheme。真实手机 QQ 的 App 跳转需要客户端验收，服务端 HTTP 和消息 payload 测试不能替代这一结论。

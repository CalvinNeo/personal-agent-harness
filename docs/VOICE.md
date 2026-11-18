# QQ 语音输入

可选语音入口把 QQ 投递的语音转换为当前请求文字，先回显识别结果，再进入既有命令、提醒、记忆、Codex 或普通回答。识别在本机执行，使用 SenseVoice int8 和 Silero VAD；不把音频上传给外部转写服务。默认没有启用语音，也不会自动安装模型或启动识别进程。

群顶层语音在 QQ 实际投递后可以唤醒机器人；同条消息明确 @ 其他人的语音不会因此唤醒。群和私聊仍须通过 AstrBot 主白名单。语音转写不会增加发送者权限，识别后的命令会重新匹配命令过滤器和内容安全检查。直接引用中的语音也支持识别，并保留 Reply 标识；引用里的其他文字仍是上下文，不作为当前指令。

## 安装

使用固定 AstrBot `4.29.3` 环境，先按仓库说明安装累计框架补丁。补丁包含 QQ 音频附件识别、语音处理前的唤醒与访问检查，以及转写后的命令重匹配；仅复制 worker 不能接入消息管道。

```sh
python3 scripts/apply_patches.py --site-packages /opt/astrbot/venv/lib/python3.12/site-packages --check
python3 scripts/apply_patches.py --site-packages /opt/astrbot/venv/lib/python3.12/site-packages
python3 -m venv /opt/astrbot-voice/venv
/opt/astrbot-voice/venv/bin/pip install -r deploy/examples/voice-requirements.txt.example
install -m 0644 services/voice_worker.py /opt/astrbot-codex-runner/voice_worker.py
install -d -o astrbot -g astrbot -m 0700 /var/lib/astrbot/data/voice-input /var/lib/astrbot/data/voice-input/tmp
install -o astrbot -g astrbot -m 0600 deploy/examples/voice-config.json.example /var/lib/astrbot/data/voice-input/config.json
install -o root -g astrbot -m 0640 deploy/examples/voice.env.example /etc/astrbot/voice.env
```

从 [sherpa-onnx 上游模型发布](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models) 获取 SenseVoice 2024-07-17 int8 模型、tokens，以及 Silero VAD 模型，核对上游版本和校验值，按模型各自许可证使用。权重、下载缓存及音频样本不放入本仓库。默认模型目录须包含：

```text
/var/lib/astrbot/data/models/sensevoice-20240717-int8/
├── model.int8.onnx
├── tokens.txt
└── silero_vad.onnx
```

模型文件由 root 管理，并允许 `astrbot` 只读；目录中的私有数据不可因此开放给其他用户。worker 启动时只读取本地模型，没有联网下载兜底。

将 `deploy/systemd/voice-input.socket`、`voice-input@.service` 安装到 `/etc/systemd/system/`。需要自定义路径时，将 `voice-astrbot.conf.example` 安装为 `astrbot.service.d/voice.conf`，让 AstrBot 与 worker 读取同一环境文件。三个变量分别设置语音状态目录、Unix socket 和模型目录；改变 socket 路径还须同步修改 socket 单元的 `ListenStream`。

```sh
systemctl daemon-reload
systemctl enable --now voice-input.socket
```

最后在本机把 `voice-input/config.json` 中 `enabled` 改为 `true` 并重启 AstrBot 以加载框架补丁。主白名单仍使用已批准的完整 UMO；不要清空 AstrBot 主白名单，也不必向仓库模板填写真实身份。停用时把 `enabled` 改回 `false`，然后停止 socket；这不会删除其他聊天、记忆或提醒。

## 执行与限制

每次只接受一段语音，输入不超过 8 MiB、60 秒，转写最多 3000 字。支持 QQ SILK，以及可识别的 WAV、Ogg、FLAC、AMR、AAC、MP3 和容器音频。音频必须来自允许的 QQ 域名，DNS 结果须为公网地址；下载、跳转、格式和解码时长均有上限。QQ 适配器先保留远端音频引用，访问检查通过后才下载，避免在白名单检查之前做媒体处理。

worker 由权限为 `0600` 的 Unix socket 按需启动，每个连接使用独立进程，结束后退出。模板限制为单连接、无网络、600 MiB 内存、单核 CPU 配额和 65 秒最长运行时间。状态目录和临时音频由 `astrbot` 持有，音频处理结束即清理。SQLite 去重记录仅保存消息身份哈希和时间，不保存语音或转写正文。

没有检测到语音、解码失败、识别过长、并发忙、内容安全不通过或回显未取得真实发送回执时，会停止本条请求，不继续执行可能识别错误的命令。识别回显不占用后续业务回复的发送标记和回执绑定。回显及后续对话仍可能按 AstrBot 原有策略进入聊天历史；转写不是隐藏内容。

## 验证

无需模型、网络或 QQ 的边界测试：

```sh
python3 -m unittest discover -s tests -p 'test_voice_*.py' -v
ASTRBOT_WHEEL=/path/to/astrbot-4.29.3-py3-none-any.whl python3 -m unittest discover -s tests -p 'test_voice_patch.py' -v
```

在安装可选解码依赖的 Python 环境中运行 `test_voice_worker.py`，会额外验证真实 WAV 重采样、时长限制；本机存在 VAD 模型时还会验证静音不会生成命令。这些测试均使用合成数据，不调用真实 QQ、模型提供商或生产任务。

`tests/runtime_voice_input.py` 在真实 AstrBot 环境中使用合成事件和截获发送，检查转写、回显、引用、权限与 QQ 音频适配行为；识别器和下载传输由测试替换。测试与原始 wheel 的累计 patch 验证可以在独立临时环境运行，不要求覆盖正在运行的框架。

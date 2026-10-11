# astrbot_plugin_mc_monitor

面向 Docker 部署的 Minecraft Java 服务端持续监控插件。通过日志增量读取识别真实事件，使用 AstrBot 当前 LLM 与已有角色人格生成通知开场，再通过官方主动消息接口发送 QQ 私聊。默认关闭监控，安装本身不会连接或改动 MC、FRP、Docker 或其他服务。

当前版本：**1.0.0**。作者：Cheney。要求 Python ≥3.10、AstrBot ≥4.9.2 且 <5、OneBot v11 `aiocqhttp` 适配器。依赖仅 `aiohttp`；Windows 开发额外使用 `tzdata`。没有注册外部 LLM、聊天系统或工具。

[GitHub 仓库](https://github.com/Yyyyyylor/astrbot_plugin_mc_monitor) · [版本与安装包](https://github.com/Yyyyyylor/astrbot_plugin_mc_monitor/releases) · [问题反馈](https://github.com/Yyyyyylor/astrbot_plugin_mc_monitor/issues)

## 功能与数据来源

| 内容 | 来源和边界 |
| --- | --- |
| 玩家进入、退出、玩家名 | Vanilla/Paper 常见 `joined/left the game` 日志；忽略聊天正文 |
| UUID | 日志中的真实 UUID / GameProfile；缺少时显示不可用，不计算冒充真实 UUID |
| 在线人数、列表 | 完整启动日志之后的增量状态，或受限 RCON `list` 校准；首次从末尾监听时保持未知 |
| 白名单拒绝、验证失败、踢出、封禁 | 常见英文服务端消息；自定义语言/模组格式可能需补充规则；不读取或修改白名单文件 |
| 启动、停止、崩溃迹象 | ready / stopping / crash / watchdog 日志；停止日志表示开始关闭，不等于已退出 |
| 容器状态、异常退出、异常重启迹象 | 可选只读桥接器；检测容器 ID、StartedAt、状态变化及 OOM/退出码；缺少正常停止证据的重启明确标注“推断” |
| CPU、内存 | Docker stats 容器指标；100% CPU=占用一个逻辑核，允许超过100%；内存扣除 inactive_file，百分比相对于 cgroup 限额，无限额时通常为宿主可用上限 |
| TPS / MSPT | 仅确认 Paper 命令可用时发送固定 `tps` / `mspt`；分别读取1分钟TPS、5秒平均MSPT；原版、其他格式或无权限均显示不可用 |
| 无响应 | 可选 Java Server List Ping 状态接口；只证明状态接口响应，不能独自证明游戏主线程正常，也不代表 FRP 公网可达 |

支持重启续读、inode 轮转、截断和同 inode 重写检测、未完成行缓存、保留的 `.log.gz` 内容指纹匹配恢复。SQLite 将游标、解析状态和待发通知一起提交；重复轮询不重复产生事件。普通通知合并，冷却期间保留事实，高等级通知使用独立发送任务，绕过普通合并窗口。失败按接收者分别重试，不重复发送给已经成功的接收者。

## 安装

在 AstrBot 插件管理中使用“从链接安装”，填写仓库地址：

```text
https://github.com/Yyyyyylor/astrbot_plugin_mc_monitor
```

需要固定版本时，下载 [v1.0.0 安装包](https://github.com/Yyyyyylor/astrbot_plugin_mc_monitor/releases/download/v1.0.0/astrbot_plugin_mc_monitor-v1.0.0.zip)，通过“从文件安装”上传 ZIP。Release 同时提供 SHA-256 校验文件。也可以将完整插件目录复制到 AstrBot 的 `data/plugins/astrbot_plugin_mc_monitor/`。入口 `main.py`、`metadata.yaml`、`_conf_schema.json` 和 `mc_monitor/` 必须位于同一个插件根目录。

尚未提交 AstrBot 插件市场；GitHub 仓库与 Release 是本插件的发布渠道。桌面端 4.27.5 已验证本地 ZIP 安装，其他版本以实际安装入口为准。

AstrBot 通常会安装 `requirements.txt`。若当前部署禁用自动安装，在 **AstrBot 实际使用的 Python 环境、插件根目录** 执行：

```bash
python -m pip install -r requirements.txt
```

用途：安装插件运行依赖；不要在宿主的另一个 Python 中安装后假定容器可用。随后在插件管理中加载/重载本插件。

## 先跑通日志和私聊

1. 查明 **Ubuntu 宿主机上的 MC logs 目录**。将整个目录只读映射到 AstrBot 容器，例如 `/srv/minecraft/logs:/mc-logs:ro`，不要只挂载 `latest.log`，否则轮转后的新文件可能不可见。容器中的 AstrBot 用户需要目录搜索和文件读取权限。
2. 现有容器若没有此挂载，需要在维护窗口调整 AstrBot 部署；本插件不会自动重建容器或修改 compose。先保存现有配置，不改 MC、FRP 的端口/网络/挂载。可先在隔离 AstrBot 中验收。
3. 在原生插件配置面板设置 `logs.path=/mc-logs/latest.log`，`timezone` 与 MC 日志时区一致。保留首次读取位置 `end` 可避免安装后补发旧事件；需要读取保留历史时，首次安装选择 `beginning`。已有游标始终续读。
4. 给 AstrBot 管理员账号发 QQ 私聊：`/mc绑定`。此命令存储真实 `event.unified_msg_origin`，不猜测平台 ID 或 QQ 号映射。多个接收者可分别绑定，也可在面板中填完整 UMO 列表。
5. 开启 `enabled`。先关闭 `llm.enabled`，用 `/mc测试` 确认模板主动消息能送达；再开启 LLM，留空 Provider/人格以使用目标会话配置。
6. `/mc状态` 查看最近采样、数据来源及待发队列。真实玩家入服/退服后核对通知中的事实字段。

面板可用 `notifications.use_bound_recipients` 控制是否启用命令绑定。`notifications.send_test_once` 打开并保存可排队测试，下一次测试先关闭保存再打开；插件重载不会重复触发。

UMO 形式是 **`真实平台实例ID:FriendMessage:真实session_id`**。例如 `qq-main:FriendMessage:10001` 只展示格式，不能直接当作自己的目标。拒绝群聊 UMO，绑定命令限制管理员且只允许 `aiocqhttp` QQ 私聊。QQ 官方机器人适配器不在支持范围内，其主动发送能力与 OneBot 不同。

| 管理员命令 | 用途 |
| --- | --- |
| `/mc绑定` | 将当前 QQ 私聊登记为接收者，持久保存 |
| `/mc解绑` | 取消当前私聊的命令绑定；面板列表需另行移除 |
| `/mc测试` | 为所有当前接收者排队测试事件；LLM 开启时会真实调用配置的提供商 |
| `/mc状态` | 查看最近采样和队列；不触发额外网络/模型请求 |
| `/mc重载` | 重读并校验配置，错误时保留最后有效配置 |

## 可选指标与部署

详细步骤见 [部署说明](docs/DEPLOYMENT.md)，所有操作均需在自己的维护窗口执行。本次开发没有执行这些部署步骤。

- Docker 指标通过 `tools/docker_readonly_bridge.py`，只提供指定容器的 `GET /snapshot/{container}`；AstrBot **不挂载 Docker socket**。
- 原生 `docker.sock:ro` 不能把 Docker API 降为只读，因此不提供这种部署方式。
- Java 状态探测使用容器内网地址；不要经 FRP 探测后把代理中断判断为 MC 崩溃。
- RCON 默认关闭；仅在原部署已经安全启用或维护窗口配置后使用。密码通过 AstrBot 容器的环境变量传入，面板只填环境变量名。插件没有任意命令执行入口。

全部配置字段、默认值与约束见 [配置说明](docs/CONFIGURATION.md)。面板保存后插件每2秒检查配置对象/配置文件变化，部分 AstrBot 版本也会自动重载插件。无效值保留最后有效配置，结构化日志只指出字段路径。关闭监控暂停采集和发送，保留游标及待发队列；禁用/卸载插件会取消全部后台任务并关闭 HTTP/TCP 资源。

## 人格、事实与安全

使用官方 `Context.get_current_chat_provider_id(umo=...)`、`Context.llm_generate(...)`、人格/对话管理器，以及 `Context.send_message(umo, MessageChain)`。显式人格优先，其次当前对话人格，再按 UMO 读取默认人格。可只读引用最近0～6条文本聊天，默认0；不创建、更新、切换或追加正常聊天历史。

日志是非可信输入。解析层只保留匹配规则提取出的名字、UUID、时间、数值和固定原因代码；不把日志原文、IP、踢出理由原文送给模型。模型仅获得已识别的事件类别，按角色语气从安全开场句中选择一句，返回严格校验的JSON。**事实字段始终由程序原样附加**。不符合约束、超时、Provider/人格不可用均降级模板。此约束有意限制自由发挥，不能仅靠提示词保证模型永远不捏造事实。

QQ 主动发送是否最终送达取决于 OneBot 协议端、账号和平台限制。AstrBot `send_message=True` 表示找到平台并完成 API 调用，不代表 QQ 用户已读。插件不将测试替身返回成功当作真实送达证据。

## 持久化与恢复边界

数据在 AstrBot 的 `data/plugin_data/astrbot_plugin_mc_monitor/state.sqlite3`（含 WAL/SHM），不在插件源码内。升级时保留 AstrBot `data` 持久卷。迁移/备份时先停用本插件，完整复制数据目录；不要只在写入中复制主 SQLite 文件。

- 接收者按事件采集时登记；新增接收者不补发之前的事件。移除接收者或关闭某类通知后，待发队列中的对应记录会取消。全局关闭只暂停，重新开启继续。
- 冷却期间保留并延后同类同对象事件，按 `max_batch` 和消息长度分批发送。持续异常只在进入/恢复时通知，不因轮询重复发送。
- 达到队列上限时回滚本轮游标/状态，暂停推进并记录错误；恢复发送后继续。必须给日志保留足够时间，不能在无限断网下保证无限数据保留。
- 轮转扫描最多4096目录项，压缩匹配最多16份归档及默认64MiB解压预算；不可恢复、超过预算或缺失的日志会报告可能缺口，不伪造缺失事件。保留归档依修改时间顺序读取；手工改写归档时间可能破坏排序。
- 日期缺失的日志时间按文件日期和跨午夜规则推断，通知显式标注；跨多天离线、时钟回拨和保留不足可能使日期不准确。建议使用带日期的日志格式并保持主机时钟同步。
- UUID/在线列表缺少完整来源时不可用；采集失败、日志缺口或已知容器换代时会取消完整在线列表假设。RCON 可以重新校准在线名单。
- 发送采用至少一次语义：QQ 已发送而 SQLite 尚未记录成功时，如果进程恰好崩溃，恢复后可能重复该条。平台没有可用的幂等确认接口，无法承诺跨进程绝对“恰好一次”。
- 轮询之间瞬间发生且未留下日志/最终容器状态的变化无法保证捕获；Docker 初次采样建立基线，不将安装时已有状态当作新事件。被管理员 `kill` 与应用崩溃可能有相同退出码，通知保留证据及“迹象”表述。

## 开发与验证

在插件根目录，使用独立开发环境。Linux/macOS 将下面的 `.venv/Scripts/python.exe` 替换为 `.venv/bin/python`：

```powershell
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q --basetemp=.pytest_cache/tmp
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe tools/build_package.py
```

依次用于创建隔离环境、安装开发依赖、运行隔离测试、静态检查、格式检查、生成可安装ZIP。测试只使用临时文件、SQLite、本地协议服务器及 AstrBot API 测试替身，无真实 QQ 消息或付费 LLM 请求。

已验证与未验证范围见 [验证记录](docs/VALIDATION.md)，模块和官方API依据见 [架构说明](docs/ARCHITECTURE.md)。已在 AstrBot 桌面端 4.27.5 实际安装，验证原生配置保存、模拟日志采集、去重、轮转、重载续读、来源中断恢复、配置回退和停用。尚未在用户 Ubuntu 24.04、真实 MC/Paper、FRP、AstrBot Docker、QQ 与配置的 LLM 上联调；不能把隔离测试和短时桌面验证等同于生产验证。

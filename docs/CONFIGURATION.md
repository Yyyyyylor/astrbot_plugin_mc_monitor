# 配置说明

所有字段均由 `_conf_schema.json` 提供原生界面。以下为实际Schema生成的字段表。

保存后的配置会被插件校验；无效值保留最后有效配置，仅日志字段路径，不回显输入。`minimum/maximum` 为插件校验约束。

## 全局设置

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `enabled` | 启用持续监控 | `false` | 完成日志路径与私聊目标配置后开启；关闭时保留游标与待发队列。 |
| `timezone` | MC 日志时区 | `"Asia/Shanghai"` | 必须与 MC 日志实际时区一致；日志未带日期时按文件时间与日期切换推断。 |
| `poll_seconds` | 监控轮询间隔（秒） | `5.0` |  范围 1～3600。 |
| `io_timeout` | 网络超时（秒） | `5.0` |  范围 0.1～60。 |
| `retention_days` | 已处理事件去重保留天数 | `14` |  范围 1～365。 |

## 日志增量读取（`logs`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `logs.enabled` | 启用日志 | `true` |  |
| `logs.path` | AstrBot 容器内日志路径 | `"/mc-logs/latest.log"` | 挂载整个 logs 目录为只读，不能只挂 latest.log 单文件。 |
| `logs.initial_position` | 首次安装读取位置 | `"end"` | 已有游标始终续读；从末尾读取会使在线人数未知，直到启动日志或 RCON 校准。 可选：end, beginning。 |
| `logs.rotation_glob` | 轮转文件匹配 | `"*.log*"` | 只查 logs 目录，支持 .log 和 .log.gz；压缩归档通过内容指纹匹配续读。 |
| `logs.max_bytes` | 每轮最大读取字节 | `262144` |  范围 4096～4194304。 |
| `logs.max_line_bytes` | 单行最大字节 | `16384` |  范围 1024～131072。 |
| `logs.recovery_max_bytes` | 压缩归档恢复解压预算（字节） | `67108864` | 每次匹配最多检查16份归档；超过预算或归档缺失时报告可能的日志缺口。 范围 1048576～268435456。 |

## Docker 只读桥接（`docker`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `docker.enabled` | 启用容器状态和资源指标 | `false` |  |
| `docker.container` | MC 容器名 | `"minecraft"` |  |
| `docker.bridge_url` | 只读桥接服务地址 | `"http://172.17.0.1:8765"` | 仅访问配套桥接器 /snapshot；不要指向裸 Docker API。 |
| `docker.secret_env` | 桥接 Token 所在环境变量名 | `"MC_MONITOR_BRIDGE_TOKEN"` | 填写变量名，不填写 Token。桥接器和 AstrBot 使用同一密钥。 |

## Minecraft Java 状态探测（`status`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `status.enabled` | 启用无响应检测 | `false` |  |
| `status.host` | MC 内网主机名 | `"minecraft"` | 使用内网直连地址，避免把 FRP 断线误判为 MC 故障。 |
| `status.port` | Java 服务端端口 | `25565` |  范围 1～65535。 |

## 可选 RCON 指标（`rcon`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `rcon.enabled` | 启用 RCON | `false` |  |
| `rcon.host` | RCON 内网主机名 | `"minecraft"` |  |
| `rcon.port` | RCON 端口 | `25575` |  范围 1～65535。 |
| `rcon.secret_env` | RCON 密码所在环境变量名 | `"MC_MONITOR_RCON_PASSWORD"` | RCON 本身有管理权限，应只在可信内网开放；插件仅发送固定只读命令。 |
| `rcon.performance_source` | TPS/MSPT 命令能力 | `"unavailable"` | 仅确认 Paper 支持 tps / mspt 时选择 paper；原版默认不可用。 可选：unavailable, paper。 |

## QQ 私聊通知（`notifications`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `notifications.recipients` | 目标 UMO 列表 | `[]` | 每项为真实 platform_id:FriendMessage:session_id；管理员私聊发送 /mc绑定 可自动登记。 |
| `notifications.merge_seconds` | 普通事件合并窗口（秒） | `10.0` |  范围 0～3600。 |
| `notifications.max_batch` | 每条最多事件数 | `8` |  范围 1～15。 |
| `notifications.queue_limit` | 待发记录上限（按接收者计数） | `10000` | 达到上限暂停日志推进并记录错误，不静默丢弃待发事件。 范围 100～100000。 |
| `notifications.send_timeout` | 主动消息发送超时（秒） | `15.0` |  范围 1～120。 |
| `notifications.retry_base` | 失败重试起始间隔（秒） | `5.0` |  范围 1～600。 |
| `notifications.retry_max` | 失败重试最长间隔（秒） | `300.0` |  范围 5～86400。 |
| `notifications.max_attempts` | 快速重试次数 | `5` | 超出后进入长期重试间隔；队列保留，重启继续。 范围 1～20。 |
| `notifications.quiet_enabled` | 启用安静时段 | `false` |  |
| `notifications.quiet_start` | 安静开始 HH:MM | `"23:00"` |  |
| `notifications.quiet_end` | 安静结束 HH:MM | `"07:00"` | 相同时间表示全天安静。普通事件延后，高等级可按下方开关立即发送。 |
| `notifications.high_bypass_quiet` | 高等级事件绕过安静时段 | `true` |  |
| `notifications.recovery` | 发送恢复通知 | `true` |  |

| `notifications.use_bound_recipients` | 启用命令绑定的私聊接收者 | `true` | 开启时与面板UMO列表取并集，关闭仅用面板列表。管理员私聊命令管理绑定。 |
| `notifications.send_test_once` | 发送一次测试消息（打开并保存） | `false` | 已启用且有接收者时排队发送；再次测试先关闭保存再打开。持久化边沿防止重载重复。 |

## 人格化通知（`llm`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `llm.enabled` | 启用当前 AstrBot LLM | `true` |  |
| `llm.provider_id` | Provider（留空随目标会话） | `""` |  |
| `llm.persona_id` | 人格（留空随会话） | `""` |  |
| `llm.prompt` | 通知风格提示词 | `"请以已有角色的语气，简短自然地提醒我关注服务器事件。"` |  |
| `llm.context_messages` | 只读引用最近聊天消息数 | `0` | 默认关闭；最多 6 条、总计 4000 字符。不写回聊天历史，不包含工具或系统消息。 范围 0～6。 |
| `llm.timeout` | LLM 总超时（秒） | `15.0` |  范围 0.1～120。 |

## 阈值及恢复（`alerts`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `alerts.consecutive` | 连续异常采样次数 | `3` |  范围 1～60。 |
| `alerts.recover_samples` | 连续恢复采样次数 | `2` |  范围 1～60。 |
| `alerts.hysteresis_percent` | 恢复迟滞百分比 | `5.0` |  范围 0～40。 |
| `alerts.low_tps` | TPS 低于此值告警（0 禁用） | `18.0` |  范围 0～20。 |
| `alerts.high_mspt` | MSPT 高于此值告警（0 禁用） | `50.0` |  范围 0～10000。 |
| `alerts.high_cpu` | 容器 CPU% 告警（100%=1核，0 禁用） | `400.0` |  范围 0～100000。 |
| `alerts.high_memory` | 容器内存% 告警（0 禁用） | `85.0` |  范围 0～100。 |

## 各事件独立策略（`events`）

| 字段 | 说明 | 默认值 | 范围/说明 |
| --- | --- | --- | --- |
| `events.join.enabled` | 通知开关 | `true` |  |
| `events.join.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.join.cooldown` | 同类同对象通知冷却（秒） | `0.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.quit.enabled` | 通知开关 | `true` |  |
| `events.quit.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.quit.cooldown` | 同类同对象通知冷却（秒） | `0.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.whitelist.enabled` | 通知开关 | `true` |  |
| `events.whitelist.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.whitelist.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.auth.enabled` | 通知开关 | `true` |  |
| `events.auth.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.auth.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.kick.enabled` | 通知开关 | `true` |  |
| `events.kick.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.kick.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.ban.enabled` | 通知开关 | `true` |  |
| `events.ban.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.ban.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.server_start.enabled` | 通知开关 | `true` |  |
| `events.server_start.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.server_start.cooldown` | 同类同对象通知冷却（秒） | `0.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.server_stop.enabled` | 通知开关 | `true` |  |
| `events.server_stop.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.server_stop.cooldown` | 同类同对象通知冷却（秒） | `0.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.crash.enabled` | 通知开关 | `true` |  |
| `events.crash.level` | 通知等级 | `"high"` |  可选：normal, high。 |
| `events.crash.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.restart.enabled` | 通知开关 | `true` |  |
| `events.restart.level` | 通知等级 | `"high"` |  可选：normal, high。 |
| `events.restart.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.container.enabled` | 通知开关 | `true` |  |
| `events.container.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.container.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.alert.enabled` | 通知开关 | `true` |  |
| `events.alert.level` | 通知等级 | `"high"` |  可选：normal, high。 |
| `events.alert.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.recovery.enabled` | 通知开关 | `true` |  |
| `events.recovery.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.recovery.cooldown` | 同类同对象通知冷却（秒） | `0.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.monitor_error.enabled` | 通知开关 | `true` |  |
| `events.monitor_error.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.monitor_error.cooldown` | 同类同对象通知冷却（秒） | `60.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |
| `events.test.enabled` | 通知开关 | `true` |  |
| `events.test.level` | 通知等级 | `"normal"` |  可选：normal, high。 |
| `events.test.cooldown` | 同类同对象通知冷却（秒） | `0.0` | 冷却期内记录保留并合并，冷却结束后发送。 范围 0～86400。 |

## 生效与来源

- 默认未启用。安装后先确认日志和私聊目标，再开启监控。
- 面板Provider与人格使用AstrBot官方选择器；留空分别随目标会话Provider和人格。
- `secret_env` 填环境变量名称，实际密钥/密码由部署环境注入；配置不会打印密钥。
- 冷却按接收者、事件类型、玩家或指标对象分别计算，期间事件保留并延后合并。
- 阈值0表示禁用对应指标告警；不可用数据既不触发阈值告警，也不触发虚假恢复。
- 连续异常/恢复次数按实际采样计数，轮询间隔之外还受网络超时和日志补读耗时影响。
- 人格生成受安全开场句约束；风格提示词无法授权输出日志中的指令或修改事实。
- 修改环境变量、容器挂载和网络通常需要对应容器重建；不属于插件配置热更新。
- `logs.initial_position` 仅无持久游标时生效，不会重置已保存游标。
- 修改日志路径建立该路径的新游标并取消原在线列表完整性假设；切回原路径可续读。
- 命令绑定接收者单独持久化，与面板列表取并集。解绑命令不修改面板列表。

当前共 97 个叶子配置字段。

# 部署说明

本文件是供操作者检查后执行的示例，不会被插件自动执行。现有服务不因安装源码而改变。首先核实 MC 镜像/版本、容器名、日志挂载、AstrBot 实际 Python 和数据目录，以及 OneBot 适配器状态。不得直接覆盖既有 compose。

## 仅日志模式

最少权限方案是把已有 MC 日志目录只读提供给 AstrBot。在现有 AstrBot 服务的 `volumes` 中增补一条（路径需按实际替换）：

```yaml
volumes:
  - /srv/minecraft/logs:/mc-logs:ro
```

只挂 `latest.log` 会在 rename 轮转后指向旧 inode；应挂整个目录。不建议把全部 MC 数据目录、服务端配置或宿主根目录挂入 AstrBot。不要为读日志递归重置现有服务目录权限。

修改容器挂载通常需要重建 AstrBot 容器，会带来短暂停机。请在维护窗口先保存当前 compose/部署配置，确认已有 `data` 持久卷和网络保留，再操作 **AstrBot 对应服务**。本次隔离开发未执行重建、重启或任何部署命令。

## Docker 只读桥接器

桥接器运行在 Ubuntu 宿主机的独立 Python 环境；AstrBot 不获得 socket 或 docker 组权限。它固定容器名，校验 Bearer Token，只允许一条 HTTP GET 路由。上游仅请求 `/v1.41/containers/{固定名}/json` 和 `stats?stream=false`；不提供 exec、logs、inspect全量输出、列举其他容器、POST、启动、停止或删除能力。

宿主 Docker socket 访问本质上具有高权限。持有 socket 的桥接进程属于可信边界，其专用宿主账号的 docker 组权限也接近 root；`ReadOnlyPaths` 和 socket 文件只读挂载不能限制 Docker HTTP 动词。此实现通过固定路由和固定上游 GET 约束远端权限，不宣称消除了宿主进程被攻破的风险。

示例准备流程：**Ubuntu 24.04 宿主机，维护窗口，项目源码目录**。下面只创建新的桥接环境，不停止现有服务；`/opt` 和 `/etc` 写入需管理员权限。

```bash
sudo install -d /opt/mc-monitor-bridge
sudo cp tools/docker_readonly_bridge.py /opt/mc-monitor-bridge/
sudo python3 -m venv /opt/mc-monitor-bridge/.venv
sudo /opt/mc-monitor-bridge/.venv/bin/python -m pip install 'aiohttp>=3.10,<4'
```

用途依次为：建立独立目录、复制桥接器、创建隔离环境、安装HTTP依赖。Ubuntu 若未安装 `python3-venv`，先由系统管理员安装系统包。

为新桥接器生成独立密钥，文件不能与插件源码或发布ZIP混放。以下命令只写新文件，若已有密钥则保留，且不打印密钥：

```bash
sudo /opt/mc-monitor-bridge/.venv/bin/python - <<'PY'
import os
import secrets
path = '/etc/mc-monitor-bridge.token'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as file:
    file.write(secrets.token_urlsafe(48) + '\n')
PY
```

先在本机 loopback 启动检查（按实际替换容器名）：

```bash
sudo /opt/mc-monitor-bridge/.venv/bin/python /opt/mc-monitor-bridge/docker_readonly_bridge.py --container minecraft --listen 127.0.0.1 --token-file /etc/mc-monitor-bridge.token
```

此命令前台运行桥接器，`Ctrl+C` 退出；不会启动或重启 MC。AstrBot 容器不能通过自己的 `127.0.0.1` 访问宿主服务。对接时选择**实际 Docker bridge 的宿主私网地址**或已有受控私网入口；`172.17.0.1` 只是常见默认，必须核对实际网络。不要绑定公网地址、`0.0.0.0`，不要通过 FRP 发布此接口。跨不可信网络必须使用受控的 HTTPS 反向代理，插件保持证书校验开启。

生产使用可按 `tools/mc-monitor-bridge.service.example` 配置一个新 systemd 服务。例子中的账号、容器名、监听地址和路径都必须先核实；创建专用账号并仅向该宿主桥接进程授予 socket 访问权，Token 文件所有者设为该账号且权限600。示例文件不自动安装，也不自动变更现有系统服务。

通过已有的秘密管理/受保护 env-file，把同一 Token 注入 AstrBot 容器的 `MC_MONITOR_BRIDGE_TOKEN`，不要把值放在命令参数、README、仓库或聊天中。面板配置：

```json
{
  "docker": {
    "enabled": true,
    "container": "minecraft",
    "bridge_url": "http://实际宿主内网地址:8765",
    "secret_env": "MC_MONITOR_BRIDGE_TOKEN"
  }
}
```

Token 改变需要同步两端。环境变量改变通常需要重新创建对应 AstrBot 容器，插件配置热更新无法改变已启动进程的环境。

## Java 状态与 RCON

Java 状态探测无需改 MC 权限，直接使用 AstrBot 可达的 MC 内网主机名和游戏端口。若容器分属不同网络，先由操作者评估现有网络；插件不自动改网络、FRP 或防火墙。

RCON 只在需要在线名单校准或 Paper TPS/MSPT 时启用。若 MC 原先未启用，修改 `server.properties` 通常需要重启 MC，应延后到维护窗口。RCON 密码在协议中明文传输，必须限制在可信内网，不能通过 FRP 暴露管理端口。

在 AstrBot 容器注入 `MC_MONITOR_RCON_PASSWORD`，面板设置对应 `host`、`port` 和 `secret_env`；只有确认服务端支持时将 `performance_source` 改成 `paper`。插件仅发送固定 `list`、`tps`、`mspt`，以及用于结束分包响应的空命令；不接受面板、日志或 LLM 提供的任意命令。

RCON 本身的密码往往具有管理权限，插件的命令白名单不能改变服务端授权模型。若不接受这项凭据权限，保持 RCON 关闭，TPS/MSPT 显示不可用。不要为监控给 AstrBot 新增不受限的 Docker 权限。

## 实机验收与退出

按顺序核对：模板 `/mc测试`、人格化 `/mc测试`、玩家入退、合法与拒绝登录、MC日志轮转、插件重载续读、已有Docker状态变化、停用插件后无后台发送。崩溃/OOM/重启演练在隔离 MC 容器进行，不以生产故障作为验收手段。

停用/卸载插件会取消采集、紧急通知、普通通知、配置监听四个任务并关闭连接；数据目录保留。独立桥接服务由宿主操作者单独停止。需要恢复旧部署时撤回此次新增的配置片段，保留原有挂载、网络、MC和FRP设置。

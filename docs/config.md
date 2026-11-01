# 配置参考

配置文件是 `services.json`，与 `panel.py` 同目录。改完保存即热重载（约 2 秒内生效），
不需要重启面板。也可以直接在面板「设置」里改，改坏了会拦下来并逐条报错。

结构：

```json
{
  "panel":   { ... },   // 面板外观与监听
  "services": [ ... ],  // 受管服务
  "mappings": [ ... ],  // 映射链路
  "deploy":  { ... }    // 一键部署的路径与硬件门槛
}
```

---

## panel

| 字段 | 默认 | 说明 |
|---|---|---|
| `title` | `服务中心` | 页面标题 |
| `subtitle` | — | 标题下的一行说明 |
| `bind` | `127.0.0.1` | 监听地址。**不要随便改成 `0.0.0.0`**，见 README 的限制 |
| `port` | `8899` | 监听端口。改了要重启面板才生效（页面上有「重启面板」按钮） |
| `refresh_sec` | `5` | 后台探测与前端刷新的间隔，秒 |

---

## services[]

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✓ | 唯一标识，只能用英文字母、数字、下划线、短横线 |
| `name` | ✓ | 显示名 |
| `group` | | 分组，纯展示，相同 group 的卡片排在一起 |
| `desc` | | 一句话说明 |
| `kind` | ✓ | `task` / `process` / `external` |
| `task` | | 计划任务名，`kind=task` 时必填 |
| `cmd` | | 启动命令，`kind=process` 且没有 `task` 时用来拉起 |
| `cwd` | | 工作目录 |
| `match` | | 进程命令行关键字，字符串数组。用于查 PID、杀进程 |
| `port` | | 端口，纯展示 |
| `probe` | | 探测配置，见下 |
| `logs` | | 日志文件路径数组，卡片上的「日志」读第一个 |
| `actions` | | 允许的操作，勾了才有按钮：`start` / `stop` / `restart` |
| `slow_start_sec` | | 冷启动宽限期。这期间探测失败不算故障（比如模型加载要 5 分钟） |

### kind

- **`task`** — 由 Windows 计划任务托管。启停走 `Start-ScheduledTask` / `Stop-ScheduledTask`
- **`process`** — 裸进程。启动用 `cmd`，停止按 `match` 匹配命令行后杀进程
- **`external`** — 别的机器上的服务，只探测、不启停

---

## probe

| type | 字段 | 说明 |
|---|---|---|
| `http` | `url` `timeout` `expect` `degraded_on` `degraded_hint` `headers` `api_key_header` | 最常用。`expect` 是算成功的状态码（默认 `[200]`）；`degraded_on:[503]` 表示这些码算"加载中"而不是故障；`degraded_hint` 是加载中时显示的文案；`api_key_header` 配 `api_key_file` 时会自动带上该请求头 |
| `tcp` | `host` 或 `resolve` / `port` / `timeout` | 只测端口通不通 |
| `dns` | `domain` / `record_type` | 解析 A / AAAA。注意记录类型字段是 `record_type`，`type` 已被探测类型占用 |
| `task` | 无 | 只查计划任务状态与上次运行结果 |
| `none` | 无 | 仅登记，不探测 |

两个占位符：

- `host: "@self"` / URL 里写 `@self` — 替换成本机主局域网 IPv4（自动排除 `169.254.*` 和 Tailscale 网卡）
- `resolve: "域名"` — 先解析成 IPv6 再连端口。目标只监听 IPv6 时必须用这个

---

## mappings[]

一条链路。整条链路的状态取各段里最差的那个。

| 字段 | 说明 |
|---|---|
| `id` `name` `group` `desc` | 同 services |
| `segments` | 数组，每段一个 `{ label, hint, probe }`。`hint` 只用于展示 |
| `ddns` | `{ record, type, owner }`。`type` 是 `AAAA` 或 `A` |
| `access` | 「打开」按钮跳的地址 |

DDNS 检查会做一件额外的事：把解析出的地址与本机公网前缀比对。
同段 → 记录是新鲜的；不同段 → DDNS 可能没更新（比如运营商换了 IPv6 前缀）。
这只在 `AAAA` 并且本机有公网 IPv6 时有意义，否则会显示"无法比对"。

---

## deploy

| 字段 | 默认 | 说明 |
|---|---|---|
| `base_dir` | `../breeze-tts` | 目标项目根目录 |
| `repo_url` | 空 | 代码缺失时在部署日志里提示的仓库地址 |
| `project_dir` | `base_dir/breeze-tts-main` | 推理代码所在目录 |
| `venv_dir` | `base_dir/venv` | 虚拟环境 |
| `model_dir` | `base_dir/model` | 模型权重 |
| `wheel_dir` | `base_dir/wheels` | 离线 torch 轮子目录（有就优先用它） |
| `gateway_port` | `7861` | 服务端口 |
| `task_name` | `BreezeTTSGateway` | 注册的计划任务名 |
| `base_python` | `python` | 建 venv 用的基础解释器 |
| `torch_version` | `2.9.1` | 期望的 torch 版本 |
| `pip_index` | `https://pypi.org/simple` | pip 源，国内可换镜像 |
| `torch_index` | `https://download.pytorch.org/whl/cu128` | torch 轮子源，cu 版本要跟驱动对得上 |
| `requirements` | 见下 | 硬件门槛 |

`requirements`：

| 键 | 含义 |
|---|---|
| `vram_min_gb` / `vram_rec_gb` / `vram_fast_gb` | 显存 最低 / 推荐 / 能开 fast path |
| `cc_min` | 最低计算能力（Turing 是 7.5） |
| `driver_min` | 最低驱动版本 |
| `ram_min_gb` / `ram_rec_gb` | 内存 |
| `disk_min_gb` | 目标盘剩余空间 |

判定时所有显存档位比较都留 0.5 GB 容差——标称 12 GB 的卡实际报 11.94 GiB。

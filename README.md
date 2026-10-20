# codex工作灯

原名“aaa无敌”。Windows 上的 Codex 技能，通过 BLE 控制已有 ESP32 状态灯。明确启用后只跟踪绑定的根聊天，灯保持 SOLID 常亮、75% 亮度。

技能调用仍为 `$aaa-wudi`，安装目录和内部协议沿用 `aaa-wudi`，兼容现有安装。

| 状态 | 灯色 |
|---|---|
| 工作 / 恢复 | 蓝 `#0000FF` |
| 当前请求完成 | 绿 `#00FF00` |
| 等待用户或方案待实施 | 红 `#FF0000` |
| 当前无法继续 | 黄 `#FFFF00` |
| 未启用 / 取消 | 熄灭 |

默认不启用；其他聊天的普通事件不影响灯色。同一聊天重新启用会刷新绑定，其他聊天明确启用会接管。后台重启后必须再次明确启用。本仓库提供控制软件与设备协议，**不提供 ESP32 固件**。

## 安装与升级

需要 Windows、Python 3.12 或更新版本、可用的蓝牙适配器，以及实现下述 GATT 协议的设备。下载仓库后在 PowerShell 中执行（示例路径和地址需替换）：

```powershell
.\install.ps1 -Python 'D:\Python312\python.exe' -DeviceAddress 'AA:BB:CC:DD:EE:FF'
```

安装器在 `%CODEX_HOME%\skills\aaa-wudi` 安装技能；未设置 `CODEX_HOME` 时使用 `%USERPROFILE%\.codex`。本机配置、专用虚拟环境和日志默认位于 `%LOCALAPPDATA%\aaa-wudi`。可用 `-CodexHome`、`-DataDir` 指定其他绝对路径，中文和空格路径均受支持。数据目录必须位于技能目录之外。

- 首次必须指定 Python 和设备地址，不扫描附近设备。地址格式为 `AA:BB:CC:DD:EE:FF`。
- 已安装时优先复用 `runtime.json` 记录的 Python、设备地址和数据目录，显式参数覆盖记录。旧配置缺少地址时需补传 `-DeviceAddress`。
- 基础 Python 会在数据目录创建专用 venv；传入已有 venv 则复用。依赖固定为 `bleak==3.0.2`，安装只改该虚拟环境。
- 更新正在运行的安装前，先通过旧版本入口执行 `shutdown`，确认设备熄灭。安装器不终止旧进程，不启动服务、不连接设备、不启用灯。
- 原安装的发布文件、`runtime.json` 和已有 `hooks.json` 备份到数据目录的 `backups\skill-时间-随机值`。安装结束输出 JSON，包含 `backup_dir`、`skill_dir` 和 Hook 结果。
- Hooks 注册幂等保留其他定义，不修改 `config.toml`。只有新增或变更的定义需要在 Codex 中正常审核；安装器不会自动信任，见 [官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)。

再次执行 `install.ps1` 即升级；已有配置 JSON 损坏时会明确停止，保留原文件，先修复再安装。`-SkipDependencies` 仅供隔离测试或已自行准备依赖的离线环境，不验证或安装 BLE 依赖；普通安装不要传此参数。`runtime.example.json` 展示本机配置格式，实际 `runtime.json` 被 Git 忽略。

## 使用与诊断

在 Codex 中明确调用 `$aaa-wudi` 或选择 **codex工作灯**（原“aaa无敌”）启用。PowerShell 入口位于安装目录，读取本机配置中记录的 Python：

```powershell
$skillDir = Join-Path $env:USERPROFILE '.codex\skills\aaa-wudi'
# 自定义 CODEX_HOME 时，把 skillDir 改为该目录下的 skills\aaa-wudi。
& (Join-Path $skillDir 'scripts\light.ps1') activate
& (Join-Path $skillDir 'scripts\light.ps1') status
& (Join-Path $skillDir 'scripts\light.ps1') status --verbose
& (Join-Path $skillDir 'scripts\light.ps1') doctor
& (Join-Path $skillDir 'scripts\light.ps1') deactivate
& (Join-Path $skillDir 'scripts\light.ps1') shutdown
```

`activate` 只在有可信当前轮次时亮蓝灯；`pending_next_prompt` 表示绑定成功但自动跟踪尚未就绪，保持熄灯。Hooks 后续自动处理状态，根代理使用本轮提供的轮次和 nonce 输出最终标记，具体规则见 [SKILL.md](SKILL.md)。`deactivate` 清除绑定并熄灯，`shutdown` 还会结束后台服务。

`binding_accepted` 仅表示绑定成功，`tracking_ready` 表示已取得可信轮次；`confirmed` 必须经过当前蓝牙连接的写入和 STATE 回读。断线即取消旧确认，重连后重新应用目标。`observed` 是最近一次回读，不是持续采样。`doctor` 提供运行环境、Hook 路径和诊断；日志位于数据目录。故障不阻塞原任务。

进程被强制终止或电脑突然退出时，现有固件可能保留最后灯色。恢复后明确关闭或启用；灯色本身不能证明任务仍在运行。

## 设备 BLE UART 协议

设备地址由安装参数写入本机配置。下列 UUID 为协议常量：

| 属性 | UUID | 用途 |
|---|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | 主服务 |
| RX | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | 写入命令，write with response |
| TX | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | notify 应答 |
| STATE | `6e400004-b5a3-f393-e0a9-e50e24dcca9e` | 读取 UTF-8 JSON |

RX 接受 ASCII 命令：`MODE SOLID`、`COLOR #RRGGBB`、`BRIGHT 75`、`ON`、`OFF`。控制器依次发送，写入的命令没有末尾换行。每条命令需由 TX 返回完整的 `OK <原命令>`；例如 `OK BRIGHT 75`。以 `ERR` 开头的应答视为协议错误，其他通知不作为成功确认。TX 支持换行分隔多条应答。

开启时命令顺序为设置模式、设置颜色、设置亮度、开启；关闭只发送 `OFF`。随后读取 STATE，例如：

```json
{"power":true,"color":"#0000FF","brightness":75,"mode":"SOLID","output":"#0000BF"}
```

`power` 为布尔值，`color`/`output` 为 `#RRGGBB` 字符串，`brightness` 为 75，`mode` 为 `SOLID`。开启必须回读目标颜色、模式、亮度且 `output` 非零；关闭必须回读 `power:false`、`output:"#000000"`。只有命令应答与回读均通过才确认目标已应用。

## 开发与测试

从仓库根目录运行，不需要本机 `runtime.json`、真实设备或历史聊天：

```powershell
& 'D:\Python312\python.exe' -X utf8 -B -m unittest discover -s tests -p 'test_*.py' -v
```

自动测试覆盖状态机、轮次缓存、最终标记、假 BLE 回读与重连、IPC、临时目录安装及真实 PowerShell / cmd 启动。安装测试使用临时源目录、临时 CodexHome 和无网络依赖安装；不会启用灯或写入用户配置。GitHub Actions 使用 [checkout](https://github.com/actions/checkout) 与 [setup-python](https://github.com/actions/setup-python)，在 Windows 的 Python 3.12、3.13 上运行相同测试。首次推送后查看 Actions 结果，本地通过不能代替远程 CI。

`tests/hardware_check.py` 与 `tests/live_hook_check.py` 为需主动运行的硬件/桌面验收，不属于 unittest 自动发现。前者会操作灯和专用测试聊天并以关闭结束，后者观察真实当前聊天事件；仅在已配置设备并明确需要硬件验收时运行。模拟测试通过不代表真实设备或桌面事件验收通过。

## 恢复与发布

恢复前通过当前版本 `shutdown` 结束服务。把安装输出的 `backup_dir` 内 `SKILL.md`、`agents`、`scripts`、README 等发布文件及 `runtime.json` 复制回 `skill_dir`；如需恢复 Hooks，核对备份 `hooks.json` 后仅恢复本技能定义，保留备份后新增的其他 Hooks。内部 IPC 协议不做双版本兼容，恢复后也必须重新明确启用。

上传前确认 `git status` 中没有真实 `runtime.json`、设备地址、密钥、聊天标识、日志、缓存、备份或虚拟环境；运行全部测试并检查差异。仓库不自动上传 GitHub。本次修复与代码统计见 [CHANGES.md](CHANGES.md)。MIT 许可证见 [LICENSE](LICENSE)。

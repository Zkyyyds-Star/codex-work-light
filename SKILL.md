---
name: aaa-wudi
description: "明确调用 codex工作灯（原 aaa无敌）时，将 ESP32 状态灯绑定当前聊天，以四色显示工作、完成、等待用户和阻塞；也可查询、诊断或停用。"
---

# codex工作灯

原名“aaa无敌”，调用标识仍为 `$aaa-wudi`。

默认熄灯。只有用户明确调用 `$aaa-wudi` 或选择 **codex工作灯**（原 **aaa无敌**）要求启用时才执行 `activate` 并绑定当前聊天；同聊天再次启用刷新绑定，其他聊天明确启用则接管。查询、诊断或关闭请求不要先启用。

## 命令

将 `<技能目录>` 替换为本 `SKILL.md` 所在目录。入口读取安装时记录的 Python，不另找系统 Python；安装与设备协议见 [README](README.md)。

```powershell
& (Join-Path '<技能目录>' 'scripts/light.ps1') activate
& (Join-Path '<技能目录>' 'scripts/light.ps1') status
& (Join-Path '<技能目录>' 'scripts/light.ps1') status --verbose
& (Join-Path '<技能目录>' 'scripts/light.ps1') doctor
& (Join-Path '<技能目录>' 'scripts/light.ps1') deactivate
```

普通模式启用后可进入 Plan 模式持续跟踪，无需每轮运行脚本。`activate` 取得可信当前轮次才亮蓝灯；返回 `pending_next_prompt` 时仅绑定待命、保持熄灯，等待真实根聊天 Hook 上下文，不保证下一条消息必定有效，不猜测身份或自行重新启用。后台重启后也需再次明确启用。`deactivate` 在任意聊天均全局熄灯并清除绑定。

身份优先使用 `CODEX_THREAD_ID`，缺失才用 `CODEX_SESSION_ID`；不得猜测或替换。只有根代理控制灯，子代理只汇报结果；其他聊天的普通事件不影响绑定。

## 自动状态与最终标记

启用时均为 **SOLID 常亮、75% 亮度**：工作蓝、完成绿、等待用户红、阻塞黄。Hooks 处理开始、恢复、提问、权限等待及取消；桌面异步回答按问题调用和题号匹配，仍有未答问题就保持红灯。正常耗时和可自行解决的重试保持蓝灯。

根代理只在最终答复的最后一行追加 `<!-- aaa-wudi:{state} turn={turn_id} binding={binding_nonce} -->`，替换三个占位值：

| state | 使用条件 |
|---|---|
| `completed` | 当前请求已完成 |
| `needs_user` | 等用户回答、选择、授权、协助，或正式方案等待实施 |
| `blocked` | 当前无法继续且不是等待用户答复 |

标记放在代码围栏、引用、示例及 `<proposed_plan>` 块之外，只用于根代理 final。仅使用**本轮 Hook 或本轮 activate** 提供的有效 `turn_id`、`binding_nonce`；不得借用 status、旧轮次或其他聊天的值，缺少有效上下文时不发标记。Plan 待实施用 `needs_user`，标记放在 `</proposed_plan>` 之后。未解决等待优先保持红灯，工具成功、子代理结束或回合停止不代表任务完成；缺失有效最终标记时熄灯并记录诊断。

普通模式必要时才用 `set` 后备入口，必须同时带本轮 `--turn-id` 和当前 `--binding-nonce`；无 nonce 会拒绝。不要用后备入口绕过待答记录。

## 诊断与边界

`binding_accepted` 代表绑定成功，`tracking_ready` 代表已有可信轮次，`confirmed` 仅代表设备当前连接已写入并回读匹配。熄灯回读成功不代表自动跟踪可用；`observed` 不是连续实时采样。详情见 `status --verbose` / `doctor`，`bound_event_counts` 记录本次绑定后接受的事件。

灯故障不阻塞原任务，未启用时重连不点灯。只有新增或改动的 Hook 定义走 Codex 正常审核，不重复请求既有信任。本技能不改变任务权限或完成标准，不刷固件、不读取历史聊天、不修改无关配置，无需 MCP。强制结束进程或电脑突然退出后固件可能保留最后颜色，不据此推断任务仍在运行。

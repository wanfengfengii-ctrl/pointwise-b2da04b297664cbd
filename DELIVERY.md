# 交付说明：无人值守租约导出采集

本交付在既有归档核（archive.py）、一次性人工链路（collector.py/manual.py）与发布设施（ops.py）之上，提供可恢复的采集 worker、便携监督进程 keeper 与 macOS LaunchAgent 自启动，全部沿现有入口交付。门户与归档业务未重写。

## 构建、校验、安装

```
python3 ops.py build OUT.pyz                 # 确定性 ZIP 应用 + OUT.pyz.sha256
python3 OUT.pyz ops install OUT.pyz PREFIX   # 先核验 sha256 与模块清单，再原子替换 PREFIX/app.pyz
```

损坏或与摘要不符的包在安装时报 BAD_PACKAGE 并拒绝，已安装的 app.pyz 不被替换。安装后数据在 PREFIX/state，日志在 PREFIX/logs。安装应用支持全部相同入口（submit/status/read/agent/keeper/portal/manual/worker），脱离开发源码运行。

## 提交与查询

```
python3 PREFIX/app.pyz ops submit PREFIX SOURCE_URL   # 冻结任务；同URL重投返回原状态，异URL拒绝(TASK_CONFLICT)
python3 PREFIX/app.pyz ops status PREFIX              # idle|queued|collecting|awaiting_ack|complete|failed
python3 PREFIX/app.pyz ops read PREFIX                # 按 export_id 排序的档案数组（含稳定回执）
```

status 为含 state、export_id、receipt、error 的 JSON 对象。export_id 未确定为 null，确定后不改绑；receipt 归档前为 null，归档后为原稳定回执；complete 表示本地归档完整且远端已确认删除；failed 必带非空 error。阶段与错误查看：`ops status`，以及 PREFIX/logs/stderr.log 中 worker 的过渡与重试日志。

## 用户级自启动与自动重开

- macOS（验收环境）：`python3 PREFIX/app.pyz ops agent PREFIX enable` 安装 LaunchAgent（RunAtLoad+KeepAlive，固定安装时的绝对解释器路径，标签 local.pointwise.capture.<hash>，plist 在 ~/Library/LaunchAgents）。worker 被杀或崩溃由 launchd 自动重开；`... agent PREFIX disable` 停用并移除 plist。
- 其他用户会话：`python3 PREFIX/app.pyz keeper PREFIX` 前台监督进程，提供相同的立即启动与退出重开语义；SIGTERM/SIGINT 或创建 PREFIX/state/keeper.stop 可停止。

## 恢复语义（自动收敛，无需人工介入）

worker 将任务绑定（binding.json）、阶段（status.json）与档案（archive.sqlite）持久化，重开后据此收敛：

- 下载中被终止：重新领取当前令牌从头重下（不做断点续传）。
- 归档提交后被终止：重领令牌后核对摘要、长度与原文字节完整性，复用已归档内容直接确认，不重下；下载跨租约过期不会停滞。
- 确认结果不明（响应丢失/超时）：重新领取；源已删则只报空，此时仅凭“本任务已持久归档且身份匹配”的证据判定完成；无归档证据遇空源明确失败（REMOTE_EMPTY）。空源或超时本身从不被当作完成。
- 网络中断、超时、租约失效：自动重试，不进入 failed。坏正文（CONTENT_MISMATCH）、同身份异摘要/改绑（IDENTITY_CONFLICT/IDENTITY_REBIND）、档案损坏（ARCHIVE_DAMAGED）明确失败且不得确认。
- 远端调用与等待不占用 SQLite 写事务；档案与回执全局唯一。

## 停用与卸载

```
python3 PREFIX/app.pyz ops agent PREFIX disable   # macOS 停用 LaunchAgent（keeper 场景：SIGTERM 或 touch PREFIX/state/keeper.stop）
rm -rf PREFIX                                     # 删除安装数据（app.pyz、state、logs）
```

## 上线核账（verify 实跑输出）

`python3 checks.py` 重跑原业务回归，并经安装产物做真实进程级核账：正常采集、每次下载跨期后重领确认、下载中终止、archived 处终止、确认已生效但响应丢失后在 ack_attempted 处终止、坏正文拒绝、源不可达自动重试、档案损坏拒绝，以及损坏包拒绝与同URL重投。macOS 上经真实 LaunchAgent 启动与终止后自动重开；其他平台经安装产物内 keeper 完成同等核账。实跑输出：

```
BUSINESS_REGRESSION_OK
GOLIVE_NORMAL_OK
GOLIVE_EXPIRY_OK
GOLIVE_KILL_DOWNLOAD_OK
GOLIVE_KILL_ARCHIVED_OK
GOLIVE_LOST_ACK_OK
GOLIVE_BAD_BODY_OK
GOLIVE_NETWORK_RETRY_OK
GOLIVE_DAMAGED_ARCHIVE_OK
GOLIVE_OK
```

`python3 checks.py --demo` 经安装应用展示真实采集与远端删除结果（实跑，receipt_id 每次由归档核新生成）：

```json
{"archive": [{"export_id": "notice-17", "length": 24, "raw_base64": "5YWs5ZGKMTcK5Y+W5rC06YeP77yaMTIK", "receipt": {"export_id": "notice-17", "length": 24, "receipt_id": "f7ce2321c73c4d2db120f7b31b34c909", "sha256": "8695bbda668a795e0880c13b93ee639ae0f4d88a08369ede7b70f04a1567cda4"}, "sha256": "8695bbda668a795e0880c13b93ee639ae0f4d88a08369ede7b70f04a1567cda4"}], "audit": {"acked": true, "deleted": true}, "status": {"error": null, "export_id": "notice-17", "receipt": {"export_id": "notice-17", "length": 24, "receipt_id": "f7ce2321c73c4d2db120f7b31b34c909", "sha256": "8695bbda668a795e0880c13b93ee639ae0f4d88a08369ede7b70f04a1567cda4"}, "state": "complete"}}
```

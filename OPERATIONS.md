# 租约导出采集的发布交付

门户每天交出一份待删除的原始导出。archive.py是已有归档核：校验完整字节，按export_id唯一保存原文与稳定回执；collector.py和manual.py是正确的一次性人工领取、下载、归档、确认链路。worker.py目前只会轮询调用人工链路，遇到错误即写failed，没有可靠交接恢复。不要重写门户或归档业务来代替部署。

## 实际环境与范围

验收使用macOS登录用户的GUI会话、Python 3.9以上标准库及SQLite，不使用容器、第三方依赖、管理员权限或外部账号。源站是本项目的本地HTTP进程，整个客户端重启实验期间保持同一源进程存活；不要求源站自身灾备。路径可含空格。LaunchAgent固定安装时的绝对解释器路径，验收期间该解释器存在。
每个安装前缀只接纳一个任务，绑定一个源站URL；只有一个采集worker，不要求并发提交、多源调度、软件升级、历史回收或断电耐久性。导出最大1MiB，允许整包读入内存，不做Range或分段续传；下载被杀可以从头重新取。
关键交接约束是下载时间可长于租约：有效令牌打开的下载会继续完成，确认却必须使用当前有效令牌。下载成功后反复重新下载可能永远确认不了。

## 已有运行入口

以下每行都是直接执行的argv形式，无shell包装：
- python3 checks.py
- python3 checks.py --demo
- python3 portal.py data/example.json --port 0
- python3 manual.py collect ROOT URL
- python3 manual.py read ROOT
- python3 ops.py build OUT.pyz
- python3 ops.py install OUT.pyz PREFIX
- python3 ops.py submit PREFIX URL
- python3 ops.py status PREFIX
- python3 ops.py read PREFIX
- python3 ops.py agent PREFIX enable
- python3 ops.py agent PREFIX disable

portal启动后首行输出真实source_url；端口0由系统分配，只监听127.0.0.1。源启动后可另开终端运行manual collect。
build生成确定性ZIP应用和OUT.pyz.sha256，install先核验再原子安装PREFIX/app.pyz；数据在PREFIX/state，日志在PREFIX/logs。pack、build、install、agent可复用，不要求重新发明发布框架。安装应用也支持相同入口，例如python3 /absolute/PREFIX/app.pyz ops submit /absolute/PREFIX URL；启用代理后由安装应用的worker推进任务。
submit将source_url冻结到state/task.json，不等待采集完成；同URL重投返回原状态，异URL拒绝且不修改原任务。只有一个提交者，不要求实现提交竞争协议。未提交status为idle。
交付后的status仍是一个JSON对象，包含state、export_id、receipt、error。state使用idle、queued、collecting、awaiting_ack、complete、failed；可不暴露转瞬即逝的collecting。身份未确定时export_id为null，确定后不能改绑。receipt在归档前为null，归档后为原稳定回执；complete表示本地归档完整且远端已确认删除，failed须有非空error。可重试故障不能永久停在failed。现有worker只是基线演示，尚未满足这些状态语义。
read输出按export_id排序的JSON数组，每项包含export_id、sha256、length、raw_base64、receipt；raw_base64是原文字节的Base64。保留archive.save与read的业务语义，可扩展运维状态及归档查询接口。
数据样例是UTF-8 JSON，export_id为notice-17，body为“公告17\n取水量：12\n”，归档的是该字符串的UTF-8字节；保留换行和中文原字节。回执含export_id、sha256、length、receipt_id，同身份同内容重复保存返回原回执，同身份异内容拒绝。非法正文不得产生档案。
样例正常结束后只有一份notice-17档案，length=24，sha256=8695bbda668a795e0880c13b93ee639ae0f4d88a08369ede7b70f04a1567cda4，raw_base64=5YWs5ZGKMTcK5Y+W5rC06YeP77yaMTIK；status为complete，源站审计acked和deleted均为true。receipt_id由归档核生成，不固定。

## 源站契约

data/example.json包含export_id、body、lease_seconds、download_seconds。身份为1至64位ASCII字母、数字、下划线或短横线，body编码后不超过1MiB。时间单位为秒；可复制样例并将lease_seconds设为1、download_seconds设为2，得到每次下载都跨租约到期的真实场景。
- POST /claim，JSON请求体{}。待采时返回state=leased、export_id、sha256、length、lease_token、expires_in；sha256为原始字节的64位小写十六进制摘要。每次领取发新令牌并立即废除旧令牌，expires_in只是相对期限，不能代替源端确认。
- GET /download?token=令牌。当前有效令牌返回完整原始字节；开始后跨到期仍交付正文。失效返回HTTP 409及{"error":"LEASE_EXPIRED"}，原文已删除则HTTP 410及{"error":"GONE"}。
- POST /ack，JSON请求体包含export_id、lease_token。仅当前有效令牌可首次确认；成功返回{"state":"acked"}并立即删除原文。无效确认返回HTTP 409及LEASE_EXPIRED。成功后再次claim只返回{"state":"empty"}，不再提供身份、摘要或下载。
确认可能已生效却关闭连接而无任何响应。不能把请求异常当作确认未生效，也不能把empty无条件当作采集完成；必须有本任务已持久归档且身份匹配的证据。原任务尚无归档证据却遇到空源须明确失败。
网络中断、超时及租约失效自动重试；完整HTTP正文长度或摘要不符、同身份异摘要、本地归档损坏明确失败且不得确认。确认前必须核验完整归档；已有同身份档案须核对摘要、长度及原文字节完整性后复用。下载跨期后应复用已归档内容重新领取并确认，不能因每次重下都过期而停滞。远端调用及等待不得占用SQLite写事务。

## 可控故障与上线核账

源站另有仅供测试的接口，生产采集逻辑只能调用claim/download/ack，不能借audit/control完成交接。
POST /control接收JSON对象：expire=true立即使当前令牌失效；bad_download=true使下次下载多出一个字节；drop_ack_reply=true使下次有效确认先删除原文再断开连接。GET /audit返回acked、deleted、current_token和events；事件记录claim、download、ack、rejected_ack、empty及有关令牌，可核对归档与远端删除。
hooks.py提供暂停点：预先创建PREFIX/state/hooks/LABEL.hold，命中后LABEL.reached写入当前进程PID，移除hold才继续。downloaded位于整包下载完成后、归档前；archived位于归档事务提交后、确认前；ack_attempted位于每次确认调用返回或抛错后、推进任务完成前。保留这些语义位置，恢复时已有档案不要求再触发downloaded。
交付必须保留原业务回归，补充经过安装应用的上线核账：正常采集、每次下载都跨期后的重新领取确认、下载中终止、archived处终止、确认已生效但响应丢失后在ack_attempted处终止，以及坏正文拒绝。通过真实LaunchAgent启动及终止后的自动重开，核对本地原文字节、唯一档案及稳定回执，并用源站审计确认当前令牌和实际删除；不能用手工重启worker代替。损坏包必须在替换安装应用前拒绝。
移走开发副本后，安装入口仍能提交、查询并完成采集。交付简短恢复卸载说明及实际上线核账输出，说明如何查看阶段和错误、同URL重投、停用代理及删除安装数据。业务回归内含手工路径的失败边界，不要求把手工命令改成自动恢复服务。

.pointwise-runtime.json是公开运行清单：version为1、kind为cli，setup是准备命令argv数组的列表，verify和demo各是一条argv数组。目前verify通过内存HTTP线缆执行真实客户端与HTTP处理器及SQLite归档，demo展示真实档案及远端删除结果，不绑定端口或写磁盘。交付时verify须同时重跑原业务和安装产物上线核账，demo须经安装应用展示真实采集与确认结果。

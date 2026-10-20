---
status: accepted
---

# 长扫描分离运行，后台进程照常处理其他命令

用户决定（2026-10-17）：agent 的时间和计算的时间要能重叠。no1b 任务里命令之间的空档（agent 思考、写脚本）合计 148 分钟，后台计算 65 分钟（不含 30 分钟以上的等待）；后台进程一次只处理一个请求，长扫描期间 agent 连读结果、看系统设置都要等。

`saos scan --detach` 在本请求里完成校验、建目录和镜头快照（`scans/NNN/base.zmx`），然后把扫描交给后台线程，立即返回。后台线程只用 worker 池（独立的 standalone 实例）和这份快照，不碰后台进程的主实例，所以主实例照常处理后续命令；之后工作镜头怎么改、怎么存都不影响这次扫描。进度写在 `scans/NNN/progress.json`（流程图窗口也读它），结果写在 `scans/NNN/result.json`，`saos scan --show` 只读这些文件，后台进程忙时也能用。`saos job cancel` 同时停掉这个任务的分离扫描。

## Considered Options

- 后台进程整体改成多线程、每个请求一个线程：主实例的 ZOS-API 调用不能并发，所有命令都要加锁，改动面大，收益只在扫描这一类请求上。
- 让 agent 自己用 `run_in_background` 之类的平台功能等命令返回：客户端可以不等，但后台进程仍被占着，其他命令照样排队；而且技能不能依赖某一平台独有的功能。
- `ctx.map` 也支持分离：exec 脚本本身跑在主实例上，分离就要让脚本和后续命令同时用主实例，不安全。需要分离的批量评估写成 `saos scan` 的计划。

## Consequences

worker 池同一时间只给一个扫描或 `ctx.map` 用，忙时立即返回 `POOL_BUSY`；有分离扫描在跑时 `daemon stop` 返回 `BUSY`，先取消或等它结束。后台进程意外退出时扫描中断，`saos scan --show` 会说明没有结果、也没有后台进程在跑它。

2026-10-20 起（ADR-0012）：worker 默认跑 Optiland，不占许可证，“独立的 standalone 实例”只在会话在 OpticStudio 中时成立；分离运行只用 worker 池和镜头快照、不碰主实例的决定不变。

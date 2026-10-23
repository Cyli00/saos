# 在常驻后台进程内执行 agent 编写的脚本

固定命令覆盖不到的 ZOS-API 能力，agent 就用不了。光纤侧视框架用到的折转镜、Biconic 面、半椭球都不在命令里，最后只能绕开后台进程直接写 ZOS-API 代码。因此后台进程提供一个通用入口，执行 agent 写的 Python 脚本：脚本能拿到已连接的系统对象、ZOS-API 命名空间和任务路径。脚本出错只返回错误，不让后台进程退出。每次执行都把脚本副本和返回结果存进任务目录，交付的数值才能追溯到具体是哪段代码算出来的。固定命令只保留通用操作，同一类脚本在多个任务里反复出现，或者涉及覆盖文件、丢失未保存修改这类安全问题时，才升级为固定命令。

## Considered Options

- 只扩充固定命令：每遇到一个新系统都要先改工具，agent 被命令清单卡住。
- 只提供 Python 库，由 agent 自己连接 OpticStudio：每次连接要 4.4–5.6 s（ADR-0002），而且显性调试必须由持有 Interactive Extension 连接的那个进程来操作窗口。

## Consequences

脚本和后台进程在同一个进程里运行。脚本可能改坏系统状态，或者长时间占住后台进程；OpticStudio 的状态本来就由 agent 负责，这个风险接受，不做沙箱。

2026-10-20 起（ADR-0012）：镜头在 Optiland 中时 `ctx.system` 和 `ctx.zosapi` 为空，脚本经 `ctx` 的通用接口（`get`/`set`、`trace`、`op`、`analysis` 等）在两个后端上都能运行；直接调用 ZOS-API 的脚本只能在 `saos zos begin` 之后的 OpticStudio 阶段运行，`SAOS_BACKEND` 不能提前打开 OpticStudio。光纤配方的测量库就是这一类。

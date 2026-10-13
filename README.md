# 玉衡星视频 Skill

将已确认的分镜交给 Agent，用玉衡星视频密钥生成、恢复任务并下载片段。
Python 3.9+，仅使用标准库。主入口和备用入口必须属于同一玉衡星平台。

## 安装与接入

使用固定发布版本 `v1.0.0`，核对网页提供的发布 commit/ZIP SHA256 后，
让 Agent 执行 `setup.py`。密钥仅通过程序标准输入交付，不写在命令参数中，
不打印，不提交 Git。网页的“复制安装与密钥”会提供完整接入信息；它包含
密钥，交给可信 Agent 后会留在其会话历史中。

```text
python3 setup.py --endpoint https://47.92.162.252:588 \
  --fallback-endpoint https://yuhengx.dpdns.org --api-prefix /video/v1 --replace
```

以上命令需由 Agent 的子进程标准输入传入密钥，终端无输入不会继续。
默认 Skill 目录为 `~/.codex/skills/yuheng-video-gen`；其他 Agent 使用
`--skill-dir <该 Agent 的个人 Skill 目录>/yuheng-video-gen`。脚本不自动修改
Agent 配置或 shell profile。密钥写入 `~/.config/yuheng-star/keys/<随机标识>.key`，
`client.json` 引用绝对路径，私密目录 0700、文件 0600。远程 Agent 会安装到
其运行环境，不能直接写入浏览器所在设备。

`--replace` 表示使用本次明确选中的密钥及平台配置。旧密钥不覆盖，旧配置
与旧 Skill 目录或链接本身均备份；历史任务文件保留，不更改提交 ID。
入口配置未改变时，已有 `state_endpoint_aliases` 继续保留；入口改变时，旧别名
只留在配置备份中，不自动启用。媒体超时保留。迁移旧任务入口时仅在
已确认同平台的情况下添加 `--state-endpoint-alias <历史入口>`。不要为恢复
旧任务更换账号。目的路径中的链接或硬链接被拒绝；旧 Skill 的顶层链接
可以备份并替换，不写穿它所指向的源目录。

若 YUHENG_ 或兼容 MUSE_ 的入口/密钥/媒体超时环境变量存在，脚本会拒绝
安装并列出冲突变量名；应在本次安装及后续调用进程清除这些变量，避免
客户端使用另一身份或错误 API 前缀。不需要修改用户全局 shell 配置。

安装只做携带密钥的 GET 查询；已鉴权的 `NOT_FOUND` 响应代表接入成功，
401、其他响应或不可达均明确失败。只在连通故障或 502/504 时尝试明确
指定的备用入口，不跟随重定向。不创建视频，不扣额度。

## 使用与许可

安装后参见 [SKILL.md](SKILL.md)。已有任务继续使用原 state，未知提交
结果不得另建任务重试。密钥和任务资料应保存在私密目录，不进入此仓库。
可在已有 GitHub 登录权限下为仓库 Star；未登录或无权限不影响安装。

遵循 [GNU AGPL v3](LICENSE)。客户端从服务项目现行客户端原样导出，
归属与对应源码说明见 [NOTICE.txt](NOTICE.txt)。

Frontend design and development by New API contributors.

Original project: https://github.com/QuantumNous/new-api

---
name: yuheng-video-gen
description: 用玉衡星视频API key提交已确认分镜，可纯文字或带参考图生成，恢复原任务并下载10秒片段。
---

本Skill只调用玉衡星视频API；本机不持有服务端SSH或管理员凭据。

首次接入：使用已核对固定发布版本的 `setup.py`，密钥通过子进程stdin交付，不放命令参数、不回显。明确主入口、备用入口与 `/video/v1`；默认安装至 `~/.codex/skills/yuheng-video-gen`，其他Agent以 `--skill-dir` 指定其个人Skill目录。用户明确选中新密钥时使用 `--replace`：备份旧配置和Skill，保留旧key及state，私密保存新key并引用绝对路径。安装仅GET鉴权验证，不生成、不扣额；成功后直接使用现有配置，无须用户填写文件路径。环境override冲突须在安装及调用进程明确清除，不改全局shell配置；失败如实报告，不称已接入。详见 [README.md](README.md)。

1. 获取用户已确认的提示词；参考图可选，不传图片即可纯文字出片。每组1–10片、每片10秒；保留用户内容、顺序与原图。
2. 有效接单每片扣一视频单位；失败、未知、下载失败不自动退款。明确重做会产生新费用，网络重试坚持原state。
3. 自动读 `~/.config/yuheng-star/client.json` 的endpoint、api_key_file和api_prefix；显式YUHENG_ENDPOINT/YUHENG_API_KEY_FILE优先。配置与key文件须0600，仅HTTPS或回环HTTP，不打印秘密、不提交Git、不发给其他服务。已有旧配置兼容，不为更名重建任务。
4. plan示例：`{"clips":[{"prompt":"已确认提示词","duration":10,"references":[]}]}`；有图时references填原图绝对路径。plan/state含用户内容，应置于私密目录。
5. `python3 <Skill目录>/yuheng_client.py submit --plan <plan> --state <固定state>`，随后 `wait --state <同state> --output <用户目录>`。第一次请求前持久化提交ID；生成ACK丢失/断网/重启沿同state和原submission恢复，不新建ID。上传仅明确前置SERVICE_BUSY（503）或RATE_LIMIT（429）有限重试；普通503、断线、超时等不明结果留下upload_pending，后续同state禁止盲重POST，先核查原上传，不另建state绕过。不要删除.lock绕过正在执行的提交。
6. 文件下载经大小/SHA256校验后原子完成；失败只重下载同一资产，多片不默认拼接。
7. 明示每片expires_at：完成日之后的北京时间零点，临近午夜可能仅保存数分钟。下载不续期，本地副本独立保留；过期410不得借恢复重新生成。
8. RECONCILING交管理员核查原尝试；PARTIAL_SUCCESS保留已成文件并说明其余状态。不调用管理入口/SSH、不盲重投。
9. 最终交付优先 `![玉衡星视频片段](/成片绝对路径.mp4)`，路径含空格用尖括号，同时提供下载链接、顺序、10秒与云端到期。仅嵌入已校验本地视频，客户端不支持则明确降级为文件链接；展示不得再次扣量或携带key。

媒体socket timeout默认930秒，可由YUHENG_MEDIA_TIMEOUT_SECONDS（兼容MUSE_MEDIA_TIMEOUT_SECONDS）或私密配置media_timeout_seconds调至930–1800秒；此为socket等待而非整段下载总时限。服务端默认媒体等待180秒、共享等待256个，活动上传120秒、原下载lease600秒；等待不签lease、不延长TTL。

双入口接入：主入口为 `https://47.92.162.252:588` 时，在同一私密配置加入 `fallback_endpoint: "https://yuhengx.dpdns.org"`、`api_prefix: "/video/v1"`。只有明确属于同一平台的备用入口可接收该key；不自动跟随重定向。`YUHENG_FALLBACK_ENDPOINT`可显式覆盖，显式覆盖主endpoint时不会偷偷继承旧配置中的备用地址。

客户端只读探测主入口（每次最多两次，每次5秒；正常后60秒内不重复探测）。连通故障或502/504切备用；密钥、权限、额度、限流及业务繁忙不据此切换。备用停留5分钟，再于下一请求只读探测主入口；路由缓存video-route.json不改变任务state。视频开始传输后，连续两个15秒窗口低于100 KiB/s或传输停滞可切换并重下载同一资产，仍校验大小和SHA256。生成与媒体准备等待不当作下载慢速。上传结果未知仍保留upload_pending，禁止自动换路重传；生成提交只复用原submission_id。未配置备用入口时保留原单入口行为。

同一平台从旧回环入口迁移到上述主入口时，可显式配置 `state_endpoint_aliases: ["http://127.0.0.1:28172"]`，让旧state原样恢复；仅加入已经确认连接同一后端的历史入口，不加入其他平台或账号的入口，不修改旧state或重建提交ID。

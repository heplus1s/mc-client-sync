# GitHub 正式版 → Gitee 客户端模组

只复制白名单内的正式 Release 主 JAR：银行、商店、市场、领地和 BotMod（`MinecraftBotMod`，模组 ID 为 `societybots`）。BotMod 从 0.1.3 起同步，且单独的 `latest-bot.json` 仅在一次全量同步中同时验证银行 ≥ 1.0.30、领地 ≥ 1.0.18 后生成或更新。旧 PCL 更新器的 `latest.json` 固定四项（银行、商店、市场、领地）；旧客户端不会被强制安装 Bot。客户端安装 BotMod 还需现有 Fabric API、Carpet。游艺厅不在镜像范围，更新列表会移除先前误加的条目。
目标为 `intelsayamdyes/mc-client-pack`。每个模组版本使用独立 Gitee 发行标签，例如 `bankmod-v1.0.19`。
不复制源码、测试包、收费 Mod 或第三方附件；不删除或覆盖已有附件。只有匿名下载回来的 JAR 与官方 JAR 字节匹配后才提交清单；不自动降级，不改 TOML、资源包或配置。

## 一次性设置

1. 在单独的 GitHub 同步仓库（建议私有 `mc-client-sync`）默认分支放入附件中的 `.github/workflows/`、`scripts/`、`tests/`，这些脚本不上传至 Gitee。
2. GitHub 同步仓库 → Settings → Secrets and variables → Actions → Secrets，新建 `GITEE_TOKEN`。令牌须有目标 Gitee 仓库 Release 写入权限。使用同一令牌尝试 HTTPS Git push；如果账号的 API 令牌不能用于 Git，设置另一个专用 Git 认证 Secret `GITEE_GIT_PASSWORD`。Git 用户名默认为 `intelsayamdyes`，可用 Variables 中的 `GITEE_GIT_USER` 覆盖。保护分支必须允许该同步账号正常推送。不要把令牌发聊天、写文件或贴到 URL。
3. Actions → **Mirror approved client JARs to Gitee** → **Run workflow**，不勾选 apply 先演练。第一轮可填 `mod=bankmod`、`tag=v1.0.19`，确认选到正确的银行 JAR。演练不写 Gitee。
4. 再勾选 apply 运行一次，查日志中的 Gitee 匿名 JAR SHA-256 和匿名清单确认；用 PCL 启动前检查验证玩家正常更新。测试成功后，在 Actions → Variables 增加 `GITEE_SYNC_ENABLED=true`。设置后每小时的 07、22、37、52 分钟检查白名单模组的最新正式版，GitHub 实际排队可能比计划晚。删除变量即可停用定时写入。两次发布太密集时可通过手动指定 `mod`+ `tag`补同步中间正式版。

## 清单模式

- 新版补丁读取独立的 `latest-managed.json`（`kind=latest-managed`）。每行仅指向已验 SHA-256 的 Gitee 正式 JAR；全量验证通过才发布。旧版继续读取 `latest.json` 或 `latest-bot.json`，首次安装的 `install.json` 默认仍不变。
- 以后新增一个自研客户端 Mod，将其仓库、Mod ID 和最低支持版本加入脚本白名单，发布按同一命名格式的 JAR，再运行全量同步。新版客户端无需改脚本。退役时明确登记 `MANAGED_RETIRED`；记录持续保留在 `removedIds`，单纯缺行或下载失败不会触发卸载。退役只影响新版客户端已记录、哈希仍匹配的 JAR，不删 Gitee 旧附件、不动第三方 Mod 或配置。旧的固定四项/五项清单兼容策略保持独立。

- 默认 `GITEE_MANIFEST_MODE=latest`：只改 `master` 的 `latest.json` 中发生变化的自制模组条目，保留 `install.json` 为既有初装基线、两份清单既有的 `release=v3.04` 等包级字段。支持读取 `latest.json` 的 PCL 启动检查之后只会更新原四项；首次安装若仍按 `install.json`，则先用基线，直到启动更新成功。
- `GITEE_MANIFEST_MODE=none`：只同步附件，不写任何 Git 文件，PCL 清单也不会变化。
- `GITEE_MANIFEST_MODE=paired`：在同一个提交流程匹配更新 `latest.json` 和 `install.json`，前提是配好经验证的 `GITEE_ASSETSET_SCHEME`。现有 `assetSet` 生成办法未确认，不应直接开 paired；`canonical-json-sha256` 仅当既有指纹等于脚本的排序紧凑 JSON SHA-256 时才生效，`preserve` 只应在确认启动器不把它当内容校验后使用，否则保留默认模式。

重复运行只验证已有同名附件，不覆盖，哈希不同报错；同一工作流在仓库内按目标串行。Git 不使用 force，远程分支有并行变更时停止后重跑。上传失败或匿名下载暂时不可用时，可能留下未被清单引用的发行条目；下次复用并验证。
仅离线测试，未以真实 Gitee 凭证联调。

[GitHub Releases](https://docs.github.com/en/rest/releases/releases) · [Gitee 官方 SDK API](https://gitee.com/sdk/gitee5j/blob/main/docs/RepositoriesApi.md) · [Gitee 稳定附件链接](https://blog.gitee.com/2022/08/18/update/)

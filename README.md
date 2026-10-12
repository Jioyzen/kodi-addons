# Jioyzen Kodi Add-ons

基于实际使用中持续修复的 Kodi 插件维护仓库，主要验证环境为 **CoreELEC 22 Amlogic-no / Kodi 22 Piers、S905X2**。插件源码、可安装 ZIP、Kodi 仓库索引和下载站由同一份源码生成。

- 下载站：<https://jioyzen.github.io/kodi-addons/>
- 发布包：<https://github.com/Jioyzen/kodi-addons/releases>
- 仓库插件：`repository.jioyzen`

## 已维护的插件

| 插件 ID | 名称 | 维护内容 |
| --- | --- | --- |
| `script.ai.movie.scanner` | AI 海报刮削 | 文件名提取、TMDb 候选校验、长片约束、歧义保留人工确认、类型与合集元数据、图片与演员缓存、手动匹配界面修复 |
| `plugin.video.filteredmovies` | 海报墙 | 首页异步加载、空分类处理、图片与合集兜底、焦点和返回状态、导航边界、背景图、圆角布局、缓存与稳定性优化 |
| `metadata.tmdb.cn.optimization` | TMDB CN Optimization | Kodi 22 数据库定位与缓存兼容修复；保留原作者刮削流程 |
| `script.alist` | Alist 管理 | 独立服务控制、启动就绪核验、自启状态核验、UTF-8 输出、日志与二维码窗口修复 |
| `script.clouddrive2` | CloudDrive2 管理 | 独立服务控制、启动就绪核验、自启状态核验、UTF-8 输出、日志与二维码窗口修复 |

五个插件均已解除 `script.module.cebox` 依赖。媒体插件在自身内部定位有效的 Kodi 数据库；服务管理插件在自身内部包含管理代码和二维码库。

AI 海报刮削仍通过自己的“添加目录”入口扫描、匹配与入库，**不是 Kodi 视频源“设置内容”菜单中的刮削器**。TMDB CN Optimization 则可在该菜单中选择，两者是不同的入库流程。

## 在 Kodi 中安装

1. 进入“设置 → 系统 → 插件”，允许“未知来源”。
2. 从下载站或 Releases 下载 `repository.jioyzen-1.0.0.zip`，保存到盒子可访问的位置。
3. 进入“插件 → 从 ZIP 文件安装”，安装该文件。
4. 进入“从仓库安装 → Jioyzen Kodi Add-ons”，选择需要的插件。

也可在文件管理器添加文件源 `https://jioyzen.github.io/kodi-addons/`，通过 `repository.jioyzen/` 目录安装仓库插件。

已有同 ID 插件的用户可在插件信息的“版本”中选择本仓库提供的维护版本。仓库保留现有插件 ID，升级沿用原来的 `userdata/addon_data/<插件ID>/` 设置、密钥与扫描记录，不需要重新建立媒体库。以后开启插件自动更新即可接收本仓库的新版本。若同时使用其他提供相同 ID 的仓库，需检查实际安装来源；不要同时依赖不同维护分支的更新。

首次安装 AI 海报刮削后，在插件设置中填写自己的 OpenAI 兼容 API 地址、密钥和模型；发布包的 AI 密钥默认值为空。已有用户的密钥继续沿用本机设置。

这是**插件更新**，不会升级 CoreELEC 内核、DTB 或系统固件。

### Alist / CloudDrive2 安装条件

当前发布包是 **CoreELEC aarch64 服务管理插件**，不含后台程序本体，也不负责下载安装后台程序。已完成 CEBOX 迁移的盒子继续使用现有程序与数据。

| 工具 | 程序路径 | 数据路径 | 默认端口 |
| --- | --- | --- | --- |
| Alist | `/storage/.kodi/addons/service.alist/alist` | `/storage/.kodi/userdata/addon_data/service.alist` | 5244 |
| CloudDrive2 | `/storage/.kodi/addons/service.clouddrive/clouddrive` | `/storage/.kodi/userdata/addon_data/service.clouddrive` | 19798 |

程序须为兼容的 aarch64 Linux 版本且具备执行权限；CloudDrive2 还需匹配的 `wwwroot`。首次启动或开启自启时，如缺少 systemd unit，插件可从自带模板创建；存在的 unit 不会被覆盖。管理插件升级不会删除云盘数据或改变现有自启选择。

## 仓库结构

```text
addons/                    插件源码，含 repository.jioyzen
scripts/                   校验、测试、打包、发布脚本
.github/workflows/         GitHub Actions 云端检查与发布
docs/                      生成的 Pages 下载站：索引、校验文件、ZIP 和图标
```

`docs/` 的生成内容不提交到源码分支，由 Actions 打包为 Pages artifact 并部署。下载站使用 HTTPS；仓库索引与 ZIP 通过 SHA-256 校验，另生成 `addons.xml.md5` 兼容文件。

## 后续修改与发布

1. 修改 `addons/<插件ID>/` 的源码，同时提高该插件 `addon.xml` 中的数字版本号。
2. 本地执行：
   ```sh
   python3 scripts/test_repository.py
   python3 scripts/build_repository.py
   ```
3. 提交并推送到 `main`。Actions 自动验证 Python / XML、入口资源、敏感信息、版本递增与 ZIP 校验，然后创建正式 GitHub Release 并部署 Pages。
4. Kodi 定期检查仓库，或手动执行“检查更新”。自动更新开启时安装新版本。

Pull Request 只执行检查与打包，不发布。可通过 Actions 的 `workflow_dispatch` 手动重新运行发布；相同提交已经发布时不覆盖 Release 附件。构建使用 `ubuntu-latest` 云端 runner，不需要本地电脑开机。

不要提交 `userdata`、个人 API 密钥、SMB 地址、扫描历史、媒体数据库或图片缓存。CI 会拒绝常见私密令牌与本次环境的私人地址。已有插件通用 TMDb 客户端配置不等同于用户在设置中输入的 DeepSeek 密钥；用户密钥仅保存在本机。

## 来源与许可

保留插件原有作者信息和许可证，不将所有插件统一改称 Jioyzen 原创。详见 [NOTICE.md](NOTICE.md)。本仓库提供维护分支，不是 Kodi、CoreELEC、TMDb、Alist 或 CloudDrive2 的官方发布。

# Jioyzen Kodi Add-ons

基于实际使用中持续修复的 Kodi 插件维护仓库，主要验证环境为 **CoreELEC 22 Amlogic-no / Kodi 22 Piers、S905X2**。插件源码、可安装 ZIP、Kodi 仓库索引和下载站由同一份源码生成。

- 下载站：<https://jioyzen.github.io/kodi-addons/>
- 发布包：<https://github.com/Jioyzen/kodi-addons/releases>
- 仓库插件：`repository.jioyzen`

## 已维护的插件

| 插件 ID | 名称 | 维护内容 |
| --- | --- | --- |
| `resource.language.zh_cn` | Chinese（Jioyzen） | 以官方简体中文为基础补全 CoreELEC / Kodi 22 设置翻译，沿用标准语言 ID，兼容皮肤及插件中文目录 |
| `script.ai.movie.scanner` | AI 刮削入库 | 文件名提取、TMDb 候选校验、长片约束、歧义保留人工确认、类型与合集元数据、电视剧目录与季集匹配、未知集收录、图片与演员缓存、独立清库、目录分页与隐私保护 |
| `plugin.video.filteredmovies` | 海报墙 | 独立电影／电视剧／季／演员详情页、本地演员作品、分类独立排序、类型及地区筛选、分页与焦点恢复、播放后返回状态、首页增量更新、圆角布局及异步稳定性优化 |
| `skin.embuary.mod.jioyzen` | Embuary Mod（Jioyzen） | 玻璃程序坞桌面、五列三行应用卡片、九张内置壁纸、背景柔化、默认外观；必装依赖海报墙、AI 刮削入库与天气卡片，首次启用自动配置天气 |
| `weather.jioyzen` | 天气卡片 | 中国天气网数据，国内 IP 自动定位、城市／区县中文及拼音搜索、位置保存、30分钟缓存与离线保留，无需账户或密钥 |
| `metadata.tmdb.cn.optimization` | TMDB CN Optimization | Kodi 22 数据库定位与缓存兼容修复；保留原作者刮削流程 |
| `script.alist` | Alist 管理 | 主程序安装与更新、版本显示、独立服务控制、启动就绪核验、自启状态核验、UTF-8 输出、日志与二维码窗口修复 |
| `script.openlist` | OpenList 管理 | 官方主程序安装与更新、独立数据及端口、版本显示、服务控制、自启、日志、后台二维码与密码重置 |
| `script.clouddrive2` | CloudDrive2 管理 | 主程序安装与更新、版本显示、独立服务控制、启动就绪核验、自启状态核验、UTF-8 输出、日志与二维码窗口修复 |

媒体插件及管理插件均无需 `script.module.cebox`。媒体插件在自身内部定位有效的 Kodi 数据库；服务管理插件在自身内部包含管理代码和二维码库。

AI 刮削入库仍通过自己的“添加目录”入口扫描、匹配与入库，**不是 Kodi 视频源“设置内容”菜单中的刮削器**。TMDB CN Optimization 则可在该菜单中选择，两者是不同的入库流程。

## 在 Kodi 中安装

1. 进入“设置 → 系统 → 插件”，允许“未知来源”。
2. 从下载站或 Releases 下载 `repository.jioyzen-1.0.1.zip`，保存到盒子可访问的位置。
3. 进入“插件 → 从 ZIP 文件安装”，安装该文件。
4. 进入“从仓库安装 → Jioyzen Kodi Add-ons”，选择需要的插件。

也可在文件管理器添加文件源 `https://jioyzen.github.io/kodi-addons/`，通过 `repository.jioyzen/` 目录安装仓库插件。

已有同 ID 插件的用户可在插件信息的“版本”中选择本仓库提供的维护版本。仓库保留现有插件 ID，升级沿用原来的 `userdata/addon_data/<插件ID>/` 设置、密钥与扫描记录，不需要重新建立媒体库。以后开启插件自动更新即可接收本仓库的新版本。若同时使用其他提供相同 ID 的仓库，需检查实际安装来源；不要同时依赖不同维护分支的更新。

### 皮肤与天气卡片

安装 **Embuary Mod（Jioyzen）** 时，Kodi 自动解析海报墙、AI 刮削入库与天气卡片的必装依赖；启用皮肤时自动启用天气卡片并将其设为天气提供者，首次初始化开启自动定位并清除旧手动地点。后续可在天气卡片设置中关闭自动获取，输入城市／区县中文、拼音首字母或全拼，从结果中确认并保存位置。

天气数据直接从中国天气网获取，不需要注册、API 密钥或积分。自动定位使用国内出口 IP，按市级查询；位置搜索支持区县。网站数据接口改变时可能需要更新插件。网络失败时保留最近成功的天气，缺少的体感温度、降水概率等字段隐藏。

皮肤内置九张壁纸，默认使用 Mountain Lion 2；已保存的其他壁纸选择继续保留。程序坞、应用卡片、背景柔化、天气卡片及其布局均随插件打包，不依赖开发目录或用户配置文件。详细设置见插件自己的 README。

首次安装 AI 刮削入库后，在插件设置中填写自己的 API 密钥；预填接口 `https://api.deepseek.com` 和模型 `deepseek-flash`，可改用其他 OpenAI 兼容服务。发布包的 AI 密钥默认值为空。已有用户的密钥继续沿用本机设置。

这是**插件更新**，不会升级 CoreELEC 内核、DTB 或系统固件。

### Chinese（Jioyzen）语言包

在“从仓库安装 → Jioyzen Kodi Add-ons → 语言”安装 **Chinese（Jioyzen）**。已安装官方简体中文时，可打开其插件信息的“版本”，选择来自本仓库的版本。

本包保留 `resource.language.zh_cn` 与 `zh_CN`，与官方版使用同一个 ID：安装后替换官方版，不同时保留两份。这样皮肤和其他插件仍能加载标准简体中文目录，无需修改 Kodi。系统界面使用本包的补全翻译；插件、皮肤仍使用各自的翻译文件，未覆盖的文字按 Kodi 原有规则回退英文。

初始维护版 `11.0.103.1` 基于盒子已验证的 `11.0.103`，后续在同一基础上递增最后一位；同步新的官方基础时再调整前三段版本。保留原翻译团队署名。此语言包只更新文本和语言资源，不修改个人设置、字体文件、插件逻辑或内核。

安装后应确认更新来源为 `repository.jioyzen`，并开启该插件的自动更新。不要同时切换不同仓库提供的同 ID 分支。

### Alist / OpenList / CloudDrive2 安装条件

当前发布包是 **CoreELEC aarch64 服务管理插件**。安装包不捆绑主程序，但可从插件菜单手动下载安装官方主程序。已有主程序会被识别，继续使用现有配置。

| 工具 | 程序路径 | 数据路径 | 默认端口 |
| --- | --- | --- | --- |
| Alist | `/storage/.kodi/addons/service.alist/alist` | `/storage/.kodi/userdata/addon_data/service.alist` | 5244 |
| OpenList | `/storage/.kodi/addons/service.openlist/openlist` | `/storage/.kodi/userdata/addon_data/service.openlist` | 5245 |
| CloudDrive2 | `/storage/.kodi/addons/service.clouddrive/clouddrive` | `/storage/.kodi/userdata/addon_data/service.clouddrive` | 19798 |

未安装时，打开管理插件会显示“安装主程序”。已安装时，菜单显示当前版本并提供“检查主程序更新”；查询官方稳定版后，由用户确认下载安装。仅安装管理插件或打开菜单，不会自动下载或更新主程序。

- Alist 发布源：<https://github.com/AlistGo/alist/releases>，使用 `alist-linux-arm64.tar.gz`。
- OpenList 发布源：<https://github.com/OpenListTeam/OpenList/releases>，使用完整的 `openlist-linux-arm64.tar.gz`。首次安装在配置文件中设置 HTTP 端口 5245；已有配置不覆盖，不读取或导入 Alist 的账号、挂载与云盘配置。
- CloudDrive2 发布源：<https://github.com/cloud-fs/cloud-fs.github.io/releases>，使用 `clouddrive-2-linux-aarch64-<版本>.tgz`，包含对应的 `wwwroot`。
- 下载使用 HTTPS，并核验 GitHub 发布资产的 SHA-256、包大小、文件结构及 ARM64 ELF 格式。缺少官方 SHA-256 的发布不自动安装。
- 下载和解包完成后才停止旧服务；备份旧程序及配置，替换后检查进程和管理端口。失败时恢复旧程序、配置及原来的运行状态；自动恢复受阻时保留恢复文件并提示路径。
- 保留已有 systemd unit 和自启选择；原来停止的服务只临时启动进行检查，完成后仍停止。初次安装不自动开启开机自启。
- 用户手动替换 CloudDrive2 程序后，如无法验证已有版本，会显示“版本未知”，不猜测版本。

更新前需约 1.3 GB 可用空间，配置体积较大时还需额外空间。默认下载超时上限为 10 分钟，可取消安装；网络或校验失败不替换旧程序。

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
   python3 -m unittest discover -s scripts -p 'test_*.py' -v
   python3 scripts/build_repository.py
   ```
3. 提交并推送到 `main`。Actions 自动验证 Python / XML、入口资源、敏感信息、版本递增与 ZIP 校验，然后创建正式 GitHub Release 并部署 Pages。
4. Kodi 定期检查仓库，或手动执行“检查更新”。自动更新开启时安装新版本。

Pull Request 只执行检查与打包，不发布。可通过 Actions 的 `workflow_dispatch` 手动重新运行发布；相同提交已经发布时不覆盖 Release 附件。构建使用 `ubuntu-latest` 云端 runner，不需要本地电脑开机。

不要提交 `userdata`、个人 API 密钥、SMB 地址、扫描历史、媒体数据库或图片缓存。CI 会拒绝常见私密令牌与本次环境的私人地址。已有插件通用 TMDb 客户端配置不等同于用户在设置中输入的 DeepSeek 密钥；用户密钥仅保存在本机。

## 来源与许可

保留插件原有作者信息和许可证，不将所有插件统一改称 Jioyzen 原创。详见 [NOTICE.md](NOTICE.md)。本仓库提供维护分支，不是 Kodi、CoreELEC、TMDb、Alist 、OpenList 或 CloudDrive2 的官方发布。

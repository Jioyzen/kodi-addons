# 来源与许可说明

本仓库保存从用户原有 CEBOX 安装迁移并经修改的插件，以及新编写的仓库发布基础设施。保留已有的 `provider-name`、版权声明及许可证文件。

- Chinese（Jioyzen）：基于原安装的官方简体中文语言包 11.0.103，保留 `strings.po` 的 Kodi / Weblate 翻译团队署名，并加入本分支已验证的 CoreELEC 和 Kodi 22 翻译补全；沿用 `resource.language.zh_cn` 以保持语言目录兼容。
- 海报墙：现有代码包含 forbxy 项目来源说明及 GPL v2 许可证文件，保留在插件目录。
- TMDB CN Optimization：来自 Kodi TMDb Python 刮削器及 forbxy/晓的优化版本，保留其 `GPL-2.0-or-later` 元数据和许可证文件。
- AI 刮削入库（原名 AI 海报刮削）：原安装标记作者 `cebox`，暂未确认可核验的原作者公开源码仓库或许可证。不为其擅自声明新的统一开源许可证。
- Alist / CloudDrive2 管理：保留原管理插件元数据与资产；独立管理代码为本次维护改写。此仓库不包含 Alist / CloudDrive2 后台程序。
- OpenList 管理：基于本仓库独立 Alist 管理代码适配，使用独立服务、程序路径、配置与端口；不包含 OpenList 后台程序，安装时从 OpenListTeam 官方发布源下载。图标来自 <https://res.oplist.org/logo/logo.svg>，SVG 原图保留于 `resources/logo.svg`，PNG 为同一图标的兼容显示版本。
- 管理插件内置 segno 二维码库，保留 `resources/lib/vendor/LICENSE.segno` 中的 BSD 许可证与版权声明。
- `repository.jioyzen` 与 `scripts/` 中新编写的发布基础设施使用 MIT，见 `scripts/LICENSE`。该许可不覆盖其他插件的原作者代码、图片或第三方库。

各组件遵循各自已有的许可证。作者或许可来源尚不明确的组件，不应将本仓库的发布行为解释为已获得原作者对任意用途的授权。

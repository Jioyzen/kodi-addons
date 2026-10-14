# Embuary Mod（Jioyzen）

适用于 CoreELEC NO / Kodi 22 的独立皮肤。首页采用玻璃程序坞；海报墙、AI 刮削入库和天气卡片均为必装依赖，由 Kodi 在安装皮肤时解析并安装。仓库需要同时提供这些插件及符合版本要求的安装包。

## 安装依赖

- 海报墙 plugin.video.filteredmovies ≥ 2.5.11：电影与剧集入口。
- AI 刮削入库 script.ai.movie.scanner ≥ 1.10.2：媒体库入口。
- 天气卡片 weather.jioyzen ≥ 0.5.3：首页天气。

上述插件均不依赖本皮肤，不构成循环依赖；各插件自己的依赖也由 Kodi 自动解析。天气插件默认开启自动获取位置，无需密钥。皮肤首次启用时自动启用“天气卡片”，将其设为 Kodi 天气提供者、开启自动定位并清除旧的手动地点。完成后记录初始化标记；后续用户仍可在天气卡片中手动选择地区，不会每次回桌面都重置位置。皮肤启用期间使用自己的天气提供者；插件被禁用或全局天气提供者变成其他插件时，再次进入首页会自动恢复天气卡片。网络请求由 Kodi 原生天气管理器异步执行，皮肤初始化脚本只做一次短暂配置，不常驻。皮肤仍保留插件暂时禁用或天气数据不可用时的兼容处理。

## 默认外观

默认值采用当前开发盒子确认的配置：

- 内置壁纸 Mountain Lion 2（background_overlay=15）。
- 红色主题、Default 字体、默认纹理、100% 界面缩放。
- 评分显示为媒体评分（circle_rating=true）。
- 首页所有现有入口显示，动态媒体背景启用，滑动动画、自动滚动、触摸模式、个人头像／姓名、右上角旧天气信息关闭。
- 天气卡片布局、图标、程序坞、设置卡片透明度及其他页面背景的柔化与压暗效果均在皮肤 XML 和资源中，无需导入 userdata。
- 可选背景路径、旧辅助插件设置保持为空；不带入过滤条件、媒体库位置、播放记录和天气位置等个人／运行数据。

Includes_Defaults.xml 使用 Kodi 原生 Skin.SetString / Skin.SetBool，在首次打开首页、皮肤设置或启动窗口时初始化。空背景值采用默认壁纸；首次初始化评分显示。已有的非空壁纸选择和明确的评分模式优先保留。初始化标记防止重新进入首页／重载皮肤时覆盖用户修改；Skin.ResetSettings 清除标记后会恢复这些默认值。其他布尔设置沿用 Kodi 原生 false 默认值。

## 内置背景

背景切换使用原有艺术图片配置按钮：7种原始纹理，以及9张壁纸。

| 编号 | 壁纸 | 插件内资源 |
| --- | --- | --- |
| 8 | Monterey Dark | media/home/background.jpg |
| 9 | Odyssey | extras/backgrounds/odyssey.jpg |
| 10 | Apple Glow | extras/backgrounds/apple-glow.png |
| 11 | Astrophotography | extras/backgrounds/astrophotography.jpg |
| 12 | Delphinium | extras/backgrounds/delphinium.jpg |
| 13 | Monterey Layers | extras/backgrounds/monterey-layers.jpg |
| 14 | Mountain Lion 1 | extras/backgrounds/mountain-lion-1.jpg |
| 15 | Mountain Lion 2 | extras/backgrounds/mountain-lion-2.jpg |
| 16 | Translucent Floral | extras/backgrounds/translucent-floral.jpg |

全部资源使用 special://skin/ 路径，随皮肤打包；不依赖开发机器的 wallpaper 文件夹或盒子的绝对文件路径。媒体背景在启用时优先于内置壁纸。

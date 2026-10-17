# 抖音视频下载器（Web 版 + 命令行版）

粘贴抖音分享链接，下载**无水印视频（优先 1080p）+ 封面 + 完整文案 + 数据元信息**，用于自媒体对标研究与自有内容备份。零依赖，只用 Python 3 标准库。

## 两种用法

### 1. Web 版（部署到服务器，浏览器打开就能用）

```bash
python3 server.py                      # 默认端口 8901
PORT=9000 python3 server.py            # 自定义端口
DOUYIN_DL_CODE=你的口令 python3 server.py   # 开启访问口令（推荐）
```

浏览器打开 `http://服务器地址:8901`，粘贴链接 → 解析 → 点按钮下载。

接口：

| 接口 | 说明 |
| --- | --- |
| `GET /` | 网页界面 |
| `POST /api/parse` `{url}` | 解析链接，返回作者/文案/点赞评论转发/封面等元数据 |
| `GET /api/download?id=&q=1080` | 代理下载无水印视频（q=1080 优先高清，省略为默认档） |
| `GET /api/cover?id=` | 代理下载封面 |

常驻运行可用 systemd：把本仓库放到 `/opt/douyin-downloader`，改 `douyin-dl.service` 里的口令后
`cp douyin-dl.service /etc/systemd/system/ && systemctl enable --now douyin-dl`。

> 为什么不能只用 GitHub Pages？Pages 只能托管静态页面，而解析必须由服务端完成
> （cookie 预热、服务端抓取分享页数据、代理下载），浏览器直连会被跨域与平台风控挡住。
> 所以 GitHub 负责存代码，服务跑在自己的服务器上。

### 2. 命令行版（本机使用）

```bash
python3 douyin-dl.py "https://v.douyin.com/xxxxxx/"
python3 douyin-dl.py            # 交互模式：一行一个链接，可粘贴整段分享文字
```

产出落在 `downloads/`：`.mp4` 视频、`.jpg` 封面、`.txt` 文案、`.json` 元数据；图集作品自动逐张下图。

## 原理

短链展开取作品 ID → 预热 iesdouyin 首页拿到 `ttwid` cookie（跳过这一步分享页只返回空壳，是关键）→
读分享页内置数据 `_ROUTER_DATA` 拿到播放地址 → `playwm` 换 `play` 得无水印地址、`ratio` 改 `1080p` 取高清档 → CDN 直下载 / 服务端代理下载。

## 注意

- 平台改版或风控收紧时可能暂时失效，属于这类工具的常态；解析失败先稍后重试。
- 公开部署请务必设置访问口令，并控制使用频率：服务器 IP 被平台限流是迟早的事，别拿它当公共服务运营。
- 下载内容版权归原作者所有，请勿搬运冒充原创。

## 实测

2026-10-17 端到端实测通过（公开视频，11 秒）：Web 版解析接口、1080p 代理下载（1,944,318 字节）、封面下载均正常；命令行版产出齐全。

## 手机当 APP 用（PWA）

已做 PWA 适配：部署后用手机浏览器打开站点，安卓 Chrome 菜单选「添加到主屏幕」、iPhone Safari 选「添加到主屏幕」，桌面会出现图标，点开是全屏界面，跟原生 APP 一样用。

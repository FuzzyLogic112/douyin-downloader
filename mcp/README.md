# 抖音下载器 · MCP 版

把抖音下载器包装成 **MCP (Model Context Protocol) 工具**：任何支持 MCP 的 AI 客户端
（Claude Desktop / Claude Code / Cursor / Windsurf / Muse 等）填一段配置，
就能把下载器当原生工具调用，不用再教它命令行参数。

零第三方依赖：只用 Python 3.8+ 标准库。解析引擎就是仓库根目录的 `douyin-dl.py`，本目录不复制引擎。

## 提供的工具（3 个）

| 工具 | 作用 | 主要参数 |
|---|---|---|
| `douyin_parse` | 只解析不下载：作者、文案、发布时间、点赞/评论/转发、音乐、封面、是否图集 | `url`（链接或整段分享文本） |
| `douyin_download` | 下载无水印视频（图集则逐张下图）+ 封面 + 文案 txt + 元数据 json，返回绝对路径和文件大小 | `url` 必填；`quality` auto/1080/720；`output_dir` 可选；`use_ffmpeg` 可选 |
| `douyin_remove_watermark` | 给本地已带烧录水印的视频做 delogo 修复（需 ffmpeg），下载来的无水印源用不到 | `file_path` 必填；`region` auto/tl/br/x,y,w,h；`preview` 是否出对照图 |

返回值是结构化 JSON（含 `structuredContent`），AI 能直接读到成功/失败、文件路径和元数据，不用解析人话输出。

## 安装（3 步）

1. 克隆仓库（或把仓库拷到目标机器）：
   ```bash
   git clone https://github.com/FuzzyLogic112/douyin-downloader.git
   ```
2. 把 `mcp/mcp.json.example` 里的配置改好绝对路径，填进你的 AI 客户端的 MCP 配置文件：
   - Claude Desktop：`claude_desktop_config.json` 里合并 `mcpServers` 段
   - Cursor / Windsurf：设置里的 MCP 配置，格式相同
   - Windows 上把 `python3` 改成 `python`（或 py 启动器路径）
3. 重启客户端，在工具列表里应能看到 `douyin_parse / douyin_download / douyin_remove_watermark`。

## 配置说明

- `DOUYIN_DL_PATH`（可选环境变量）：`douyin-dl.py` 的完整路径。不设时按“服务器同目录 → 上级目录 → 当前目录”顺序找。
- `DOUYIN_DOWNLOAD_DIR`（可选环境变量）：默认下载目录。不设时落在引擎旁的 `downloads/`。每次调用也可以用 `output_dir` 参数临时指定。
- ffmpeg 可选：装了自动做 faststart + 元数据 + 封面嵌入；没装自动跳过，不影响下载。

## 手工自测（不经 AI）

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 | python3 mcp/douyin_mcp_server.py
```
应返回服务器信息和 3 个工具的定义。日志打在 stderr，不污染协议通道。

## 注意

- 下载是同步阻塞调用：长视频可能跑几分钟，请把客户端的工具调用超时调大（如 600 秒）。
- 同一 IP 高频请求会触发抖音风控，失败时工具会返回 `success:false` 和原因，稍后重试即可。
- 仅用于公开作品的对标研究与自有内容备份，请尊重原作者版权。

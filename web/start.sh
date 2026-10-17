#!/bin/sh
# 抖音下载器 Web 版启动脚本
# 可选环境变量：PORT（默认 8901）、DOUYIN_DL_CODE（访问口令，强烈建议设置）
cd "$(dirname "$0")"
exec python3 server.py

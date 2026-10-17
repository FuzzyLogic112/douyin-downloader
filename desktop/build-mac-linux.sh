#!/bin/bash
# macOS / Linux 打包：生成单文件可执行程序（dist/抖音下载器）
cd "$(dirname "$0")" || exit 1
python3 -c "import PyInstaller" 2>/dev/null || python3 -m pip install pyinstaller
python3 -m PyInstaller --noconfirm --clean --onefile --windowed --name "抖音下载器" --icon icon.png douyin_app.py
echo "完成：dist/抖音下载器"

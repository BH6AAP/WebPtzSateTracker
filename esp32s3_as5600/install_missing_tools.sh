#!/bin/bash
# 下载并安装 arduino-esp32 3.3.11 缺失的工具链 (macOS arm64)
# 兼容 bash 3.2 (macOS 自带), 不用关联数组
# 用法: bash install_missing_tools.sh
set -e

TOOLS_DIR="$HOME/Library/Arduino15/packages/esp32/tools"
WORK_DIR="$HOME/Downloads/esp32-tools-dl"
mkdir -p "$WORK_DIR"

# 使用并行数组: 名称 / 版本 / 下载URL / SHA256
TOOL_NAMES=(esp-x32 xtensa-esp-elf-gdb openocd-esp32 esptool_py)
TOOL_VERS=(2601 17.1_20260402 v0.12.0-esp32-20260424 5.3.1)
TOOL_URLS=(
  "https://github.com/espressif/crosstool-NG/releases/download/esp-14.2.0_20260121/xtensa-esp-elf-14.2.0_20260121-aarch64-apple-darwin.tar.gz"
  "https://github.com/espressif/binutils-gdb/releases/download/esp-gdb-v17.1_20260402/xtensa-esp-elf-gdb-17.1_20260402-aarch64-apple-darwin24.5.tar.gz"
  "https://github.com/espressif/openocd-esp32/releases/download/v0.12.0-esp32-20260424/openocd-esp32-macos-arm64-0.12.0-esp32-20260424.tar.gz"
  "https://github.com/espressif/esptool/releases/download/v5.3.1/esptool-v5.3.1-macos-arm64.tar.gz"
)
TOOL_SHAS=(
  "763755178c15299f8d6f3c88b121f9f43d06be499f11be4a5de51bf3e75b3022"
  "da97440e74a9ff36370bdb598cf421a8183c11ae6fb44431be594ad16dbe77ef"
  "c7bffa205ca92a69ae7bc74e6e428824084e404355cbb9df2238fe30f5f435bb"
  "f63f7203d88cfe4c17aea34d6cf82769458ce204e49a05816c6384c2d299e6ca"
)

N=${#TOOL_NAMES[@]}
for ((idx=0; idx<N; idx++)); do
  tool="${TOOL_NAMES[$idx]}"
  ver="${TOOL_VERS[$idx]}"
  url="${TOOL_URLS[$idx]}"
  sha="${TOOL_SHAS[$idx]}"
  file="$WORK_DIR/$(basename "$url")"
  dest="$TOOLS_DIR/$tool/$ver"
  echo ""
  echo "===== [$tool] $ver ====="

  if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
    echo "  [OK] 已存在: $dest (跳过)"
    continue
  fi

  # 断点续传下载 (最多重试5轮)
  echo "  下载 $file"
  for i in 1 2 3 4 5; do
    curl -L --retry 3 --continue-at - -o "$file" "$url" 2>/dev/null && break
    echo "  下载中断, 重试 $i/5..."
    sleep 2
  done

  # 校验 SHA-256
  echo "  校验 SHA-256..."
  if [ -f "$file" ]; then
    got=$(shasum -a 256 "$file" | awk '{print $1}')
    if [ "$got" != "$sha" ]; then
      echo "  [FAIL] 校验失败! 期望 $sha"
      echo "  实际 $got"
      echo "  请重新下载: $url"
      exit 1
    fi
    echo "  [OK] 校验通过"
  else
    echo "  [FAIL] 文件不存在, 下载失败"
    exit 1
  fi

  # 解压到目标 (去掉顶层目录)
  mkdir -p "$dest"
  echo "  解压到 $dest ..."
  tar -xzf "$file" -C "$dest" --strip-components=1
  echo "  [OK] $tool 安装完成 ($(du -sh "$dest" | awk '{print $1}'))"
done

echo ""
echo "=========================================="
echo "所有工具安装完成！请重启 Arduino IDE"
echo "=========================================="

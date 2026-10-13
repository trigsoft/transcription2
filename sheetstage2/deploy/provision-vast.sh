#!/usr/bin/env bash
# Vast PROVISIONING_SCRIPT: fetch the published source, then reuse the installer.
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo '请以 root 执行。' >&2; exit 1; }
ROOT="${WORKSPACE:-/workspace}/transcription"
mkdir -p "$ROOT/deploy"
exec > >(tee -a "$ROOT/deploy/provision.log") 2>&1
exec 9>"$ROOT/deploy/provision.lock"
flock -n 9 || { echo '另一个安装任务正在运行。' >&2; exit 1; }
if [[ -f "$ROOT/deploy/vast-provisioned" ]]; then
 echo '已经安装成功，网站由 Supervisor 自动启动。'
 exit 0
fi
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
echo '下载 transcription2 v0.1.1 源码……'
curl -fL --retry 2 --connect-timeout 30 --max-time 300 \
 https://github.com/trigsoft/transcription2/archive/refs/tags/v0.1.1.tar.gz -o "$archive"
tar -xzf "$archive" --strip-components=1 -C "$ROOT"
if bash "$ROOT/sheetstage2/deploy/install-vast.sh"; then
 :
else
 status=$?
 [[ $status == 42 ]] || exit "$status"
 echo 'TorchAudio 不兼容，自动改用系统 Python 的 CUDA 12.8 配套方案。'
 bash "$ROOT/sheetstage2/deploy/install-vast.sh" --use-cu128
fi
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$ROOT/deploy/vast-provisioned"
echo "安装成功；日志：$ROOT/deploy/provision.log"

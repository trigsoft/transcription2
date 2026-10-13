#!/usr/bin/env bash
# Run on Vast Ubuntu 24.04; check dependencies and actual TorchAudio loading.
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PY=/venv/main/bin/python
USE_CU128=0
case "${1:-}" in
  '') ;;
  --use-cu128) USE_CU128=1; PY=/usr/bin/python3; export PIP_BREAK_SYSTEM_PACKAGES=1 ;;
  *) echo '用法：install-vast.sh [--use-cu128]' >&2; exit 1 ;;
esac
PORT=8791
install_started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
install_started_seconds=$SECONDS
finish_install() {
 local status=$? elapsed=$((SECONDS - install_started_seconds)) finished_at
 trap - EXIT
 finished_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
 echo "安装结束：$finished_at；耗时 ${elapsed} 秒；退出码 $status"
 mkdir -p "$ROOT/deploy" && printf '%s\t%s\t%s\t%s\n' \
  "$install_started_at" "$finished_at" "$elapsed" "$status" >> "$ROOT/deploy/install-times.tsv" || true
 exit "$status"
}
trap finish_install EXIT
echo "安装开始：$install_started_at"
export DEBIAN_FRONTEND=noninteractive
trap 'install_status=$?; echo "安装失败：第 $LINENO 行。请检查上方错误；已有模型和用户数据未删除。" >&2; exit "$install_status"' ERR
[[ $EUID == 0 ]] || { echo '请以 root 执行。' >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo '仅支持 Linux x86_64。' >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo '此脚本仅适用于 Ubuntu 24.04。' >&2; exit 1; }
[[ -f /etc/vast-agents-guide.md && -x $PY ]] || { echo '未找到 Vast 预装环境。' >&2; exit 1; }
[[ -d $ROOT/muscriptor/muscriptor && -f $ROOT/sheetstage2/pdf-web/dist/index.html ]]
command -v supervisorctl >/dev/null
[[ -f /opt/supervisor-scripts/utils/logging.sh && -f /opt/supervisor-scripts/utils/environment.sh ]]
cd "$ROOT"
/venv/main/bin/python - <<'PY'
import sys,torch
assert sys.version_info[:2] == (3,12), '需要预装 Python 3.12'
assert torch.cuda.is_available(), 'CUDA GPU 不可用'
assert torch.ones(16,device='cuda').sum().item()==16
print('预装 Python / PyTorch / GPU 检查通过')
PY
if [[ $USE_CU128 == 0 ]]; then
 "$PY" sheetstage2/deploy/select_vast_audio.py --preflight
fi
# Do not overwrite another application's supervisor entry or occupied port.
"$PY" - "$ROOT" <<'PY'
import socket,sys
from pathlib import Path
root=Path(sys.argv[1]); config=Path('/etc/supervisor/conf.d/transcription-sheetsage2.conf')
wrapper=Path('/opt/supervisor-scripts/transcription-sheetsage2.sh')
if config.exists():
 assert wrapper.exists() and str(root) in wrapper.read_text(), '已有同名服务属于其他目录'
else:
 with socket.socket() as s:
  assert s.connect_ex(('127.0.0.1',8791)) != 0, '8791 已被其他程序占用'
PY
mkdir -p deploy cache/huggingface tools/bin tools/musescore
if [[ ! -f service.env ]]; then
  {
    printf 'export PYTHONPATH=%q\n' "$ROOT/sheetstage2/src:$ROOT/muscriptor"
    printf 'export SHEETSTAGE2_DATA_DIR=%q\n' "$ROOT/data"
    printf 'export HF_HOME=%q\n' "$ROOT/cache/huggingface"
    printf 'export HF_MODULES_CACHE=%q\n' "$ROOT/cache/huggingface/modules"
    printf 'export TORCH_HOME=%q\n' "$ROOT/cache/torch"
    printf 'export PLAYWRIGHT_BROWSERS_PATH=%q\n' "$ROOT/cache/playwright"
    printf 'export MUSCRIPTOR_MUSESCORE=%q\n' "$ROOT/tools/bin/mscore"
    printf 'export SHEETSTAGE2_MUSCRIPTOR_PYTHON=%q\n' "$PY"
    cat <<'ENV'
export SHEETSTAGE2_RELEASE_MODEL_AFTER_TRANSCRIPTION=1
export HF_HUB_OFFLINE=1
export SHEETSTAGE2_COOKIE_SECURE=0
export SHEETSTAGE2_MAIL_PROVIDERS=''
export SHEETSTAGE2_GMAIL_EMAIL=''
ENV
  } > service.env
fi
chmod 600 service.env
. ./service.env
if [[ $USE_CU128 == 1 ]]; then
 export SHEETSTAGE2_MUSCRIPTOR_PYTHON="$PY"
else
 [[ $SHEETSTAGE2_MUSCRIPTOR_PYTHON == "$PY" ]] || { echo 'service.env 的解释器与本次部署不一致。' >&2; exit 1; }
fi
# Reuse an existing login or HF_TOKEN; never print credentials.
/venv/main/bin/python - <<'PY'
from huggingface_hub import get_token
assert get_token(), '缺少 HF token：请设置 HF_TOKEN，或把 token 放到 HF_HOME/token；需要有 MuScriptor Large 访问权限'
PY
if [[ ! -f deploy/environment-before-install.txt ]]; then
 "$PY" -m pip freeze > deploy/environment-before-install.txt
fi
if [[ $USE_CU128 == 1 ]]; then
 echo '在系统 Python 安装 CUDA 12.8 配套；保留平台原有环境……'
 "$PY" -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu128 \
  torch==2.8.0+cu128 torchaudio==2.8.0+cu128
fi
TORCH_VERSION=$("$PY" -c 'import torch; print(torch.__version__)')
# Reuse installed TorchAudio and verify actual binary loading.
AUDIO_VERSION=$("$PY" sheetstage2/deploy/select_vast_audio.py --install)
echo '安装系统工具和项目依赖……'
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl python3-dev ffmpeg libsndfile1 \
 libegl1 libopengl0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-shape0 libxcb-util1 \
 libnss3 libasound2t64 libglib2.0-0t64 libfontconfig1 libjack-jackd2-0 xvfb xauth libpipewire-0.3-0t64
"$PY" - "$TORCH_VERSION" "$AUDIO_VERSION" <<'PY'
from pathlib import Path
import sys
lines=Path('sheetstage2/deploy/requirements-vast-cu130.txt').read_text().splitlines()
lines=[x for x in lines if not x.startswith(('torch==','torchaudio=='))]
lines += ['torch=='+sys.argv[1], 'torchaudio=='+sys.argv[2]]
Path('deploy/requirements-selected.txt').write_text('\n'.join(lines)+'\n')
PY
"$PY" -m pip install --no-cache-dir -r deploy/requirements-selected.txt
"$PY" - "$TORCH_VERSION" <<'PY'
import sys,torch
assert torch.__version__==sys.argv[1], '安装改变了预装 PyTorch 版本'
PY
"$PY" "$ROOT/sheetstage2/deploy/select_vast_audio.py" --preflight
"$PY" -m pip freeze > deploy/environment-vast.txt
"$PY" -m playwright install --with-deps chromium
if [[ ! -x tools/musescore/bin/mscore4portable ]]; then
 tmp=$(mktemp -d)
 curl -fL --retry 2 --connect-timeout 30 --max-time 600 \
  https://github.com/musescore/MuseScore/releases/download/v4.7.4/MuseScore-Studio-4.7.4.260706075-x86_64.AppImage \
  -o "$tmp/musescore.AppImage"
 chmod +x "$tmp/musescore.AppImage"
 (cd "$tmp"; ./musescore.AppImage --appimage-extract > /dev/null)
 cp -a "$tmp/squashfs-root/." tools/musescore/
 rm -rf "$tmp"
fi
{
 printf '#!/bin/sh\nexec /usr/bin/xvfb-run -a '
 printf '%q ' "$ROOT/tools/musescore/bin/mscore4portable"
 printf '"$@"\n'
} > tools/bin/mscore
chmod 755 tools/bin/mscore
tools/bin/mscore --version
echo '依次下载模型及乐谱资源……'
HF_HUB_OFFLINE=0 "$PY" sheetstage2/deploy/prepare_models.py
# Environment checks only: no song or transcription request.
"$PY" - <<'PY'
import torch,numpy
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--no-sandbox'])
 page=b.new_page();page.set_content('<p>ok</p>');assert page.inner_text('p')=='ok';b.close()
print('pip check、TorchAudio 导入和 Chromium 检查通过：',torch.__version__,numpy.__version__)
PY
if [[ $USE_CU128 == 1 ]]; then
 "$PY" - <<'PY'
from pathlib import Path
import re,shutil
p=Path('service.env')
backup=Path('service.env.before-cu128')
if not backup.exists(): shutil.copyfile(p,backup)
s=p.read_text()
s=re.sub(r'^export SHEETSTAGE2_MUSCRIPTOR_PYTHON=.*$',
         'export SHEETSTAGE2_MUSCRIPTOR_PYTHON=/usr/bin/python3',s,flags=re.M)
p.write_text(s)
PY
fi
{
 cat <<'WRAPPER'
#!/bin/bash
. /opt/supervisor-scripts/utils/logging.sh
. /opt/supervisor-scripts/utils/environment.sh
set -e
WRAPPER
 printf 'cd %q\n' "$ROOT"
 printf '. %q\n' "$ROOT/service.env"
 printf 'pty %q -m sheetstage2.web --host 127.0.0.1 --port %s 2>&1\n' "$PY" "$PORT"
} > /opt/supervisor-scripts/transcription-sheetsage2.sh
chmod 755 /opt/supervisor-scripts/transcription-sheetsage2.sh
cat > /etc/supervisor/conf.d/transcription-sheetsage2.conf <<'CONF'
[program:transcription-sheetsage2]
environment=PROC_NAME="%(program_name)s"
command=/opt/supervisor-scripts/transcription-sheetsage2.sh
autostart=true
autorestart=unexpected
startsecs=5
stopasgroup=true
killasgroup=true
stopsignal=TERM
stopwaitsecs=30
stdout_logfile=/dev/stdout
redirect_stderr=true
stdout_events_enabled=true
stdout_logfile_maxbytes=0
CONF
supervisorctl reread
supervisorctl update
supervisorctl restart transcription-sheetsage2
"$PY" - <<'PY'
import time,urllib.request
for _ in range(30):
 try:
  for path in ['/healthz','/','/vendor/jsmediatags-3.9.7.min.js']:
   with urllib.request.urlopen('http://127.0.0.1:8791'+path,timeout=3) as r:
    assert r.status==200
  break
 except (OSError,AssertionError):time.sleep(1)
else:raise SystemExit('网站健康检查失败，请查看 /var/log/portal/transcription-sheetsage2.log')
print('安装完成：网站监听 127.0.0.1:8791。未运行歌曲任务。')
PY

#!/usr/bin/env python3
"""Upload this checkout and install on a Vast host using a pasted SSH command."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REMOTE = '/workspace/transcription'


def parse_ssh(command: str) -> list[str]:
    words = shlex.split(command)
    if not words or words.pop(0) != 'ssh':
        raise ValueError('请传入以 ssh 开头的连接命令')
    options, host = [], None
    while words:
        word = words.pop(0)
        if word in ('-L', '-R', '-D'):
            if not words:
                raise ValueError(f'{word} 缺少参数')
            words.pop(0)
        elif word.startswith(('-L', '-R', '-D')):
            continue
        elif word in ('-N', '-f', '-T', '-t'):
            continue
        elif word in ('-p', '-i', '-F', '-J', '-o'):
            if not words:
                raise ValueError(f'{word} 缺少参数')
            value = words.pop(0)
            if word == '-p' and (not value.isdigit() or not 1 <= int(value) <= 65535):
                raise ValueError('SSH 端口无效')
            # Configuration capable of executing commands is deliberately not accepted.
            if word == '-o' and value.split('=', 1)[0].lower() not in (
                'identitiesonly', 'serveraliveinterval', 'connecttimeout',
            ):
                raise ValueError('不支持这个 -o 选项；请通过 ~/.ssh/config 配置连接')
            options += [word, value]
        elif word.startswith('-'):
            raise ValueError(f'不支持 SSH 参数：{word}')
        elif host is None and re.fullmatch(r'[\w.@:\[\]-]+', word):
            host = word
        else:
            raise ValueError('只接受 SSH 连接参数，不接受远端命令')
    if not host:
        raise ValueError('缺少 SSH 主机')
    return ['ssh', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'LogLevel=ERROR', *options, host]


def run(ssh: list[str], command: str, *, stdin=None) -> None:
    subprocess.run([*ssh, command], stdin=stdin, check=True)


def source_filter(info: tarfile.TarInfo):
    if any(p == '__pycache__' or p == '.DS_Store' or p.startswith('._')
           for p in Path(info.name).parts):
        return None
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description='从 Mac 上传并安装 SheetSage2 + MuScriptor')
    parser.add_argument('ssh_command', help='用引号包住整条 SSH 命令')
    parser.add_argument('--use-cu128', action='store_true',
                        help='改用系统 Python 的 PyTorch/TorchAudio 2.8 + CUDA 12.8，保留平台环境')
    parser.add_argument('--hf-token-file', type=Path,
                        default=Path(os.environ.get('HF_HOME', str(Path.home()/'.cache/huggingface'))) / 'token')
    args = parser.parse_args()
    ssh = parse_ssh(args.ssh_command)
    print('检查远端环境……', flush=True)
    # Check package dependencies; missing audio is installed later.
    if args.use_cu128:
        run(ssh, '/usr/bin/python3 --version; nvidia-smi -L')
    else:
        try:
            with (ROOT/'sheetstage2/deploy/select_vast_audio.py').open('rb') as code:
                run(ssh, '/venv/main/bin/python - --preflight', stdin=code)
        except subprocess.CalledProcessError as error:
            if error.returncode != 42:
                raise
            print('TorchAudio 不兼容，自动改用系统 Python 的 CUDA 12.8 配套方案。', flush=True)
            args.use_cu128 = True
    paths = ['sheetstage2/src', 'muscriptor/muscriptor',
             'sheetstage2/pdf-web/dist/index.html', 'sheetstage2/pdf-web/dist/vendor']
    paths += ['sheetstage2/deploy/'+name for name in (
        'install-vast.sh', 'select_vast_audio.py', 'prepare_models.py',
        'requirements-vast-cu130.txt',
    )]
    for path in paths:
        if not (ROOT/path).exists():
            raise ValueError('缺少本地源码：'+path)
    token = args.hf_token_file.expanduser()
    if not token.is_file():
        run(ssh, f'test -s {REMOTE}/cache/huggingface/token || '
            '{ echo "本机和远端均未找到 HF token，请用 --hf-token-file 指定有模型访问权限的 token 文件。" >&2; exit 1; }')
    print('打包并上传当前本地源码（包含未提交改动）……', flush=True)
    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            for path in paths:
                tar.add(ROOT/path, arcname=path, filter=source_filter)
        archive.seek(0)
        run(ssh, f'mkdir -p {REMOTE}; tar -xzf - -C {REMOTE}', stdin=archive)
    if token.is_file():
        print('安全上传 HF 凭据（不输出内容）……', flush=True)
        with token.open('rb') as secret:
            run(ssh, f'umask 077; mkdir -p {REMOTE}/cache/huggingface; '
                f'cat > {REMOTE}/cache/huggingface/token.new && '
                f'chmod 600 {REMOTE}/cache/huggingface/token.new && '
                f'mv {REMOTE}/cache/huggingface/token.new {REMOTE}/cache/huggingface/token', stdin=secret)
    print('安装依赖、下载模型并启动服务……', flush=True)
    option = ' --use-cu128' if args.use_cu128 else ''
    try:
        run(ssh, f'bash {REMOTE}/sheetstage2/deploy/install-vast.sh{option}')
    except subprocess.CalledProcessError as error:
        if error.returncode != 42 or args.use_cu128:
            raise
        print('安装后 TorchAudio 不兼容，自动改用系统 Python 的 CUDA 12.8 配套方案。', flush=True)
        run(ssh, f'bash {REMOTE}/sheetstage2/deploy/install-vast.sh --use-cu128')
    tunnel = shlex.join([*ssh[:-1], '-N', '-L', '8080:127.0.0.1:8791', ssh[-1]])
    print('\n安装完成。关闭原有 8080 隧道后运行：\n'+tunnel+'\n浏览器访问 http://localhost:8080')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print('部署失败：'+str(error), file=sys.stderr)
        sys.exit(1)

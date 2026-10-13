"""Check package dependencies and actual TorchAudio binary loading."""
import argparse
import importlib
import importlib.metadata
import subprocess
import sys


def validate_cuda(cuda: str | None) -> None:
    if cuda is None or tuple(map(int, cuda.split('.')[:2])) < (12, 8):
        raise ValueError('本项目部署要求 PyTorch CUDA 运行库至少为 12.8')


class AudioCompatibilityError(RuntimeError):
    pass


def check_audio() -> None:
    try:
        importlib.import_module('torchaudio')
    except (ImportError, OSError, RuntimeError) as error:
        raise AudioCompatibilityError(str(error)) from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    import torch
    validate_cuda(torch.version.cuda)
    if args.install:
        try:
            version = importlib.metadata.version('torchaudio')
        except importlib.metadata.PackageNotFoundError:
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir',
                            '--no-deps', 'torchaudio'], check=True, stdout=sys.stderr)
            version = importlib.metadata.version('torchaudio')
        check_audio()
        print(version)
    else:
        try:
            importlib.metadata.version('torchaudio')
        except importlib.metadata.PackageNotFoundError:
            print('尚未安装 TorchAudio，将在安装阶段检查实际导入。', file=sys.stderr)
        else:
            check_audio()
        subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)


if __name__ == '__main__':
    try:
        main()
    except AudioCompatibilityError as error:
        print(f'TorchAudio 加载失败：{error}', file=sys.stderr)
        sys.exit(42)
    except Exception as error:
        sys.exit(f'依赖检查失败：{type(error).__name__}: {error}')

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'deploy_from_mac', Path(__file__).parents[1]/'deploy/deploy-from-mac.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('command', [
    'ssh -p 60670 root@1.208.108.242 -L 8080:localhost:8080',
    'ssh -L8080:localhost:8080 -p 60670 root@1.208.108.242',
    'ssh -N -p 60670 -L 8080:localhost:8080 root@1.208.108.242',
])
def test_forwarding_removed(command):
    assert module.parse_ssh(command)==[
        'ssh','-o','StrictHostKeyChecking=accept-new',
        '-o','LogLevel=ERROR',
        '-p','60670','root@1.208.108.242',
    ]


@pytest.mark.parametrize('command', [
    'ssh -p 99999 host', 'ssh host reboot', 'ssh host;reboot',
    'ssh -L', 'ssh -o ProxyCommand=evil host', 'scp host',
])
def test_invalid_command_rejected(command):
    with pytest.raises(ValueError):
        module.parse_ssh(command)


def test_cu128_flag_bypasses_broken_audio_and_reaches_installer(monkeypatch, tmp_path):
    import sys
    token=tmp_path/'token';token.write_text('test-credential')
    commands=[]
    monkeypatch.setattr(module,'run',lambda ssh,command,**kwargs:commands.append(command))
    monkeypatch.setattr(sys,'argv',['deploy','--use-cu128','--hf-token-file',str(token),
                                  'ssh -p 16395 root@109.250.11.152'])
    module.main()
    assert '--preflight' not in '\n'.join(commands)
    assert commands[-1].endswith('install-vast.sh --use-cu128')
    assert 'test-credential' not in '\n'.join(commands)


def test_connection_failure_does_not_trigger_reinstall(monkeypatch):
    import sys
    import subprocess
    commands=[]
    def fake_run(ssh,command,**kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(255,['ssh',command])
    monkeypatch.setattr(module,'run',fake_run)
    monkeypatch.setattr(sys,'argv',['deploy','ssh -p 16395 root@109.250.11.152'])
    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    assert len(commands)==1


@pytest.mark.parametrize('stage', ['preflight', 'install'])
def test_audio_failure_automatically_retries_cu128(monkeypatch, tmp_path, stage):
    import sys
    import subprocess
    token = tmp_path/'token'
    token.write_text('test-credential')
    commands = []
    def fake_run(ssh, command, **kwargs):
        commands.append(command)
        if (stage == 'preflight' and '--preflight' in command) or (
            stage == 'install' and command.endswith('install-vast.sh')
        ):
            raise subprocess.CalledProcessError(42, ['ssh', command])
    monkeypatch.setattr(module, 'run', fake_run)
    monkeypatch.setattr(sys, 'argv', ['deploy', '--hf-token-file', str(token), 'ssh host'])
    module.main()
    assert commands[-1].endswith('install-vast.sh --use-cu128')
    assert sum(command.endswith('--use-cu128') for command in commands) == 1

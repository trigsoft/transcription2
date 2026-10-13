"""Exercise the bootstrap with local source and a stub installer, never a server."""
import io
import os
from pathlib import Path
import subprocess
import tarfile

import pytest


@pytest.mark.parametrize('exists', [True, False])
def test_python_guard_does_not_require_late_generated_agent_guide(tmp_path, exists):
    installer = Path(__file__).parents[1] / 'deploy/install-vast.sh'
    guard = next(line for line in installer.read_text().splitlines() if '$PY ]]' in line)
    python = tmp_path / 'python'
    if exists:
        python.write_text('#!/bin/sh\nexit 0\n')
        python.chmod(0o755)
    result = subprocess.run(['bash', '-c', guard],
                            env=dict(os.environ, PY=str(python)), capture_output=True)
    assert result.returncode == (0 if exists else 1)


@pytest.mark.parametrize('status', [0, 42, 7])
def test_provision_retries_only_audio_and_marks_only_success(tmp_path, status):
    script = Path(__file__).parents[1] / 'deploy/provision-vast.sh'
    archive = tmp_path / 'source.tar.gz'
    installer = b'''#!/bin/bash
echo "$*" >> "$CALLS"
if [[ ${1:-} == --use-cu128 ]]; then exit 0; fi
exit "$INSTALL_STATUS"
'''
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('source/sheetstage2/deploy/install-vast.sh')
        info.size = len(installer)
        tar.addfile(info, io.BytesIO(installer))
    commands = tmp_path / 'bin'
    commands.mkdir()
    curl = commands / 'curl'
    curl.write_text('#!/bin/bash\ncp "$ARCHIVE" "${@: -1}"\n')
    curl.chmod(0o755)
    # flock is absent on macOS; locking itself is supplied by util-linux on Vast.
    flock = commands / 'flock'
    flock.write_text('#!/bin/bash\nexit 0\n')
    flock.chmod(0o755)
    calls = tmp_path / 'calls'
    env = dict(os.environ, PATH=str(commands)+':'+os.environ['PATH'],
               WORKSPACE=str(tmp_path / 'workspace'), ARCHIVE=str(archive),
               CALLS=str(calls), INSTALL_STATUS=str(status))
    # Omit the root guard only in the isolated test copy.
    local_script = tmp_path / 'provision.sh'
    local_script.write_text('\n'.join(line for line in script.read_text().splitlines()
                                     if not line.startswith('[[ $EUID'))+'\n')
    result = subprocess.run(['bash', str(local_script)], env=env, capture_output=True)
    assert result.returncode == (7 if status == 7 else 0)
    assert calls.read_text().splitlines() == (['', '--use-cu128'] if status == 42 else [''])
    marker = tmp_path / 'workspace/transcription/deploy/vast-provisioned'
    assert marker.exists() == (status != 7)
    if marker.exists():
        again = subprocess.run(['bash', str(local_script)], env=env, capture_output=True)
        assert again.returncode == 0
        assert len(calls.read_text().splitlines()) == (2 if status == 42 else 1)

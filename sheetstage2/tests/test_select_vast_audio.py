import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'select_vast_audio', Path(__file__).parents[1] / 'deploy/select_vast_audio.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('cuda', ['12.8', '12.9', '13.0', '13.2'])
def test_cuda_minimum_does_not_reject_new_versions(cuda):
    module.validate_cuda(cuda)


@pytest.mark.parametrize('cuda', [None, '11.8', '12.6'])
def test_old_cuda_rejected(cuda):
    with pytest.raises(ValueError):
        module.validate_cuda(cuda)


@pytest.mark.parametrize("broken", [False, True])
def test_preflight_checks_actual_audio_import(monkeypatch, broken):
    from types import SimpleNamespace
    import sys
    calls=[]
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(version=SimpleNamespace(cuda='13.2')))
    monkeypatch.setattr(sys,'argv',['check','--preflight'])
    monkeypatch.setattr(module.subprocess,'run',lambda command,**kwargs:calls.append(command))
    monkeypatch.setattr(module.importlib.metadata,'version',lambda name:'2.11.0')
    imports=[]
    def load(name):
        imports.append(name)
        if broken:
            raise RuntimeError('CUDA versions differ')
    monkeypatch.setattr(module.importlib,'import_module',load)
    if broken:
        with pytest.raises(RuntimeError,match='CUDA versions differ'):
            module.main()
    else:
        module.main()
    assert imports==['torchaudio']
    assert calls==([] if broken else [[sys.executable,'-m','pip','check']])

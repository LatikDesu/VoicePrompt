"""CUDA wheels resolve only for explicitly selected GPU processes."""

import os

import pytest

from voiceprompt.cuda_runtime import spawn_environment


def test_cpu_spawn_does_not_discover_or_change_cuda_paths(monkeypatch):
    monkeypatch.setenv('LD_LIBRARY_PATH', '/existing')
    monkeypatch.setattr('voiceprompt.cuda_runtime.library_directories',
                        lambda: (_ for _ in ()).throw(AssertionError('CPU must not inspect NVIDIA packages')))
    with spawn_environment('cpu'):
        assert os.environ['LD_LIBRARY_PATH'] == '/existing'
    assert os.environ['LD_LIBRARY_PATH'] == '/existing'


def test_cuda_paths_apply_to_child_spawn_and_parent_is_restored_on_failure(monkeypatch):
    monkeypatch.setenv('LD_LIBRARY_PATH', '/existing')
    monkeypatch.setattr('voiceprompt.cuda_runtime.library_directories', lambda: ['/isolated/cublas', '/isolated/cudnn'])
    with pytest.raises(RuntimeError):
        with spawn_environment('cuda'):
            assert os.environ['LD_LIBRARY_PATH'] == '/isolated/cublas:/isolated/cudnn:/existing'
            raise RuntimeError('spawn failed')
    assert os.environ['LD_LIBRARY_PATH'] == '/existing'


def test_cuda_spawn_restores_absent_parent_variable(monkeypatch):
    monkeypatch.delenv('LD_LIBRARY_PATH', raising=False)
    monkeypatch.setattr('voiceprompt.cuda_runtime.library_directories', lambda: ['/isolated/cublas'])
    with spawn_environment('cuda'):
        assert os.environ['LD_LIBRARY_PATH'] == '/isolated/cublas'
    assert 'LD_LIBRARY_PATH' not in os.environ

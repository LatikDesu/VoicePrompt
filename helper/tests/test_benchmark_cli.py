import subprocess
import sys


def test_missing_corpus_refuses_benchmark_before_loading_model(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'voiceprompt.cli', 'benchmark',
                             '--corpus', str(tmp_path / 'absent.json'), '--profile', 'cpu-small',
                             '--model', str(tmp_path / 'also-missing'),
                             '--output', str(tmp_path / 'report.json')],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'corpus_unavailable' in result.stderr
    assert not (tmp_path / 'report.json').exists()

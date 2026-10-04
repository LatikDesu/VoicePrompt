from voiceprompt.benchmark import edit_distance, critical_preservation


def test_negation_and_number_loss_is_visible():
    assert edit_distance(['не', 'удаляй', '42', 'файла'], ['удаляй', '43', 'файла']) == 2
    assert critical_preservation('не удаляй 42 файла', 'удаляй 43 файла', ['не', '42']) == {'не': False, '42': False}


def test_critical_words_do_not_match_substrings():
    assert critical_preservation('не 42', 'нет 142', ['не', '42']) == {'не': False, '42': False}


def test_critical_paths_are_literal_not_normalized():
    tokens = ['/home/user/MyFile.py', '--no-cache']
    assert critical_preservation(' '.join(tokens), '/home/user/myfile.py --cache', tokens) == {tokens[0]: False, tokens[1]: False}
    assert critical_preservation(' '.join(tokens), ' '.join(tokens), tokens) == {tokens[0]: True, tokens[1]: True}


def test_edit_distance_handles_empty_and_repeated_words():
    assert edit_distance([], ['a', 'b']) == 2
    assert edit_distance(['a', 'a'], ['a']) == 1


def test_benchmark_missing_audio_cannot_fabricate_metrics(tmp_path):
    import json
    import pytest
    from voiceprompt.benchmark import BenchmarkError, load_corpus

    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"entries": [{"id": "quiet-01", "split": "held_out",
        "audio_path": "missing.wav", "reference_text": "не удаляй",
        "critical_tokens": ["не"], "language": "ru"}]}))
    with pytest.raises(BenchmarkError, match="audio_missing"):
        load_corpus(corpus)


def test_benchmark_rejects_duplicate_ids_before_decoding(tmp_path):
    import json
    import pytest
    from voiceprompt.benchmark import BenchmarkError, load_corpus

    (tmp_path / "silence.wav").write_bytes(b"not decoded yet")
    example = {"id": "quiet-01", "split": "held_out", "audio_path": "silence.wav",
        "reference_text": "не удаляй", "critical_tokens": ["не"], "language": "ru"}
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"entries": [example, example]}))
    with pytest.raises(BenchmarkError, match="corpus_duplicate_id"):
        load_corpus(corpus)

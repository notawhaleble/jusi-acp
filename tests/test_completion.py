from pathlib import Path

import pytest

from jusi_acp.completion import PathCompleter


@pytest.mark.parametrize('marker', ['@', '@./', './'])
def test_path_completion_browses_one_directory_level(tmp_path: Path, marker: str) -> None:
    (tmp_path / 'src' / 'nested').mkdir(parents=True)
    (tmp_path / 'sample.py').write_text('root')
    (tmp_path / 'src' / 'sample.py').write_text('child')
    (tmp_path / 'src' / 'nested' / 'sample.py').write_text('grandchild')
    completer = PathCompleter((tmp_path,))

    root = completer.complete(marker, len(marker))
    assert {item['text'] for item in root} == {marker + 'sample.py', marker + 'src/'}
    # Apply the directory suggestion, then complete again to browse its children.
    directory = next(item['text'] for item in root if item['label'] == 'src/')
    children = completer.complete(directory, len(directory))
    assert {item['text'] for item in children} == {
        marker + 'src/sample.py', marker + 'src/nested/',
    }
    nested = marker + 'src/nested/'
    assert [item['text'] for item in completer.complete(nested, len(nested))] == [
        nested + 'sample.py',
    ]
    # A filename query cannot reach down into directories that haven't been selected.
    query = marker + 'sam'
    assert [item['text'] for item in completer.complete(query, len(query))] == [
        marker + 'sample.py',
    ]


def test_directory_completion_preserves_escaped_spaces(tmp_path: Path) -> None:
    (tmp_path / 'my source' / 'nested').mkdir(parents=True)
    (tmp_path / 'my source' / 'α file.py').write_text('child')
    (tmp_path / 'my source' / 'nested' / 'deep.py').write_text('grandchild')
    completer = PathCompleter((tmp_path,))
    directory = completer.complete('@my', 3)[0]['text']
    assert directory == '@my\\ source/'
    assert {item['text'] for item in completer.complete(directory, len(directory))} == {
        '@my\\ source/nested/', '@my\\ source/α\\ file.py',
    }

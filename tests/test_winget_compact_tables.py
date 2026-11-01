"""Compact native WinGet tables retain exact source and package constraints."""
import pytest

from optimus.maintenance import Maintenance


GIT_HEADER = 'Name Id      Version  Available'
GIT_ROW = 'Git  Git.Git 2.56.0.3 2.56.0.4'


def table(header=GIT_HEADER, rows=(GIT_ROW,)):
    return header + '\n' + '-' * max(len(header), *(len(row) for row in rows)) + '\n' + '\n'.join(rows) + '\n'


def test_compact_git_update_requires_source_scoped_exact_version_pair():
    rows, recognized, excluded = Maintenance._parse_update_table(table(), source_context='winget')
    assert recognized is True and excluded == []
    assert rows == [{'name': 'Git', 'id': 'Git.Git', 'package_id': 'Git.Git',
        'version': '2.56.0.3', 'available': '2.56.0.4', 'source': 'winget'}]


def test_single_separator_is_valid_between_all_four_header_labels():
    output = table('Name Id Version Available', ('App  A1 1       2',))
    rows, recognized, excluded = Maintenance._parse_update_table(output, source_context='winget')
    assert recognized is True and excluded == []
    assert rows == [{'name': 'App', 'id': 'A1', 'package_id': 'A1',
        'version': '1', 'available': '2', 'source': 'winget'}]


@pytest.mark.parametrize('context', [None, 'msstore', 'Winget', ''])
def test_compact_four_column_table_does_not_infer_unproven_source(context):
    rows, recognized, excluded = Maintenance._parse_update_table(table(), source_context=context)
    assert rows == [] and recognized is False and excluded


@pytest.mark.parametrize('header', ['NameId      Version  Available', 'Name IdVersion  Available',
    'Name Id      VersionAvailable', 'Name\tId      Version  Available',
    'Name Id      Available Version', 'Name Id      Version  Available Extra'])
def test_compact_update_rejects_joined_reordered_or_extra_headers(header):
    rows, recognized, excluded = Maintenance._parse_update_table(table(header), source_context='winget')
    assert rows == [] and recognized is False and excluded


def test_compact_update_rejects_repeated_header():
    rows, recognized, excluded = Maintenance._parse_update_table(table() + table(), source_context='winget')
    assert rows == [] and recognized is False and excluded


@pytest.mark.parametrize('second', [GIT_ROW, GIT_ROW.replace('Git.Git', 'git.git')])
def test_compact_update_excludes_ambiguous_repeated_identity(second):
    rows, recognized, excluded = Maintenance._parse_update_table(table(rows=(GIT_ROW, second)), source_context='winget')
    assert rows == [] and recognized is True
    assert 'Ambiguous repeated package IDs were excluded.' in excluded


@pytest.mark.parametrize('row', [GIT_ROW.replace('Git.Git', 'Git.Gi\u2026'),
    GIT_ROW.replace('Git.Git', 'Git... '), GIT_ROW.replace('2.56.0.3', 'Unknown '),
    GIT_ROW.replace('2.56.0.4', '2.56.0.3'), GIT_ROW.replace('2.56.0.4', '2.56.0.2')])
def test_compact_update_excludes_truncated_unknown_or_nonforward_rows(row):
    rows, recognized, excluded = Maintenance._parse_update_table(table(rows=(row,)), source_context='winget')
    assert rows == [] and recognized is True and excluded


def test_explicit_source_column_overrides_narrow_caller_context():
    header, row = GIT_HEADER + ' Source', GIT_ROW + ' ' * 3 + 'msstore'
    rows, recognized, excluded = Maintenance._parse_update_table(table(header, (row,)), source_context='winget')
    assert rows == [] and recognized is True
    assert 'A row without the exact winget source was excluded.' in excluded

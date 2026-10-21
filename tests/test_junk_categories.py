"""Optional junk definitions are allowlisted, distinct and never user documents."""
from optimus.cleaner import Cleaner


def test_new_junk_categories_are_optional_narrow_locations(tmp_path):
    definitions={item.id:item for item in Cleaner(tmp_path)._definitions()}
    expected={'user-error-archive','user-error-queue','windows-minidumps','nvidia-dx-cache','nvidia-gl-cache','amd-dx-cache','amd-dxc-cache'}
    assert expected <= definitions.keys()
    assert definitions['windows-minidumps'].suffixes == ('.dmp',)
    assert definitions['windows-minidumps'].requires_admin is True
    assert definitions['nvidia-dx-cache'].path.parts[-2:] == ('NVIDIA','DXCache')
    assert 'stutter' in definitions['amd-dx-cache'].description
    assert all(definitions[identity].path.name not in ('Documents','Downloads','Desktop') for identity in expected)

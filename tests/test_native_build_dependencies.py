"""Build dependency policies use mocked commands; never install host packages."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('optimus_build_dependencies', ROOT / 'tools/install_build_dependencies.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


@pytest.mark.parametrize('system,machine', [('win32','AMD64'),('linux','x86_64'),('darwin','arm64')])
def test_other_targets_require_published_crypto_and_cffi_wheels(system, machine):
    command = helper.install_command(Path('requirements-build.txt'), system=system,machine=machine,executable='fixture-python')
    assert command[:4] == ['fixture-python','-m','pip','install']
    assert '--only-binary=cryptography,cffi' in command
    assert not any(arg.startswith('--no-binary') for arg in command)
    assert command[-2:] == ['-r','requirements-build.txt']
    runner = Mock(side_effect=AssertionError('No Homebrew call for other targets'))
    saved = {'FIXTURE':'unchanged'}
    assert helper.install_environment(system=system,machine=machine,environment=saved,runner=runner) == saved
    runner.assert_not_called()


@pytest.mark.parametrize('machine', ['x86_64','AMD64'])
def test_intel_macos_forces_uncached_source_crypto_and_binary_cffi(machine):
    command = helper.install_command('requirements-build.txt',system='darwin',machine=machine)
    assert '--no-cache-dir' in command and '--force-reinstall' in command
    assert '--no-binary=cryptography' in command and '--only-binary=cffi' in command
    assert command[-2:] == ['-r','requirements-build.txt']


def openssl(tmp_path):
    root=tmp_path/'Homebrew-OpenSSL'
    for relative in ('lib/libssl.a','lib/libcrypto.a','include/openssl/ssl.h'):
        path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
    return root


def test_static_install_binds_all_openssl_paths_to_exact_homebrew_prefix(tmp_path):
    root=openssl(tmp_path)
    runner=Mock(return_value=SimpleNamespace(stdout=str(root)+'\n'))
    original={'OPENSSL_STATIC':'0','OPENSSL_DIR':'wrong','OPENSSL_LIB_DIR':'wrong','OPENSSL_INCLUDE_DIR':'wrong'}
    env=helper.install_environment(system='darwin',machine='x86_64',environment=original,
        runner=runner,brew='/fixture/brew')
    assert env['OPENSSL_STATIC']=='1' and env['OPENSSL_DIR']==str(root.resolve())
    assert env['OPENSSL_LIB_DIR']==str(root.resolve()/'lib')
    assert env['OPENSSL_INCLUDE_DIR']==str(root.resolve()/'include')
    assert original['OPENSSL_STATIC']=='0'
    assert runner.call_args.args[0]==['/fixture/brew','--prefix','openssl@3']
    assert runner.call_args.kwargs['check'] is True


@pytest.mark.parametrize('missing',['lib/libssl.a','lib/libcrypto.a','include/openssl/ssl.h'])
def test_missing_static_archives_or_headers_fail_before_pip(tmp_path,missing):
    root=openssl(tmp_path);(root/missing).unlink()
    runner=Mock(return_value=SimpleNamespace(stdout=str(root)))
    with pytest.raises(RuntimeError,match='static archives'):
        helper.install_environment(system='darwin',machine='x86_64',runner=runner,brew='/fixture/brew')


@pytest.mark.parametrize('output',['relative/prefix','/prefix\n/other-prefix',''])
def test_untrusted_homebrew_prefix_output_is_rejected(output):
    with pytest.raises(RuntimeError,match='invalid OpenSSL prefix'):
        helper.install_environment(system='darwin',machine='x86_64',brew='/fixture/brew',
            runner=Mock(return_value=SimpleNamespace(stdout=output)))


def binding(tmp_path):
    path=tmp_path/'_rust.abi3.so';path.write_bytes(b'fixture Mach-O')
    return path


@pytest.mark.parametrize('library',['/usr/local/opt/openssl@3/lib/libssl.3.dylib',
    '@rpath/libcrypto.3.dylib','@loader_path/libssl.dylib','libcrypto.dylib'])
def test_external_ssl_or_crypto_dependency_is_a_packaging_failure(tmp_path,library):
    path=binding(tmp_path)
    output=f'{path}:\n\t{library} (compatibility version 3.0.0, current version 3.6.0)\n'
    with pytest.raises(RuntimeError,match='still uses external OpenSSL'):
        helper.verify_static_openssl(system='darwin',module_path=path,
            runner=Mock(return_value=SimpleNamespace(stdout=output)))


def test_static_binding_with_system_runtime_dependencies_passes(tmp_path):
    path=binding(tmp_path)
    output=(f'{path}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n'
            '\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 1800.1.0)\n')
    runner=Mock(return_value=SimpleNamespace(stdout=output))
    result=helper.verify_static_openssl(system='darwin',module_path=path,runner=runner)
    assert result=={'status':'passed','crypto_openssl_linkage':'static','binding':'_rust.abi3.so'}
    assert runner.call_args.args[0]==['/usr/bin/otool','-L',str(path.resolve())]
    assert runner.call_args.kwargs['check'] is True


@pytest.mark.parametrize('output',['','unrecognized output','module:\n', 'x'*65537],
                         ids=['empty','unrecognized','header-only','oversized'])
def test_missing_or_malformed_otool_result_never_claims_static_linkage(tmp_path,output):
    with pytest.raises(RuntimeError,match='linkage inspection'):
        helper.verify_static_openssl(system='darwin',module_path=binding(tmp_path),
            runner=Mock(return_value=SimpleNamespace(stdout=output)))


def test_non_macos_does_not_probe_native_linkage():
    runner=Mock(side_effect=AssertionError('No macOS tool on this platform'))
    assert helper.verify_static_openssl(system='linux',runner=runner)['status']=='not_applicable'
    runner.assert_not_called()


def test_main_runs_pinned_install_then_checks_linkage_without_touching_host_packages(tmp_path,monkeypatch,capsys):
    requirements=tmp_path/'requirements-build.txt';requirements.write_text('cryptography==50.0.2\ncffi==2.1.0\n')
    install=Mock();verify=Mock(return_value={'status':'passed','crypto_openssl_linkage':'static'})
    monkeypatch.setattr(helper.subprocess,'run',install)
    monkeypatch.setattr(helper,'install_environment',lambda:{'OPENSSL_STATIC':'1'})
    monkeypatch.setattr(helper,'verify_static_openssl',verify)
    assert helper.main(['--requirements',str(requirements)])==0
    assert install.call_args.args[0][-2:]==['-r',str(requirements.resolve())]
    assert install.call_args.kwargs['check'] is True
    assert install.call_args.kwargs['env']=={'OPENSSL_STATIC':'1'}
    verify.assert_called_once_with()
    assert 'dependency_install' in capsys.readouterr().out


def test_ci_checks_linkage_before_tests_and_preserves_frozen_crypto_selftest():
    workflow=(ROOT/'.github/workflows/windows.yml').read_text(encoding='utf-8')
    assert workflow.index('python tools/install_build_dependencies.py') < workflow.index('python tools/test_native.py')
    assert workflow.index('python tools/test_native.py') < workflow.index('python tools/build_native.py')
    assert 'cryptography==50.0.2' in (ROOT/'requirements-build.txt').read_text()
    source=(ROOT/'src/optimus/app.py').read_text(encoding='utf-8')
    assert 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM' in source
    assert 'from cryptography.hazmat.primitives.kdf.scrypt import Scrypt' in source
    assert 'bundled file-utility cryptography self-test failed' in source

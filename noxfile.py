from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import nox

nox.options.envdir = os.environ.get('NOX_ENVDIR')
nox.options.sessions = ['format', 'lint', 'test', 'type_check']


def mirror_repo(session: nox.Session):
    if not os.environ.get('CI'):
        repo_dir = f'{session.create_tmp()}/instawow'
        session.run('git', 'clone', '.', repo_dir, external=True)
        session.chdir(repo_dir)


def install_coverage_hook(session: nox.Session):
    session.run(
        'python',
        '-c',
        """\
from pathlib import Path
import sysconfig

(Path(sysconfig.get_path('purelib')) / 'coverage.pth').write_text(
    'import coverage; coverage.process_startup()',
    encoding='utf-8',
)
""",
    )


@nox.session(name='format', reuse_venv=True)
def format_(session: nox.Session):
    "Format source code."
    session.install('-U', 'ruff')

    check = '--check' in session.posargs
    session.run('ruff', '--select', 'I', *[] if check else ['--fix'], '.')
    session.run('ruff', 'format', *['--check'] if check else [], '.')

    if '--skip-prettier' not in session.posargs:
        with session.chdir('instawow-gui/frontend'):
            session.run('npm', 'install', external=True)
            session.run(
                'npx',
                'prettier',
                '--check' if check else '--write',
                '.',
                external=True,
            )


@nox.session(reuse_venv=True)
def lint(session: nox.Session):
    "Lint source code."
    session.install('-U', 'ruff')
    session.run('ruff', '--output-format', 'full', *session.posargs, '.')
    session.notify('format', ['--check'])


@nox.session(python='3.11')
@nox.parametrize(
    'constraints',
    [
        '',
        dedent(
            """\
            aiohttp == 3.9.3
            aiohttp-client-cache == 0.9.1
            alembic == 1.12.0
            anyio == 3.6.2
            attrs == 23.1.0
            cattrs == 23.1.2
            click == 8.1.0
            diskcache == 5.6.3
            iso8601 == 1.0.2
            loguru == 0.7.2
            packaging == 23.0
            pluggy == 1.3.0
            prompt-toolkit == 3.0.29
            questionary == 1.10.0
            rapidfuzz == 3.3.0
            sqlalchemy == 2.0.21
            truststore == 0.7.0
            typing-extensions == 4.3.0
            yarl == 1.9.2
            aiohttp-rpc == 1.0.0
            """
        ),
    ],
    [
        'latest',
        'minimum-versions',
    ],
)
def test(session: nox.Session, constraints: str):
    "Run the test suite."
    mirror_repo(session)

    constraints_txt = 'constraints.txt'
    Path(constraints_txt).write_text(constraints)

    if session.posargs:
        (package_path,) = session.posargs
    else:
        package_path = '.'

    session.install('-c', constraints_txt, f'{package_path}[gui, test]', './tests/plugin')
    install_coverage_hook(session)

    session.run(
        *'coverage run -m pytest -n auto'.split(),
        env={'COVERAGE_PROCESS_START': 'pyproject.toml'},
    )


@nox.session
def produce_coverage_report(session: nox.Session):
    "Produce coverage report."
    session.install('coverage[toml]')
    session.run('coverage', 'combine')
    session.run('coverage', 'html', '--skip-empty')
    session.run('coverage', 'report', '-m')


@nox.session(python='3.11')
def type_check(session: nox.Session):
    "Run Pyright."
    mirror_repo(session)

    if session.posargs:
        (package_path,) = session.posargs
    else:
        package_path = '.'

    session.install(f'{package_path}[gui]')
    session.run('npx', 'pyright', external=True)


@nox.session(python=False)
def bundle_frontend(session: nox.Session):
    "Bundle the frontend."
    with session.chdir('instawow-gui/frontend'):
        session.run('git', 'clean', '-fX', '../src/instawow_gui/frontend', external=True)
        session.run('npm', 'install', external=True)
        session.run('npx', 'svelte-check', external=True)
        session.run('npm', 'run', 'build', external=True)


@nox.session(python='3.11')
def build_dists(session: nox.Session):
    "Build an sdist and wheel."
    session.run('git', 'clean', '-fdX', 'dist', external=True)
    session.install('build')
    session.run('python', '-m', 'build')


@nox.session
def publish_dists(session: nox.Session):
    "Validate and upload dists to PyPI."
    session.install('twine')
    session.run('twine', 'check', '--strict', 'dist/*')
    session.run('twine', 'upload', '--verbose', 'dist/*')


@nox.session(python=False)
def freeze_cli(session: nox.Session):
    import argparse
    import shutil
    import tempfile

    PYAPP_VERSION = 'v0.12.0'

    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel-file', required=True)
    parser.add_argument('--out-dir', required=True)

    options = parser.parse_args(session.posargs)

    pyapp_configuration = {
        'PYAPP_PROJECT_PATH': os.fspath(Path(options.wheel_file).absolute()),
        'PYAPP_EXEC_MODULE': 'instawow',
        'PYAPP_PYTHON_VERSION': '3.11',
        'PYAPP_DISTRIBUTION_EMBED': '1',
        'PYAPP_PIP_EXTERNAL': '1',
        'PYAPP_PIP_EXTRA_ARGS': '--only-binary :all:',
    }

    with tempfile.TemporaryDirectory() as app_temp_dir:
        session.run(
            'cargo',
            'install',
            '--git',
            'https://github.com/ofek/pyapp',
            '--tag',
            PYAPP_VERSION,
            '--force',
            '--root',
            app_temp_dir,
            external=True,
            env=pyapp_configuration,
        )

        for suffix in ['', '.exe']:
            from_path = Path(app_temp_dir, 'bin', 'pyapp').with_suffix(suffix)
            if not from_path.exists():
                continue

            to_path = Path(options.out_dir, 'instawow').with_suffix(suffix)
            to_path.parent.mkdir(parents=True)
            shutil.copy(from_path, to_path)

            print(to_path, end='')
            break


@nox.session(python=False)
def extract_version(session: nox.Session):
    from importlib.metadata import Distribution

    (instawow,) = Distribution.discover(name='instawow', path=list(session.posargs))
    print(instawow.version, end='')


@nox.session(python=False)
def patch_frontend_spec(session: nox.Session):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--version')
    parser.add_argument('--wheel-file')

    options = parser.parse_args(session.posargs)

    spec_path = Path(__file__).parent.joinpath('instawow-gui', 'pyproject.toml')
    spec = spec_path.read_text(encoding='utf-8')

    if options.version:
        spec = spec.replace('version = "0.1.0"', f'version = "{options.version}"')

    if options.wheel_file:
        spec = spec.replace(
            '"instawow[gui]"', f'"instawow[gui] @ {Path(options.wheel_file).resolve().as_uri()}"'
        )

    spec_path.write_text(spec, encoding='utf-8')

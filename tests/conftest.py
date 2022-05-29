from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Any
from zipfile import ZipFile

import aiohttp
from aresponses import ResponsesMockServer
import pytest

from instawow import __version__
from instawow.common import Flavour
from instawow.config import Config, GlobalConfig, SecretStr, _AccessTokens
from instawow.manager import Manager, contextualise, init_web_client

inf = float('inf')

FIXTURES = Path(__file__).parent / 'fixtures'


def pytest_addoption(parser: pytest.Parser):
    parser.addoption('--iw-no-mock-http', action='store_true')


def should_mock(fn: Callable[..., object]):
    import inspect
    import warnings

    def wrapper(request: pytest.FixtureRequest):
        if request.config.getoption('--iw-no-mock-http'):
            warnings.warn('not mocking')
            return None
        elif any(m.name == 'iw_no_mock_http' for m in request.node.iter_markers()):
            return None

        args = (request.getfixturevalue(p) for p in inspect.signature(fn).parameters)
        return fn(*args)

    return wrapper


@lru_cache(maxsize=None)
def load_fixture(filename: str):
    return (FIXTURES / filename).read_bytes()


@lru_cache(maxsize=None)
def load_json_fixture(filename: str):
    return json.loads(load_fixture(filename))


@lru_cache(maxsize=None)
def make_addon_zip(*folders: str):
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as file:
        for folder in folders:
            file.writestr(f'{folder}/{folder}.toc', b'')

    return buffer.getvalue()


@pytest.fixture(scope='session', autouse=True)
def iw_temp_dir(tmp_path_factory: pytest.TempPathFactory):
    temp_dir = tmp_path_factory.mktemp('temp') / 'instawow'
    os.environ['INSTAWOW_TEMP_DIR'] = str(temp_dir)
    return temp_dir


@pytest.fixture
def iw_global_config_values(tmp_path: Path, iw_temp_dir: Path):
    return {
        'temp_dir': iw_temp_dir,
        'config_dir': tmp_path / 'config',
        'access_tokens': _AccessTokens(cfcore=SecretStr('foo')),
    }


@pytest.fixture(params=Flavour)
def iw_config_values(request: Any, tmp_path: Path):
    addons = tmp_path / 'wow' / 'interface' / 'addons'
    addons.mkdir(parents=True)
    return {'profile': '__default__', 'addon_dir': addons, 'game_flavour': request.param}


@pytest.fixture
def iw_config(iw_config_values: dict[str, Any], iw_global_config_values: dict[str, Any]):
    global_config = GlobalConfig(**iw_global_config_values).write()
    return Config(global_config=global_config, **iw_config_values).write()


@pytest.fixture
async def iw_web_client():
    async with init_web_client() as web_client:
        yield web_client


@pytest.fixture
def iw_manager(iw_config: Config, iw_web_client: aiohttp.ClientSession):
    contextualise(web_client=iw_web_client)
    manager, close_db_conn = Manager.from_config(iw_config)
    yield manager
    close_db_conn()


@pytest.fixture(autouse=True)
@should_mock
def mock_aiohttp_requests(aresponses: ResponsesMockServer):
    aresponses.add(
        'pypi.org',
        '/pypi/instawow/json',
        'get',
        {'info': {'version': __version__}},
        repeat=inf,
    )

    aresponses.add(
        'raw.githubusercontent.com',
        '/layday/instawow-data/data/base-catalogue-v5.compact.json',
        'get',
        load_json_fixture('base-catalogue-v5.compact.json'),
        repeat=inf,
    )

    aresponses.add(
        'api.curseforge.com',
        '/v1/mods',
        'post',
        load_json_fixture('curse-addon--all.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.curseforge.com',
        '/v1/mods/20338/files',
        'get',
        load_json_fixture('curse-addon-files.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.curseforge.com',
        re.compile(r'^/v1/mods/20338/files/(\d+)/changelog'),
        'get',
        load_json_fixture('curse-addon-changelog.json'),
        repeat=inf,
    )
    aresponses.add(
        'edge.forgecdn.net',
        aresponses.ANY,
        'get',
        aresponses.Response(body=make_addon_zip('Molinari')),
        repeat=inf,
    )

    aresponses.add(
        'api.mmoui.com',
        '/v3/game/WOW/filelist.json',
        'get',
        load_json_fixture('wowi-filelist.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.mmoui.com',
        re.compile(r'^/v3/game/WOW/filedetails/'),
        'get',
        load_json_fixture('wowi-filedetails.json'),
        repeat=inf,
    )
    aresponses.add(
        'cdn.wowinterface.com',
        aresponses.ANY,
        'get',
        aresponses.Response(body=make_addon_zip('Molinari')),
        repeat=inf,
    )

    aresponses.add(
        'www.tukui.org',
        '/api.php?ui=tukui',
        'get',
        load_json_fixture('tukui-ui--tukui.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        '/api.php?ui=elvui',
        'get',
        load_json_fixture('tukui-ui--elvui.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        '/api.php?addons=all',
        'get',
        load_json_fixture('tukui-retail-addons.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        '/api.php?classic-addons=all',
        'get',
        load_json_fixture('tukui-classic-addons.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        '/api.php?classic-tbc-addons=all',
        'get',
        load_json_fixture('tukui-classic-tbc-addons.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        '/api.php',
        'get',
        '',
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        re.compile(r'^/downloads/tukui'),
        'get',
        aresponses.Response(body=make_addon_zip('Tukui')),
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        '/addons.php?download=1',
        'get',
        aresponses.Response(body=make_addon_zip('ElvUI_MerathilisUI')),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'www.tukui.org',
        re.compile(r'/classic-(?:tbc-)?addons\.php\?download=1'),
        'get',
        aresponses.Response(body=make_addon_zip('Tukui')),
        match_querystring=True,
        repeat=inf,
    )

    aresponses.add(
        'api.github.com',
        '/repos/nebularg/PackagerTest',
        'get',
        load_json_fixture('github-repo-release-json.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        '/repos/nebularg/PackagerTest/releases?per_page=10',
        'get',
        load_json_fixture('github-release-release-json.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'github.com',
        '/nebularg/PackagerTest/releases/download/v1.9.7/release.json',
        'get',
        load_json_fixture('github-release-release-json-release-json.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        re.compile(r'^/repos/p3lim-wow/Molinari$', re.IGNORECASE),
        'get',
        load_json_fixture('github-repo-molinari.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        re.compile(r'^/repos/p3lim-wow/Molinari/releases\?per_page=10$', re.IGNORECASE),
        'get',
        load_json_fixture('github-release-molinari.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'github.com',
        re.compile(
            fr'^/{re.escape("p3lim-wow/Molinari/releases/download/90200.82-Release/release.json")}$',
            re.IGNORECASE,
        ),
        'get',
        load_json_fixture('github-release-molinari-release-json.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        '/repos/ketho-wow/RaidFadeMore',
        'get',
        load_json_fixture('github-repo-no-release-json.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        '/repos/ketho-wow/RaidFadeMore/releases?per_page=10',
        'get',
        load_json_fixture('github-release-no-release-json.json'),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        '/repos/AdiAddons/AdiBags',
        'get',
        load_json_fixture('github-repo-no-releases.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        '/repos/AdiAddons/AdiBags/releases?per_page=10',
        'get',
        aresponses.Response(body=b'', status=404),
        match_querystring=True,
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        '/repos/AdiAddons/AdiButtonAuras/releases/tags/2.0.19',
        'get',
        load_json_fixture('github-release-no-assets.json'),
        repeat=inf,
    )
    aresponses.add(
        'api.github.com',
        '/repos/layday/foobar',
        'get',
        aresponses.Response(body=b'', status=404),
        repeat=inf,
    )
    aresponses.add(
        'github.com',
        re.compile(r'^(/[^/]*){2}/releases/download'),
        'get',
        aresponses.Response(body=make_addon_zip('Foo')),
        repeat=inf,
    )

    aresponses.add(
        'github.com',
        '/login/device/code',
        'post',
        load_json_fixture('github-oauth-login-device-code.json'),
        repeat=inf,
    )
    aresponses.add(
        'github.com',
        '/login/oauth/access_token',
        'post',
        load_json_fixture('github-oauth-login-access-token.json'),
        repeat=inf,
    )

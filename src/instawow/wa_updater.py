from __future__ import annotations

import json
import typing
from collections.abc import Iterable, Iterator, Sequence
from datetime import timedelta
from functools import partial, reduce
from itertools import chain, product
from typing import Any, Literal

from attrs import frozen
from cattrs import Converter
from loguru import logger
from typing_extensions import NotRequired as N
from typing_extensions import TypeAlias, TypedDict
from yarl import URL

from .http import CACHE_INDEFINITELY, make_generic_progress_ctx
from .manager import Manager
from .utils import StrEnum, bucketise, gather, read_resource_as_text, shasum, time_op
from .utils import run_in_thread as t

_Slug: TypeAlias = str

_CHECK_API_URL = URL('https://data.wago.io/api/check')
_IMPORT_STRING_API_URL = URL('https://data.wago.io/api/raw/encoded')

_aura_converter = Converter()
_aura_converter.register_structure_hook(URL, lambda v, _: URL(v))
_aura_converter.register_unstructure_hook(URL, str)


@frozen(kw_only=True)
class WeakAura:
    id: str
    uid: str
    parent: typing.Union[str, None] = None
    url: URL
    version: int

    @classmethod
    def from_lua_table(cls, lua_table: Any):
        url_string = lua_table.get('url')
        if url_string is not None:
            weakaura = _aura_converter.structure(lua_table, cls)
            if weakaura.url.host == 'wago.io':
                return weakaura


@frozen(kw_only=True)
class Plateroo(WeakAura):
    uid: str = ''

    @classmethod
    def from_lua_table(cls, lua_table: Any):
        return super().from_lua_table({**lua_table, 'id': lua_table['Name']})


@frozen
class WeakAuras:
    api_ep = 'weakauras'
    addon_name = 'WeakAuras'

    root: dict[_Slug, list[WeakAura]]

    @classmethod
    def from_lua_table(cls, lua_table: Any):
        auras = (WeakAura.from_lua_table(t) for t in lua_table['displays'].values())
        sorted_auras = sorted(filter(None, auras), key=lambda a: a.id)
        return cls(bucketise(sorted_auras, key=lambda a: a.url.parts[1]))


@frozen
class Plateroos:
    api_ep = 'plater'
    addon_name = 'Plater'

    root: dict[_Slug, list[Plateroo]]

    @classmethod
    def from_lua_table(cls, lua_table: Any):
        auras = (
            Plateroo.from_lua_table(t)
            for n, p in lua_table['profiles'].items()
            for t in chain(
                ({**p, 'Name': f'__profile_{n}__'},),
                p.get('script_data') or (),
                p.get('hook_data') or (),
            )
        )
        sorted_auras = sorted(filter(None, auras), key=lambda a: a.id)
        return cls({a.url.parts[1]: [a] for a in sorted_auras})


def _merge_auras(auras: Iterable[WeakAuras | Plateroos]):
    return {
        t: t(reduce(lambda a, b: a | b, (i.root for i in a)))
        for t, a in bucketise(auras, key=type['WeakAuras | Plateroos']).items()
    }


class _WagoApiResponse(TypedDict):
    _id: str  # +   # Alphanumeric ID
    name: str  # +  # User-facing name
    slug: str  # +  # Slug if it has one; otherwise same as ``_id``
    url: str
    created: str  # ISO datetime
    modified: str  # ISO datetime
    game: str  # "classic" or xpac, e.g. "bfa"
    username: N[str]  # +  # Author username
    version: int  # +   # Version counter, incremented with every update
    # Semver auto-generated from ``version`` - for presentation only
    versionString: str
    changelog: _WagoApiResponse_Changelog  # +
    forkOf: N[str]  # Only present on forks
    regionType: N[str]  # Only present on WAs


class _WagoApiResponse_Changelog(TypedDict):
    format: N[Literal['bbcode', 'markdown']]
    text: N[str]


class _TocNumber(StrEnum):
    retail = '100000'
    vanilla_classic = '11403'
    classic = '30400'


class WaCompanionBuilder:
    """A WeakAuras Companion port for shellfolk."""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager

        output_folder = self._manager.config.plugin_dir / __name__
        self.addon_zip_path = output_folder / 'WeakAurasCompanion.zip'
        self.changelog_path = output_folder / 'CHANGELOG.md'
        self.version_txt_path = output_folder / 'version.txt'

    def _make_request_headers(self):
        access_token = self._manager.config.global_config.access_tokens.wago
        if access_token:
            return {'api-key': access_token}

    @staticmethod
    def extract_auras(model: type[WeakAuras | Plateroos], source: str) -> WeakAuras | Plateroos:
        from ._custom_slpp import loads

        lua_table = loads(f'{{{source}}}')
        (aura_table,) = lua_table.values()
        return model.from_lua_table(aura_table)

    def extract_installed_auras(self) -> Iterator[WeakAuras | Plateroos]:
        flavour_root = self._manager.config.addon_dir.parents[1]
        saved_vars_by_account = flavour_root.glob('WTF/Account/*/SavedVariables')
        for saved_vars, model in product(
            saved_vars_by_account,
            [WeakAuras, Plateroos],
        ):
            file = (saved_vars / model.addon_name).with_suffix('.lua')
            if not file.exists():
                logger.info(f'{file} not found')
            else:
                content = file.read_text(encoding='utf-8-sig', errors='replace')
                aura_group_cache = self._manager.config.global_config.cache_dir / shasum(content)
                if aura_group_cache.exists():
                    logger.info(f'loading {file} from cache at {aura_group_cache}')
                    aura_groups_json = json.loads(aura_group_cache.read_bytes())
                    aura_groups = _aura_converter.structure({'root': aura_groups_json}, model)
                else:
                    with time_op(
                        lambda t: logger.debug(f'extracted {model.__name__} in {t:.3f}s')
                    ):
                        aura_groups = self.extract_auras(model, content)
                    aura_group_cache.write_text(
                        json.dumps(_aura_converter.unstructure(aura_groups.root)),
                        encoding='utf-8',
                    )
                yield aura_groups

    async def _fetch_wago_metadata(self, api_ep: str, aura_ids: Iterable[str]):
        async with self._manager.web_client.get(
            (_CHECK_API_URL / api_ep).with_query(ids=','.join(aura_ids)),
            expire_after=timedelta(minutes=30),
            headers=self._make_request_headers(),
            trace_request_ctx=make_generic_progress_ctx('Fetching aura metadata'),
        ) as response:
            metadata: list[_WagoApiResponse]
            if response.status == 404:
                metadata = []
                return metadata

            response.raise_for_status()

            metadata = await response.json()
            return sorted(metadata, key=lambda r: r['slug'])

    async def _fetch_wago_import_string(self, aura: _WagoApiResponse):
        async with self._manager.web_client.get(
            _IMPORT_STRING_API_URL.with_query(id=aura['_id']).with_fragment(str(aura['version'])),
            expire_after=CACHE_INDEFINITELY,
            headers=self._make_request_headers(),
            raise_for_status=True,
            trace_request_ctx=make_generic_progress_ctx(f"Fetching aura '{aura['slug']}'"),
        ) as response:
            return await response.text()

    async def get_remote_auras(
        self, auras: WeakAuras | Plateroos
    ) -> list[tuple[Sequence[WeakAura | Plateroo], _WagoApiResponse, str]]:
        if not auras.root:
            return []

        metadata = await self._fetch_wago_metadata(auras.api_ep, auras.root)
        import_strings = await gather(self._fetch_wago_import_string(r) for r in metadata)
        return [
            (auras.root.get(r['slug']) or auras.root[r['_id']], r, i)
            for r, i in zip(metadata, import_strings)
        ]

    def _generate_addon(
        self,
        auras: dict[
            type,
            list[tuple[Sequence[WeakAura | Plateroo], _WagoApiResponse, str]],
        ],
    ):
        from zipfile import ZipFile, ZipInfo

        from jinja2 import Environment, FunctionLoader

        from . import _wa_templates

        jinja_env = Environment(
            trim_blocks=True,
            lstrip_blocks=True,
            loader=FunctionLoader(partial(read_resource_as_text, _wa_templates)),
        )

        aura_dict = dict.fromkeys((WeakAuras, Plateroos), []) | dict(auras)

        self.addon_zip_path.parent.mkdir(exist_ok=True)
        with ZipFile(self.addon_zip_path, 'w') as file:

            def write_tpl(filename: str, ctx: dict[str, Any]):
                # Not using a plain string as the first argument to ``writestr``
                # 'cause the timestamp would be set to the current time
                # which would render the build unreproducible
                zip_info = ZipInfo(filename=f'WeakAurasCompanion/{filename}')
                output = jinja_env.get_template(filename).render(ctx)
                file.writestr(zip_info, output)
                return output

            data_output = write_tpl(
                'data.lua',
                {
                    'addons': {
                        WeakAuras.addon_name: [
                            (
                                metadata['slug'],
                                {
                                    'name': metadata['name'],
                                    'author': metadata.get('username', '__unknown__'),
                                    'encoded': import_string,
                                    'wagoVersion': metadata['version'],
                                    # ``wagoSemver`` is supposed to be the ``versionString``
                                    # from Wago but there is a bug where the ``version``
                                    # is sometimes not appended to the semver.
                                    # The Companion add-on's version is derived from its checksum
                                    # so if ``wagoSemver`` were to change between requests
                                    # we'd be triggering spurious updates in instawow.
                                    'wagoSemver': metadata['version'],
                                    'versionNote': metadata['changelog'].get('text', ''),
                                    'source': 'Wago',
                                },
                            )
                            for _, metadata, import_string in aura_dict[WeakAuras]
                        ],
                        Plateroos.addon_name: [
                            (
                                metadata['slug'],
                                {
                                    'name': metadata['name'],
                                    'author': metadata.get('username', '__unknown__'),
                                    'encoded': import_string,
                                    'wagoVersion': metadata['version'],
                                    'wagoSemver': metadata['version'],
                                    'versionNote': metadata['changelog'].get('text', ''),
                                    'source': 'Wago',
                                },
                            )
                            for _, metadata, import_string in aura_dict[Plateroos]
                        ],
                    },
                },
            )
            init_output = write_tpl('init.lua', {})

            interface_version = self._manager.config.game_flavour.to_flavour_keyed_enum(
                _TocNumber
            ).value
            addon_version = shasum(data_output, init_output, interface_version)[:7]

            write_tpl(
                'WeakAurasCompanion.toc',
                {'interface': interface_version, 'version': addon_version},
            )

        self.changelog_path.write_text(
            jinja_env.get_template(self.changelog_path.name).render(
                {
                    'changelog_entries': [
                        (
                            a.id,
                            a.url.parent,
                            metadata['version'],
                            metadata['changelog'].get('text') or 'n/a',
                        )
                        for v in aura_dict.values()
                        for existing_auras, metadata, _ in v
                        for a in (
                            next((i for i in existing_auras if not i.parent), existing_auras[0]),
                        )
                        if a.version != metadata['version']
                    ]
                }
            )
            or 'n/a',
            encoding='utf-8',
        )

        self.version_txt_path.write_text(
            addon_version,
            encoding='utf-8',
        )

    async def build(self) -> None:
        installed_auras = await t(list)(self.extract_installed_auras())
        installed_auras_by_type = _merge_auras(installed_auras)
        aura_groups = await gather(
            self.get_remote_auras(r) for r in installed_auras_by_type.values()
        )
        aura_groups_by_type = dict(zip(installed_auras_by_type, aura_groups))
        await t(self._generate_addon)(aura_groups_by_type)

    def get_version(self) -> str:
        return self.version_txt_path.read_text(encoding='utf-8')

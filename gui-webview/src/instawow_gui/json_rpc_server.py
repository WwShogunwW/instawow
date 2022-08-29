# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import partial
import importlib.resources
import json
import os
from pathlib import Path
import typing
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp
import aiohttp.web
from aiohttp_rpc import JsonRpcMethod, middlewares as rpc_middlewares
from aiohttp_rpc.errors import InvalidParams, ServerError
from aiohttp_rpc.server import WsJsonRpcServer
from attrs import evolve, frozen
from cattrs import GenConverter
from cattrs.preconf.json import configure_converter
import click
from exceptiongroup import ExceptionGroup
from loguru import logger
import sqlalchemy as sa
import toga  # pyright: ignore
from typing_extensions import Concatenate, Literal, ParamSpec, TypeAlias, TypedDict
from yarl import URL

from instawow import __version__, db, matchers, models, results as R
from instawow.cataloguer import CatalogueEntry
from instawow.common import Flavour, SourceMetadata, infer_flavour_from_path
from instawow.config import Config, GlobalConfig, SecretStr, config_converter
from instawow.github_auth import get_codes, poll_for_access_token
from instawow.manager import (
    LocksType,
    Manager,
    TraceRequestCtx,
    contextualise,
    init_web_client,
    is_outdated,
)
from instawow.resolvers import Defn
from instawow.utils import reveal_folder, run_in_thread as t, uniq

from . import frontend

if TYPE_CHECKING:
    ExceptionGroup = ExceptionGroup[Exception]

_T = TypeVar('_T')
_P = ParamSpec('_P')
_ManagerBoundCoroFn: TypeAlias = 'Callable[Concatenate[Manager, _P], Awaitable[_T]]'


LOCALHOST = '127.0.0.1'

_converter = GenConverter(
    unstruct_collection_overrides={
        # TODO: Replace with ``collections.abc.Set``
        frozenset: sorted,
    }
)
configure_converter(_converter)
_converter.register_structure_hook(Path, lambda v, _: Path(v))
_converter.register_unstructure_hook(Path, str)


class _ConfigError(ServerError):
    code = -32001
    message = 'invalid configuration parameters'


def _structure_excs(excs: Iterable[BaseException]):
    return [
        {'loc': [n], 'msg': str(e)}
        for e in excs
        for *_, n in (getattr(e, '__note__', '').rpartition(' '),)
        if n.isidentifier()
    ]


@contextmanager
def _reraise_validation_error(
    error_class: type[ServerError | InvalidParams] = ServerError,
    values: dict[Any, Any] | None = None,
) -> Iterator[None]:
    try:
        yield
    except ExceptionGroup as exc_group:
        logger.info(f'invalid request: {(values, exc_group)}')
        raise error_class(data=_structure_excs(exc_group.exceptions))


@t
def _read_config(profile: str) -> Config:
    with _reraise_validation_error(_ConfigError):
        return Config.read(GlobalConfig.read(), profile)


_methods: list[tuple[str, type[BaseParams]]] = []


def _register_method(method: str):
    def inner(param_class: type[BaseParams]):
        _methods.append((method, param_class))
        return frozen(slots=False)(param_class)

    return inner


@frozen(slots=False)
class _ProfileParamMixin:
    profile: str


@frozen(slots=False)
class _DefnParamMixin:
    defns: typing.List[Defn]


class BaseParams:
    @classmethod
    def bind(
        cls, method: str, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> JsonRpcMethod:
        async def respond(**kwargs: Any):
            with _reraise_validation_error(InvalidParams, kwargs):
                self = _converter.structure(kwargs, cls)
            return await self.respond(managers, app_window)

        return JsonRpcMethod(respond, name=method)

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> Any:
        raise NotImplementedError


@_register_method('config/write_profile')
class WriteProfileConfigParams(_ProfileParamMixin, BaseParams):
    addon_dir: Path
    game_flavour: Flavour
    infer_game_flavour: bool

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> Config:
        async with managers.locks['modify profile', self.profile]:
            with _reraise_validation_error(_ConfigError):
                config = config_converter.structure(
                    {
                        'global_config': await t(GlobalConfig.read)(),
                        'profile': self.profile,
                        'addon_dir': self.addon_dir,
                        'game_flavour': self.game_flavour,
                    },
                    Config,
                )
                if self.infer_game_flavour:
                    config = evolve(
                        config,
                        game_flavour=infer_flavour_from_path(config.addon_dir),
                    )
                await t(config.write)()

            # Unload the corresponding ``Manager`` instance for the
            # config to be reloaded on the next request
            managers.unload(config.profile)

            return config


@_register_method('config/read_profile')
class ReadProfileConfigParams(_ProfileParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> Config:
        return await _read_config(self.profile)


@_register_method('config/delete_profile')
class DeleteProfileConfigParams(_ProfileParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> None:
        async def delete_profile(manager: Manager):
            await t(manager.config.delete)()
            managers.unload(self.profile)

        await managers.run(self.profile, delete_profile)


@_register_method('config/list_profiles')
class ListProfilesParams(BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[str]:
        return await t(GlobalConfig().list_profiles)()


@_register_method('config/update_global')
class UpdateGlobalConfigParams(BaseParams):
    access_tokens: typing.Dict[str, typing.Optional[str]]

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> GlobalConfig:
        async with managers.locks['update global config']:
            with _reraise_validation_error(_ConfigError):
                global_config = await t(GlobalConfig.read)()
                global_config = evolve(
                    global_config,
                    access_tokens=evolve(
                        global_config.access_tokens,
                        **{
                            k: t if t is None else SecretStr(t)
                            for k, t in self.access_tokens.items()
                        },
                    ),
                )
                await t(global_config.write)()

        managers.unload_all()
        return global_config


@_register_method('config/read_global')
class ReadGlobalConfigParams(BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> GlobalConfig:
        return await t(GlobalConfig.read)()


class GithubCodesResponse(TypedDict):
    user_code: str
    verification_uri: str


@_register_method('config/initiate_github_auth_flow')
class InitiateGithubAuthFlowParams(BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> GithubCodesResponse:
        return await managers.initiate_github_auth_flow()


class GithubAuthFlowStatusReport(TypedDict):
    status: Literal['success', 'failure']


@_register_method('config/query_github_auth_flow_status')
class QueryGithubAuthFlowStatusParams(BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> GithubAuthFlowStatusReport:
        return {'status': await managers.wait_for_github_auth_completion()}


@_register_method('config/cancel_github_auth_flow')
class CancelGithubAuthFlowParams(BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> None:
        managers.cancel_github_auth_polling()


@_register_method('sources/list')
class ListSourcesParams(_ProfileParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> dict[str, SourceMetadata]:
        manager = await managers.get_manager(self.profile)
        return {r.metadata.id: r.metadata for r in manager.resolvers.values()}


@_register_method('list')
class ListInstalledParams(_ProfileParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[models.Pkg]:
        manager = await managers.get_manager(self.profile)
        installed_pkgs = (
            manager.database.execute(sa.select(db.pkg).order_by(sa.func.lower(db.pkg.c.name)))
            .mappings()
            .all()
        )
        return [models.Pkg.from_row_mapping(manager.database, p) for p in installed_pkgs]


@_register_method('search')
class SearchParams(_ProfileParamMixin, BaseParams):
    search_terms: str
    limit: int
    sources: typing.Set[str]
    start_date: typing.Optional[datetime]
    installed_only: bool

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[CatalogueEntry]:
        return await managers.run(
            self.profile,
            partial(
                Manager.search,
                search_terms=self.search_terms,
                limit=self.limit,
                sources=self.sources,
                start_date=self.start_date,
                installed_only=self.installed_only,
            ),
        )


class SuccessResult(TypedDict):
    status: Literal['success']
    addon: models.Pkg


class ErrorResult(TypedDict):
    status: Literal['failure', 'error']
    message: str


@_register_method('resolve')
class ResolveParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    async def _resolve(self, manager: Manager) -> dict[Defn, Any]:
        def extract_source(defn: Defn):
            if defn.source == '*':
                pair = manager.pair_uri(defn.alias)
                if pair:
                    source, alias = pair
                    defn = evolve(defn, source=source, alias=alias)
            return defn

        return await manager.resolve(list(map(extract_source, self.defns)))

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[SuccessResult | ErrorResult]:
        results = await managers.run(self.profile, self._resolve)
        return [
            {'status': 'success', 'addon': r}
            if isinstance(r, models.Pkg)
            else {'status': r.status, 'message': r.message}
            for r in results.values()
        ]


@_register_method('install')
class InstallParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    replace: bool

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[SuccessResult | ErrorResult]:
        results = await managers.run(
            self.profile, partial(Manager.install, defns=self.defns, replace=self.replace)
        )
        return [
            {'status': r.status, 'addon': r.pkg}
            if isinstance(r, R.PkgInstalled)
            else {'status': r.status, 'message': r.message}
            for r in results.values()
        ]


@_register_method('update')
class UpdateParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[SuccessResult | ErrorResult]:
        results = await managers.run(
            self.profile, partial(Manager.update, defns=self.defns, retain_defn_strategy=True)
        )
        return [
            {'status': r.status, 'addon': r.new_pkg}
            if isinstance(r, R.PkgUpdated)
            else {'status': r.status, 'message': r.message}
            for r in results.values()
        ]


@_register_method('remove')
class RemoveParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    keep_folders: bool

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[SuccessResult | ErrorResult]:
        results = await managers.run(
            self.profile, partial(Manager.remove, defns=self.defns, keep_folders=self.keep_folders)
        )
        return [
            {'status': r.status, 'addon': r.old_pkg}
            if isinstance(r, R.PkgRemoved)
            else {'status': r.status, 'message': r.message}
            for r in results.values()
        ]


@_register_method('pin')
class PinParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[SuccessResult | ErrorResult]:
        results = await managers.run(self.profile, partial(Manager.pin, defns=self.defns))
        return [
            {'status': r.status, 'addon': r.pkg}
            if isinstance(r, R.PkgInstalled)
            else {'status': r.status, 'message': r.message}
            for r in results.values()
        ]


@_register_method('get_changelog')
class GetChangelogParams(_ProfileParamMixin, BaseParams):
    source: str
    changelog_url: str

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> str:
        return await managers.run(
            self.profile,
            partial(Manager.get_changelog, source=self.source, uri=self.changelog_url),
        )


class AddonMatch(TypedDict):
    folders: list[AddonMatch_AddonFolder]
    matches: list[models.Pkg]


class AddonMatch_AddonFolder(TypedDict):
    name: str
    version: str


@_register_method('reconcile')
class ReconcileParams(_ProfileParamMixin, BaseParams):
    matcher: Literal['toc_source_ids', 'folder_name_subsets', 'addon_names_with_folder_names']

    _MATCHERS = {
        'toc_source_ids': matchers.match_toc_source_ids,
        'folder_name_subsets': matchers.match_folder_name_subsets,
        'addon_names_with_folder_names': matchers.match_addon_names_with_folder_names,
    }

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[AddonMatch]:
        leftovers = await managers.run(self.profile, t(matchers.get_unreconciled_folders))
        match_groups = await managers.run(
            self.profile, partial(self._MATCHERS[self.matcher], leftovers=leftovers)
        )
        resolved_defns = await managers.run(
            self.profile,
            partial(Manager.resolve, defns=uniq(d for _, b in match_groups for d in b)),
        )
        resolved_pkgs = {k: v for k, v in resolved_defns.items() if isinstance(v, models.Pkg)}
        matched_pkgs = [
            (a, m)
            for a, s in match_groups
            for m in ([i for i in (resolved_pkgs.get(d) for d in s) if i],)
            if m
        ]
        return [
            *(
                AddonMatch(folders=[{'name': f.name, 'version': f.version} for f in a], matches=m)
                for a, m in matched_pkgs
            ),
            *(
                AddonMatch(folders=[{'name': f.name, 'version': f.version}], matches=[])
                for f in sorted(leftovers - frozenset(i for a, _ in matched_pkgs for i in a))
            ),
        ]


class ReconcileInstalledCandidate(TypedDict):
    installed_addon: models.Pkg
    alternative_addons: list[models.Pkg]


@_register_method('get_reconcile_installed_candidates')
class GetReconcileInstalledCandidatesParams(_ProfileParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[ReconcileInstalledCandidate]:
        manager = await managers.get_manager(self.profile)
        installed_pkgs = (
            models.Pkg.from_row_mapping(manager.database, p)
            for p in manager.database.execute(
                sa.select(db.pkg).order_by(sa.func.lower(db.pkg.c.name))
            )
            .mappings()
            .all()
        )
        defn_groups = await managers.run(
            self.profile, partial(Manager.find_equivalent_pkg_defns, pkgs=installed_pkgs)
        )
        resolved_defns = await managers.run(
            self.profile,
            partial(Manager.resolve, defns=uniq(d for b in defn_groups.values() for d in b)),
        )
        resolved_pkgs = {k: v for k, v in resolved_defns.items() if isinstance(v, models.Pkg)}
        return [
            {'installed_addon': p, 'alternative_addons': m}
            for p, s in defn_groups.items()
            for m in ([i for i in (resolved_pkgs.get(d) for d in s) if i],)
            if m
        ]


class DownloadProgressReport(TypedDict):
    defn: Defn
    progress: float


@_register_method('get_download_progress')
class GetDownloadProgressParams(_ProfileParamMixin, BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> list[DownloadProgressReport]:
        return [
            {'defn': Defn.from_pkg(p), 'progress': r}
            for p, r in await managers.get_manager_download_progress(self.profile)
        ]


class GetVersionResult(TypedDict):
    installed_version: str
    new_version: str | None


@_register_method('meta/get_version')
class GetVersionParams(BaseParams):
    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> GetVersionResult:
        outdated, new_version = await is_outdated()
        return {
            'installed_version': __version__,
            'new_version': new_version if outdated else None,
        }


@_register_method('assist/open_url')
class OpenUrlParams(BaseParams):
    url: str

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> None:
        click.launch(self.url)


@_register_method('assist/reveal_folder')
class RevealFolderParams(BaseParams):
    path_parts: typing.List[str]

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> None:
        reveal_folder(os.path.join(*self.path_parts))


class SelectFolderResult(TypedDict):
    selection: Path | None


@_register_method('assist/select_folder')
class SelectFolderParams(BaseParams):
    initial_folder: typing.Optional[str]

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> SelectFolderResult:
        if not app_window:
            raise RuntimeError('No app to bind to')

        selection: Path | None
        try:
            selection = await app_window.select_folder_dialog('Select folder', self.initial_folder)
        except ValueError:
            selection = None
        return {'selection': selection}


class ConfirmDialogueResult(TypedDict):
    ok: bool


@_register_method('assist/confirm')
class ConfirmDialogueParams(BaseParams):
    title: str
    message: str

    async def respond(
        self, managers: _ManagerWorkQueue, app_window: toga.MainWindow | None
    ) -> ConfirmDialogueResult:
        if not app_window:
            raise RuntimeError('No app to bind to')

        ok: bool = await app_window.confirm_dialog(self.title, self.message)
        return {'ok': ok}


def _init_json_rpc_web_client():
    async def do_on_request_end(
        client_session: aiohttp.ClientSession,
        trace_config_ctx: Any,
        params: aiohttp.TraceRequestEndParams,
    ) -> None:
        trace_request_ctx: TraceRequestCtx = trace_config_ctx.trace_request_ctx
        if (
            trace_request_ctx
            and trace_request_ctx['report_progress'] == 'pkg_download'
            and params.response.content_length
            and aiohttp.hdrs.CONTENT_ENCODING not in params.response.headers
        ):
            content = params.response.content
            content_length = params.response.content_length
            entry = (
                trace_request_ctx['manager'],
                trace_request_ctx['pkg'],
                lambda: content.total_bytes / content_length,
            )
            progress_reporters.add(entry)
            content.on_eof(lambda: progress_reporters.remove(entry))

    progress_reporters: set[tuple[Manager, models.Pkg, Callable[[], float]]] = set()

    trace_config = aiohttp.TraceConfig()
    trace_config.on_request_end.append(do_on_request_end)
    trace_config.freeze()

    return (init_web_client(trace_configs=[trace_config]), progress_reporters)


class _ManagerWorkQueue:
    def __init__(self):
        self._loop = asyncio.get_running_loop()

        self._queue: asyncio.Queue[
            tuple[asyncio.Future[object], str, _ManagerBoundCoroFn[..., object]]
        ] = asyncio.Queue()

        self.locks: LocksType = defaultdict(asyncio.Lock)

        self._managers: dict[str, tuple[Manager, Callable[[], None]]] = {}

        self._web_client, self._download_progress_reporters = _init_json_rpc_web_client()

        self._github_auth_device_codes = None
        self._github_auth_flow_task = None

    async def __aenter__(self):
        self._listener = self._loop.create_task(self._listen())

    async def __aexit__(self, *args: object):
        self.cancel_github_auth_polling()
        self._listener.cancel()
        self.unload_all()
        await self._web_client.close()

    def unload(self, profile: str):
        if profile in self._managers:
            logger.debug(f'unloading {profile}')
            _, close_db_conn = self._managers.pop(profile)
            close_db_conn()

    def unload_all(self):
        for profile in list(self._managers):
            self.unload(profile)

    async def _schedule_item(
        self,
        future: asyncio.Future[object],
        profile: str,
        coro_fn: _ManagerBoundCoroFn[..., object],
    ):
        try:
            async with self.locks['modify profile', profile]:
                try:
                    manager, _ = self._managers[profile]
                except KeyError:
                    manager, _ = self._managers[profile] = Manager.from_config(
                        await _read_config(profile)
                    )

            result = await coro_fn(manager)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    async def _listen(self):
        contextualise(web_client=self._web_client, locks=self.locks)
        while True:
            item = await self._queue.get()
            asyncio.create_task(self._schedule_item(*item))
            self._queue.task_done()

    async def run(self, profile: str, coro_fn: _ManagerBoundCoroFn[..., _T]) -> _T:
        future = self._loop.create_future()
        self._queue.put_nowait((future, profile, coro_fn))
        return await future

    async def get_manager(self, profile: str):
        async def get_manager(manager: Manager):
            return manager

        return await self.run(profile, get_manager)

    async def get_manager_download_progress(self, profile: str):
        manager = await self.get_manager(profile)
        return ((p, r()) for m, p, r in self._download_progress_reporters if m is manager)

    async def initiate_github_auth_flow(self):
        async with self.locks['initiate github auth flow']:
            if self._github_auth_device_codes is None:

                async def finalise_github_auth_flow():
                    result = await poll_for_access_token(
                        self._web_client, codes['device_code'], codes['interval']
                    )

                    async with self.locks['update global config']:
                        global_config = await t(GlobalConfig.read)()
                        global_config = evolve(
                            global_config,
                            access_tokens=evolve(
                                global_config.access_tokens, cfcore=SecretStr(result)
                            ),
                        )
                        await t(global_config.write)()

                    self.unload_all()

                def on_task_complete(future: object):
                    self._github_auth_flow_task = None
                    self._github_auth_device_codes = None

                self._github_auth_device_codes = codes = await get_codes(self._web_client)
                self._github_auth_flow_task = self._loop.create_task(finalise_github_auth_flow())
                self._github_auth_flow_task.add_done_callback(on_task_complete)

        return self._github_auth_device_codes

    async def wait_for_github_auth_completion(self):
        if self._github_auth_flow_task is not None:
            try:
                await self._github_auth_flow_task
            except BaseException:
                return 'failure'
        return 'success'

    def cancel_github_auth_polling(self):
        if self._github_auth_flow_task is not None:
            self._github_auth_flow_task.cancel()


async def create_app(app_window: toga.MainWindow | None = None):
    managers = _ManagerWorkQueue()

    async def listen(app: aiohttp.web.Application):
        async with managers:
            yield

    async def get_index(request: aiohttp.web.Request):
        return aiohttp.web.Response(
            content_type='text/html',
            text=importlib.resources.read_text(frontend, 'index.html'),
        )

    async def get_static_file(request: aiohttp.web.Request):
        filename = request.path.lstrip('/')
        if filename.startswith('svelte-bundle.js'):
            content_type = 'application/javascript'
        elif filename == 'svelte-bundle.css':
            content_type = 'text/css'
        else:
            raise aiohttp.web.HTTPNotFound

        return aiohttp.web.Response(
            content_type=content_type,
            text=importlib.resources.read_text(frontend, filename),
        )

    def json_serialize(value: dict[str, Any]):
        return json.dumps(_converter.unstructure(value))

    rpc_server = WsJsonRpcServer(
        json_serialize=json_serialize,
        middlewares=rpc_middlewares.DEFAULT_MIDDLEWARES,
    )
    rpc_server.add_methods([m.bind(n, managers, app_window) for n, m in _methods])

    @aiohttp.web.middleware
    async def enforce_same_origin(
        request: aiohttp.web.Request,
        handler: Callable[[aiohttp.web.Request], Awaitable[aiohttp.web.StreamResponse]],
    ):
        if request.remote != LOCALHOST:
            raise aiohttp.web.HTTPUnauthorized

        if request.path.startswith('/api'):
            origin = request.headers.get(aiohttp.hdrs.ORIGIN)
            if origin is None:
                raise aiohttp.web.HTTPUnauthorized

            origin_url = URL(origin)
            if (
                origin_url.scheme != request.url.scheme
                or origin_url.host != request.url.host
                or origin_url.port != request.url.port
            ):
                raise aiohttp.web.HTTPUnauthorized

        return await handler(request)

    app = aiohttp.web.Application(middlewares=[enforce_same_origin])
    app.add_routes(
        [
            aiohttp.web.get('/', get_index),
            aiohttp.web.get(
                r'/svelte-bundle{extension:(?:\.css|\.js(?:\.map)?)}', get_static_file
            ),
            aiohttp.web.get('/api', rpc_server.handle_http_request),
        ]
    )
    app.cleanup_ctx.append(listen)
    app.freeze()
    return app


async def run_app(app: aiohttp.web.Application):
    "Fire up the server."
    app_runner = aiohttp.web.AppRunner(app)
    await app_runner.setup()
    assert app_runner.server  # Server is created during setup
    # By omitting the port ``loop.create_server`` will find an available port
    # to bind to - equivalent to creating a socket on port 0.
    server = await asyncio.get_running_loop().create_server(app_runner.server, LOCALHOST)
    host, port = server.sockets[0].getsockname()

    async def serve():
        try:
            # ``server_forever`` cleans up after the server when it's interrupted
            await server.serve_forever()
        finally:
            await app_runner.cleanup()

    return (
        URL.build(scheme='http', host=host, port=port),
        serve,
    )

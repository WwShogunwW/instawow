from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import partial
from itertools import starmap
import os
import typing
from typing import Any, TypeVar, overload
from uuid import uuid4

from aiohttp import web
from aiohttp_rpc import JsonRpcMethod, WsJsonRpcServer, middlewares as rpc_middlewares
from aiohttp_rpc.errors import InvalidParams as InvalidParamsError, ServerError
from pydantic import BaseModel, ValidationError, validator
from typing_extensions import Literal, TypeAlias
from yarl import URL

from . import results as E
from .config import Config
from .manager import Manager, init_web_client, prepare_database
from .matchers import get_folder_set, match_dir_names, match_toc_ids, match_toc_names
from .models import Pkg, is_pkg
from .resolvers import CatalogueEntry, Defn, PkgModel, Strategy
from .utils import get_version, is_outdated, run_in_thread as t, uniq

_T = TypeVar('_T')
ManagerWorkQueueItem: TypeAlias = (
    'tuple[asyncio.Future[Any], str, Callable[..., Awaitable[Any]] | None]'
)


LOCALHOST = '127.0.0.1'


class _ConfigError(ServerError):
    code = -32001
    message = 'invalid configuration parameters'


@contextmanager
def _reraise_validation_error(error_class: type[ServerError] = ServerError) -> Iterator[None]:
    try:
        yield
    except ValidationError as error:
        raise error_class(data=error.errors()) from error


class BaseParams(BaseModel):
    async def respond(self, managers: ManagerWorkQueue) -> Any:
        raise NotImplementedError


class _ProfileParamMixin(BaseModel):
    profile: str


class _DefnParamMixin(BaseModel):
    defns: typing.List[Defn]


class WriteConfigParams(BaseParams):
    values: typing.Dict[str, Any]
    infer_game_flavour: bool

    @t
    def respond(self, managers: ManagerWorkQueue) -> Config:
        with _reraise_validation_error(_ConfigError):
            config = Config(**self.values)
            if self.infer_game_flavour:
                config.game_flavour = Config.infer_flavour(config.addon_dir)
            config.write()

        # Dispose of the ``Manager`` for this profile so that it's reloaded
        # on next invocation of ``ManagerWorkQueue.run``
        managers.managers.pop(config.profile, None)
        return config


class ReadConfigParams(_ProfileParamMixin, BaseParams):
    @t
    def respond(self, managers: ManagerWorkQueue) -> Config:
        with _reraise_validation_error(_ConfigError):
            return Config.read(self.profile)


class DeleteConfigParams(_ProfileParamMixin, BaseParams):
    async def respond(self, managers: ManagerWorkQueue) -> None:
        async def delete_profile(manager: Manager):
            await t(manager.config.delete)()
            managers.managers.pop(self.profile, None)

        await managers.run(self.profile, delete_profile)


class ListProfilesParams(BaseParams):
    @t
    def respond(self, managers: ManagerWorkQueue) -> list[str]:
        return Config.list_profiles()


class Source(BaseModel):
    source: str
    name: str
    supported_strategies: typing.List[Strategy]
    supports_rollback: bool

    @validator('supported_strategies')
    def _sort_strategies(cls, value: list[Strategy]) -> list[Strategy]:
        return sorted(value, key=list(Strategy).index)


class ListSourcesParams(_ProfileParamMixin, BaseParams):
    async def respond(self, managers: ManagerWorkQueue) -> list[Source]:
        manager = await managers.run(self.profile)
        return [
            Source(
                source=r.source,
                name=r.name,
                supported_strategies=r.strategies,
                supports_rollback=r.supports_rollback,
            )
            for r in manager.resolvers.values()
        ]


class ListResult(BaseModel):
    __root__: typing.List[PkgModel]


class ListInstalledParams(_ProfileParamMixin, BaseParams):
    async def respond(self, managers: ManagerWorkQueue) -> ListResult:
        from sqlalchemy import func

        manager = await managers.run(self.profile)
        installed_pkgs = manager.database.query(Pkg).order_by(func.lower(Pkg.name)).all()
        return ListResult.parse_obj(installed_pkgs)


class SuccessResult(BaseModel):
    status: Literal['success']
    addon: PkgModel


class ErrorResult(BaseModel):
    status: Literal['failure', 'error']
    message: str


class MultiResult(BaseModel):
    __root__: typing.List[typing.Union[SuccessResult, ErrorResult]]

    @validator('__root__', each_item=True, pre=True)
    def _classify_tuple(cls, value: tuple[str, object]) -> SuccessResult | ErrorResult:
        status, result = value
        if status == 'success':
            return SuccessResult(status=status, addon=result)
        else:
            return ErrorResult(status=status, message=result)


class SearchParams(_ProfileParamMixin, BaseParams):
    search_terms: str
    limit: int
    sources: typing.Optional[typing.Set[str]] = None

    async def respond(self, managers: ManagerWorkQueue) -> list[CatalogueEntry]:
        return await managers.run(
            self.profile,
            partial(
                Manager.search,
                search_terms=self.search_terms,
                limit=self.limit,
                sources=self.sources,
            ),
        )


class ResolveParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    async def respond(self, managers: ManagerWorkQueue) -> MultiResult:
        def extract_source(manager: Manager, defns: list[Defn]):
            for defn in defns:
                if defn.source == '*':
                    pair = manager.pair_uri(defn.alias)
                    if pair:
                        source, alias = pair
                        defn = defn.with_(source=source, alias=alias)
                yield defn

        results = await managers.run(
            self.profile,
            partial(
                Manager.resolve,
                defns=list(extract_source(await managers.run(self.profile), self.defns)),
            ),
        )
        return MultiResult.parse_obj(
            [('success', r) if is_pkg(r) else (r.status, r.message) for r in results.values()]
        )


class InstallParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    replace: bool

    async def respond(self, managers: ManagerWorkQueue) -> MultiResult:
        results = await managers.run(
            self.profile, partial(Manager.install, defns=self.defns, replace=self.replace)
        )
        return MultiResult.parse_obj(
            [
                (r.status, r.pkg if isinstance(r, E.PkgInstalled) else r.message)
                for r in results.values()
            ]
        )


class UpdateParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    async def respond(self, managers: ManagerWorkQueue) -> MultiResult:
        results = await managers.run(
            self.profile, partial(Manager.update, defns=self.defns, retain_strategy=True)
        )
        return MultiResult.parse_obj(
            [
                (r.status, r.new_pkg if isinstance(r, E.PkgUpdated) else r.message)
                for r in results.values()
            ]
        )


class RemoveParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    keep_folders: bool

    async def respond(self, managers: ManagerWorkQueue) -> MultiResult:
        results = await managers.run(
            self.profile, partial(Manager.remove, defns=self.defns, keep_folders=self.keep_folders)
        )
        return MultiResult.parse_obj(
            [
                (r.status, r.old_pkg if isinstance(r, E.PkgRemoved) else r.message)
                for r in results.values()
            ]
        )


class PinParams(_ProfileParamMixin, _DefnParamMixin, BaseParams):
    async def respond(self, managers: ManagerWorkQueue) -> MultiResult:
        results = await managers.run(self.profile, partial(Manager.pin, defns=self.defns))
        return MultiResult.parse_obj(
            [
                (r.status, r.pkg if isinstance(r, E.PkgInstalled) else r.message)
                for r in results.values()
            ]
        )


class AddonFolder(BaseModel):
    name: str
    version: str


class AddonMatch(BaseModel):
    folders: typing.List[AddonFolder]
    matches: typing.List[PkgModel]


class ReconcileResult(BaseModel):
    reconciled: typing.List[AddonMatch]
    unreconciled: typing.List[AddonMatch]

    @validator('reconciled', 'unreconciled', pre=True)
    def _transform_matches(cls, value: Any):
        return [
            {
                'folders': [AddonFolder(name=f.name, version=f.version) for f in s],
                'matches': m,
            }
            for s, m in value
        ]


_matchers = {
    'toc_ids': match_toc_ids,
    'dir_names': match_dir_names,
    'toc_names': match_toc_names,
}


class ReconcileParams(_ProfileParamMixin, BaseParams):
    matcher: Literal['toc_ids', 'dir_names', 'toc_names']

    async def respond(self, managers: ManagerWorkQueue) -> ReconcileResult:
        leftovers = await managers.run(self.profile, t(get_folder_set))
        match_groups = await managers.run(
            self.profile, partial(_matchers[self.matcher], leftovers=leftovers)
        )
        uniq_defns = uniq(d for _, b in match_groups for d in b)
        resolve_results = await managers.run(
            self.profile, partial(Manager.resolve, defns=uniq_defns)
        )
        reconciled = [
            (a, list(filter(is_pkg, (resolve_results[i] for i in d)))) for a, d in match_groups
        ]
        unreconciled = [
            ([l], []) for l in sorted(leftovers - frozenset(i for a, _ in match_groups for i in a))
        ]
        return ReconcileResult(reconciled=reconciled, unreconciled=unreconciled)


class GetVersionResult(BaseModel):
    installed_version: str
    new_version: typing.Optional[str]


class GetVersionParams(BaseParams):
    async def respond(self, managers: ManagerWorkQueue) -> GetVersionResult:
        outdated, new_version = await is_outdated()
        return GetVersionResult(
            installed_version=get_version(), new_version=new_version if outdated else None
        )


class ManagerWorkQueue:
    def __init__(self) -> None:
        self.managers: dict[str, Manager] = {}
        self._queue: asyncio.Queue[ManagerWorkQueueItem] = asyncio.Queue()
        self._web_client = init_web_client()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def listen(self) -> None:
        Manager.contextualise(
            web_client=self._web_client,
            locks=self._locks,
        )
        while True:
            item = await self._queue.get()

            async def schedule(item: ManagerWorkQueueItem):
                future, profile, coro_fn = item
                try:
                    async with self._locks[f"load profile '{profile}'"]:
                        try:
                            manager = self.managers[profile]
                        except KeyError:
                            with _reraise_validation_error(_ConfigError):
                                config = await t(Config.read)(profile)

                            manager = self.managers[profile] = Manager(
                                config=config,
                                database=await t(prepare_database)(config),
                            )

                    if coro_fn is None:
                        result = manager
                    else:
                        result = await coro_fn(manager)
                except BaseException as error:
                    future.set_exception(error)
                else:
                    future.set_result(result)

            asyncio.create_task(schedule(item))
            self._queue.task_done()

    async def cleanup(self) -> None:
        await self._web_client.close()

    @overload
    async def run(self, profile: str, coro_fn: None = ...) -> Manager:
        ...

    @overload
    async def run(self, profile: str, coro_fn: Callable[..., Awaitable[_T]] = ...) -> _T:
        ...

    async def run(
        self, profile: str, coro_fn: Callable[..., Awaitable[_T]] | None = None
    ) -> Manager | _T:
        future: asyncio.Future[Any] = asyncio.Future()
        self._queue.put_nowait((future, profile, coro_fn))
        return await asyncio.wait_for(future, None)


def _prepare_response(
    param_class: type[BaseParams], method: str, managers: ManagerWorkQueue
) -> JsonRpcMethod:
    async def respond(**kwargs: Any) -> BaseModel:
        with _reraise_validation_error(InvalidParamsError):
            params = param_class.parse_obj(kwargs)
        return await params.respond(managers)

    return JsonRpcMethod('', respond, custom_name=method)


def serialise_response(value: dict[str, Any]) -> str:
    return BaseModel.construct(**value).json()


async def create_app() -> tuple[web.Application, str]:
    managers = ManagerWorkQueue()

    def start_managers():
        managers_listen = asyncio.create_task(managers.listen())

        async def on_shutdown(app: web.Application):
            managers_listen.cancel()
            await managers.cleanup()

        return on_shutdown

    endpoint = f'/{uuid4()}'
    rpc_server = WsJsonRpcServer(
        json_serialize=serialise_response,
        middlewares=rpc_middlewares.DEFAULT_MIDDLEWARES,
    )
    rpc_server.add_methods(
        starmap(
            _prepare_response,
            [
                (WriteConfigParams, 'config/write', managers),
                (ReadConfigParams, 'config/read', managers),
                (DeleteConfigParams, 'config/delete', managers),
                (ListProfilesParams, 'config/list', managers),
                (ListSourcesParams, 'sources/list', managers),
                (ListInstalledParams, 'list', managers),
                (SearchParams, 'search', managers),
                (ResolveParams, 'resolve', managers),
                (InstallParams, 'install', managers),
                (UpdateParams, 'update', managers),
                (RemoveParams, 'remove', managers),
                (PinParams, 'pin', managers),
                (ReconcileParams, 'reconcile', managers),
                (GetVersionParams, 'meta/get_version', managers),
            ],
        )
    )
    app = web.Application()
    app.add_routes([web.get(endpoint, rpc_server.handle_http_request)])
    app.on_shutdown.append(start_managers())
    app.freeze()
    return (app, endpoint)


async def listen() -> None:
    "Fire up the server."
    loop = asyncio.get_running_loop()
    app, endpoint = await create_app()
    app_runner = web.AppRunner(app)
    await app_runner.setup()
    # Placate the type checker - server is created in ``app_runner.setup``
    assert app_runner.server
    # By omitting the port, ``loop.create_server`` will find an available port
    # to bind to - this is equivalent to creating a socket on port 0.
    server = await loop.create_server(app_runner.server, LOCALHOST)
    assert server.sockets
    (host, port) = server.sockets[0].getsockname()
    # We're writing the address to fd 3 just in case something seeps into
    # stdout or stderr.
    message = str(URL.build(scheme='ws', host=host, port=port, path=endpoint)).encode()
    for fd in (1, 3):
        os.write(fd, message)
    try:
        # ``server_forever`` cleans up after the server when it's interrupted
        await server.serve_forever()
    finally:
        await app_runner.cleanup()

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Sequence, Set
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial, wraps
import importlib.resources
from itertools import chain, groupby, islice, repeat
import os
from pathlib import Path, PurePath
import posixpath
from shutil import move as _move
import sys
from tempfile import mkdtemp
import time
from types import ModuleType
from typing import Any, Generic, Hashable, TypeVar, overload

from typing_extensions import ParamSpec

_T = TypeVar('_T')
_U = TypeVar('_U')
_THashable = TypeVar('_THashable', bound=Hashable)
_P = ParamSpec('_P')


if sys.version_info >= (3, 11):
    from enum import StrEnum as StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


if sys.version_info >= (3, 9):

    def read_resource_as_text(package: ModuleType, resource: str, encoding: str = 'utf-8') -> str:
        return importlib.resources.files(package).joinpath(resource).read_text(encoding)

else:

    def read_resource_as_text(package: ModuleType, resource: str, encoding: str = 'utf-8') -> str:
        return importlib.resources.read_text(package, resource, encoding=encoding)


if sys.version_info >= (3, 8):
    from functools import cached_property as cached_property
else:

    class cached_property(Generic[_T, _U]):
        def __init__(self, f: Callable[[_T], _U]) -> None:
            self.f = f

        @overload
        def __get__(self, o: None, t: type[_T] | None = ...) -> cached_property[_T, _U]:
            ...

        @overload
        def __get__(self, o: _T, t: type[_T] | None = ...) -> _U:
            ...

        def __get__(self, o: _T | None, t: type[_T] | None = None) -> cached_property[_T, _U] | _U:
            if o is None:
                return self
            else:
                o.__dict__[self.f.__name__] = v = self.f(o)
                return v


class TocReader:
    """Extracts key–value pairs from TOC files."""

    def __init__(self, contents: str) -> None:
        self.entries = {
            k: v
            for e in contents.splitlines()
            if e.startswith('##')
            for k, v in (map(str.strip, e.lstrip('#').partition(':')[::2]),)
            if k
        }

    def __getitem__(self, key: str | tuple[str, ...]) -> str | None:
        if isinstance(key, tuple):
            return next(filter(None, map(self.entries.get, key)), None)
        else:
            return self.entries.get(key)

    @classmethod
    def from_addon_path(cls, path: Path, suffix: str = '.toc') -> TocReader:
        return cls((path / (path.name + suffix)).read_text(encoding='utf-8-sig', errors='replace'))


def fill(it: Iterable[_T], fill: _T, length: int) -> Iterable[_T]:
    "Fill an iterable of specified length."
    return islice(chain(it, repeat(fill)), 0, length)


def bucketise(iterable: Iterable[_U], key: Callable[[_U], _T]) -> dict[_T, list[_U]]:
    "Place the elements of an iterable in a bucket according to ``key``."
    bucket: defaultdict[_T, list[_U]] = defaultdict(list)
    for value in iterable:
        bucket[key(value)].append(value)
    return dict(bucket)


def chain_dict(
    keys: Iterable[_T], default: _U, *overrides: Iterable[tuple[_T, _U]]
) -> dict[_T, _U]:
    "Construct a dictionary from a series of two-tuple iterables with overlapping keys."
    return dict(chain(zip(keys, repeat(default)), *overrides))


def uniq(it: Iterable[_THashable]) -> list[_THashable]:
    "Deduplicate hashable items in an iterable maintaining insertion order."
    return list(dict.fromkeys(it))


def all_eq(it: Iterable[object]) -> bool:
    "Check that all elements of an iterable are equal."
    groups = groupby(it)
    return next(groups, True) and not next(groups, False)


def merge_intersecting_sets(it: Iterable[frozenset[_T]]) -> Iterator[frozenset[_T]]:
    "Recursively merge intersecting sets in a collection."
    many_sets = list(it)
    while many_sets:
        this_set = many_sets.pop(0)
        while True:
            for idx, other_set in enumerate(many_sets):
                if not this_set.isdisjoint(other_set):
                    this_set |= many_sets.pop(idx)
                    break
            else:
                break
        yield this_set


@overload
async def gather(it: Iterable[Awaitable[_U]], wrapper: None = None) -> list[_U]:
    ...


@overload
async def gather(
    it: Iterable[Awaitable[_U]],
    wrapper: Callable[[Awaitable[_U]], Awaitable[_T]],
) -> list[_T]:
    ...


async def gather(
    it: Iterable[Awaitable[object]], wrapper: Callable[..., Awaitable[object]] | None = None
) -> Sequence[object]:
    if wrapper is not None:
        it = map(wrapper, it)
    return await asyncio.gather(*it)


@overload
def run_in_thread(
    fn: type[list[object]],
) -> Callable[[Iterable[_U]], Awaitable[list[_U]]]:
    ...


@overload
def run_in_thread(fn: Callable[_P, _U]) -> Callable[_P, Awaitable[_U]]:
    ...


def run_in_thread(fn: Callable[..., object]) -> Callable[..., Awaitable[object]]:
    @wraps(fn)
    def wrapper(*args: object, **kwargs: object):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, partial(fn, *args, **kwargs))

    return wrapper


def tabulate(rows: Sequence[tuple[object, ...]], *, max_col_width: int = 60) -> str:
    "Produce an ASCII table from equal-length elements in a sequence."
    from textwrap import fill

    def apply_max_col_width(value: object):
        return fill(str(value), width=max_col_width, max_lines=1)

    def calc_resultant_col_widths(rows: Sequence[tuple[str, ...]]):
        cols = zip(*rows)
        return [max(map(len, c)) for c in cols]

    rows = [tuple(apply_max_col_width(i) for i in r) for r in rows]
    head, *tail = rows

    base_template = '  '.join(f'{{{{{{0}}{w}}}}}' for w in calc_resultant_col_widths(rows))
    row_template = base_template.format(':<')
    table = '\n'.join(
        (
            base_template.format(':^').format(*head),
            base_template.format('0:-<').format(''),
            *(row_template.format(*r) for r in tail),
        )
    )
    return table


def move(src: str | os.PathLike[str], dest: str | os.PathLike[str]) -> Any:
    return _move(
        os.fspath(src),  # See https://bugs.python.org/issue32689
        dest,
    )


def trash(paths: Collection[PurePath], *, dest: PurePath, missing_ok: bool = False) -> None:
    if not paths:
        return

    exc_classes = FileNotFoundError if missing_ok else ()

    first_path_name = next(iter(paths)).name
    parent_folder = mkdtemp(dir=dest, prefix=f'deleted-{first_path_name}-')

    for path in paths:
        try:
            move(path, dest=parent_folder)
        except exc_classes:
            pass


def shasum(*values: object) -> str:
    "Base-16-encode a string using SHA-256 truncated to 32 characters."
    from hashlib import sha256

    return sha256(''.join(map(str, filter(None, values))).encode()).hexdigest()[:32]


def is_not_stale(path: Path, ttl: timedelta) -> bool:
    "Check if a file is older than ``ttl``."
    return path.exists() and (
        (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)) < ttl
    )


def find_addon_zip_tocs(names: Iterable[str]) -> Iterator[tuple[str, str]]:
    "Find top-level folders in a list of ZIP member paths."
    for name in names:
        if name.count(posixpath.sep) == 1:
            head, tail = posixpath.split(name)
            if tail.startswith(head) and tail[-4:].lower() == '.toc':
                yield (name, head)


def make_zip_member_filter_fn(base_dirs: Set[str]) -> Callable[[str], bool]:
    "Filter out items which are not sub-paths of top-level folders in a ZIP."

    def is_subpath(name: str):
        head, sep, _ = name.partition(posixpath.sep)
        return head in base_dirs if sep else False

    return is_subpath


def is_file_uri(uri: str) -> bool:
    return uri.startswith('file://')


def file_uri_to_path(file_uri: str) -> str:
    "Convert a file URI to a path that works both on Windows and *nix."
    from urllib.parse import unquote

    unprefixed_path = unquote(file_uri[7:])  # len('file://')
    # A slash is prepended to the path even when there isn't one there
    # on Windows.  The ``PurePath`` instance will inherit from either
    # ``PurePosixPath`` or ``PureWindowsPath``; this will be a no-op on POSIX.
    if PurePath(unprefixed_path[1:]).drive:
        unprefixed_path = unprefixed_path[1:]
    return unprefixed_path


def extract_byte_range_offset(content_range: str):
    return int(content_range.replace('bytes ', '').partition('-')[0])


def normalise_names(replace_delim: str) -> Callable[[str], str]:
    import string

    trans_table = str.maketrans(dict.fromkeys(string.punctuation, ' '))

    def normalise(value: str):
        return replace_delim.join(value.casefold().translate(trans_table).split())

    return normalise


def reveal_folder(path: str | os.PathLike[str]) -> None:
    if sys.platform == 'win32':
        os.startfile(path, 'explore')
    else:
        import click

        click.launch(os.fspath(path), locate=True)


@contextmanager
def time_op(on_complete: Callable[[float], None]) -> Iterator[None]:
    start = time.perf_counter()
    yield
    on_complete(time.perf_counter() - start)

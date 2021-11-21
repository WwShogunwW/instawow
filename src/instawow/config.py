from __future__ import annotations

from collections.abc import Iterable, Set
import json
import os
from pathlib import Path, PurePath
from tempfile import gettempdir

import click
from loguru import logger
from pydantic import BaseSettings, Field, PydanticValueError, validator
from pydantic.env_settings import SettingsSourceCallable

from .common import Flavour
from .utils import trash


class _PathNotWritableDirectoryError(PydanticValueError):
    code = 'path.not_writable_directory'
    msg_template = '"{path}" is not a writable directory'


def _expand_path(value: Path):
    return Path(os.path.abspath(os.path.expanduser(value)))


def _is_writable_dir(value: Path):
    return value.is_dir() and os.access(value, os.W_OK)


def _ensure_dirs(dirs: Iterable[Path]):
    for dir_ in dirs:
        dir_.mkdir(exist_ok=True, parents=True)


def _write_config(config: BaseConfig, fields: Set[str]):
    json_output = config.json(include=fields, indent=2)
    config.config_file.write_text(json_output, encoding='utf-8')


def _read_config(config: BaseConfig, missing_ok: bool = False):
    try:
        return json.loads(config.config_file.read_bytes())
    except FileNotFoundError:
        if missing_ok:
            default_config: dict[str, object] = {}
            return default_config
        raise


def _customise_sources(
    init_settings: SettingsSourceCallable,
    env_settings: SettingsSourceCallable,
    file_secret_settings: SettingsSourceCallable,
):
    # Prioritise env vars
    return (env_settings, init_settings)


class BaseConfig(
    BaseSettings,
    env_prefix='INSTAWOW_',
    customise_sources=_customise_sources,
):
    @property
    def config_file(self) -> Path:
        raise NotImplementedError


class _GlobalConfig(BaseConfig):
    config_dir: Path = Field(default_factory=lambda: Path(click.get_app_dir('instawow')))
    temp_dir: Path = Field(default_factory=lambda: Path(gettempdir(), 'instawow'))
    auto_update_check: bool = True

    @validator('config_dir', 'temp_dir')
    def _expand_path(cls, value: Path) -> Path:
        return _expand_path(value)

    def list_profiles(self) -> list[str]:
        "Get the names of the profiles contained in ``config_dir``."
        profiles = [c.parent.name for c in self.config_dir.glob('profiles/*/config.json')]
        return profiles

    def ensure_dirs(self) -> _GlobalConfig:
        _ensure_dirs(
            [
                self.config_dir,
                self.temp_dir,
                self.cache_dir,
            ]
        )
        return self

    def write(self) -> _GlobalConfig:
        _write_config(self, set())
        return self

    @property
    def cache_dir(self) -> Path:
        return self.temp_dir / 'cache'

    @property
    def config_file(self) -> Path:
        return self.config_dir / 'config.json'


class Config(BaseConfig):
    global_config: _GlobalConfig
    profile: str = Field(min_length=1, strip_whitespace=True)
    addon_dir: Path
    game_flavour: Flavour

    @validator('addon_dir')
    def _validate_path_is_writable_dir(cls, value: Path) -> Path:
        value = _expand_path(value)
        if not _is_writable_dir(value):
            raise _PathNotWritableDirectoryError(path=value)
        return value

    @staticmethod
    def infer_flavour(folder: os.PathLike[str] | str) -> Flavour:
        tail = tuple(map(str.casefold, PurePath(folder).parts[-3:]))
        if len(tail) != 3 or tail[1:] != ('interface', 'addons'):
            return Flavour.retail
        elif tail[0] in {'_classic_era_', '_classic_era_ptr_'}:
            return Flavour.vanilla_classic
        elif tail[0] in {'_classic_', '_classic_beta_', '_classic_ptr_'}:
            return Flavour.burning_crusade_classic
        else:
            return Flavour.retail

    @classmethod
    def get_dummy_config(cls, **kwargs: object) -> Config:
        "Create a dummy configuration with default values."
        values = {
            'global_config': _GlobalConfig(),
            'profile': '__novalidate__',
            'addon_dir': '__novalidate__',
            'game_flavour': Flavour.retail,
            **kwargs,
        }
        dummy_config = cls.construct(**values)
        return dummy_config

    @classmethod
    def read(cls, profile: str) -> Config:
        "Read the configuration from disk."
        dummy_config = cls.get_dummy_config(profile=profile)
        config = cls(
            global_config=_read_config(dummy_config.global_config, missing_ok=True),
            **_read_config(dummy_config),
        )
        return config

    def ensure_dirs(self) -> Config:
        "Create folders used by instawow."
        self.global_config.ensure_dirs()
        _ensure_dirs(
            [
                self.profile_dir,
                self.logging_dir,
                self.plugin_dir,
            ]
        )
        return self

    def write(self) -> Config:
        """Write the configuration on disk.

        ``write``, unlike ``ensure_dirs``, should only be called when configuring
        instawow.  This means that environment overrides should only be persisted
        if made during configuration.
        """
        self.ensure_dirs()
        self.global_config.write()
        _write_config(self, {'addon_dir', 'game_flavour', 'profile'})
        return self

    def delete(self) -> None:
        "Delete the configuration files associated with this profile."
        trash((self.profile_dir,), dest=self.global_config.temp_dir, missing_ok=True)

    @property
    def profile_dir(self) -> Path:
        return self.global_config.config_dir / 'profiles' / self.profile

    @property
    def logging_dir(self) -> Path:
        return self.profile_dir / 'logs'

    @property
    def plugin_dir(self) -> Path:
        return self.profile_dir / 'plugins'

    @property
    def config_file(self) -> Path:
        return self.profile_dir / 'config.json'

    @property
    def db_file(self) -> Path:
        return self.profile_dir / 'db.sqlite'


def setup_logging(
    config: Config, log_level: str = 'INFO', log_to_stderr: bool = False
) -> int | None:
    import logging

    class InterceptHandler(logging.Handler):  # pragma: no cover
        logging_filename = getattr(logging, '__file__', None)

        def emit(self, record: logging.LogRecord) -> None:
            # Get the corresponding Loguru level if it exists
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find caller from where the logged message originated
            frame = logging.currentframe()
            depth = 2
            while frame and frame.f_code.co_filename == self.logging_filename:
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[InterceptHandler()], level=log_level)

    if not log_to_stderr:
        (handler_id,) = logger.configure(
            handlers=[
                {
                    'sink': config.logging_dir / 'error.log',
                    'level': log_level,
                    'rotation': '5 MB',
                    'retention': 5,  # Number of log files to keep
                    'enqueue': True,
                },
            ]
        )
        return handler_id

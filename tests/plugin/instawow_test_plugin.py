from __future__ import annotations

from datetime import datetime, timezone

import click

import instawow.plugins
from instawow.common import ChangelogFormat, Defn, SourceMetadata
from instawow.resolvers import BaseResolver, PkgCandidate


@click.command
def foo():
    "don't foo where you bar"
    print('success!')


class MyResolver(BaseResolver):
    metadata = SourceMetadata(
        id='me',
        name="It's me",
        strategies=frozenset(),
        changelog_format=ChangelogFormat.Markdown,
        addon_toc_key=None,
    )
    requires_access_token = None

    async def _resolve_one(self, defn: Defn, metadata: None) -> PkgCandidate:
        return PkgCandidate(
            source=self.metadata.id,
            id='bar',
            slug='bar',
            name='Bar',
            description='The quintessential bar add-on, brought to you by yours truly',
            url='http://example.com/',
            download_url='file:///...',
            date_published=datetime.now(timezone.utc),
            version='0',
            changelog_url='data:,',
        )


@instawow.plugins.hookimpl
def instawow_add_commands():
    return (foo,)


@instawow.plugins.hookimpl
def instawow_add_resolvers():
    return (MyResolver,)

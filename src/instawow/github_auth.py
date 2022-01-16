from __future__ import annotations

import asyncio

import aiohttp
from typing_extensions import TypedDict

CLIENT_ID = 'aa178904bdf5143e93ec'


class DeviceCodeResponse(TypedDict):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class AccessTokenResponse(TypedDict):
    access_token: str
    token_type: str
    scope: str


async def get_codes(web_client: aiohttp.ClientSession) -> DeviceCodeResponse:
    async with web_client.post(
        'https://github.com/login/device/code',
        headers={'Accept': 'application/json'},
        json={'client_id': CLIENT_ID},
        raise_for_status=True,
    ) as response:
        return await response.json()


async def poll_for_access_token(
    web_client: aiohttp.ClientSession, device_code: str, polling_interval: int = 5
) -> str:
    while True:
        async with web_client.post(
            'https://github.com/login/oauth/access_token',
            headers={'Accept': 'application/json'},
            json={
                'client_id': CLIENT_ID,
                'device_code': device_code,
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            },
            raise_for_status=True,
        ) as response:
            response_json = await response.json()
            if 'error' in response_json:
                if response_json['error'] == 'authorization_pending':
                    await asyncio.sleep(polling_interval)
                else:
                    raise ValueError('Authorization failed', response_json)
            else:
                return response_json['access_token']

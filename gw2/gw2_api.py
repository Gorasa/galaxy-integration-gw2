# (c) 2019-2020 Mikhail Paulyshka
# SPDX-License-Identifier: MIT

import asyncio
import logging
import json
import os
import random
import string
import sys
import pprint
import threading
from urllib.parse import parse_qs
from typing import Dict, Iterable, List, Set, Tuple

import common.mglx_http

from .gw2_constants import GW2AuthorizationResult

class GW2API(object):

    API_DOMAIN = 'https://api.guildwars2.com'

    API_URL_ACHIEVEMENTS = '/v2/achievements'
    API_URL_ACCOUNT = '/v2/account'
    API_URL_ACCOUNT_ACHIVEMENTS = '/v2/account/achievements'

    LOCALSERVER_HOST = '127.0.0.1'
    LOCALSERVER_PORT = 13338

    #maximum number of ids per request for bulk endpoints
    API_IDS_PER_REQUEST = 200

    RETRIES_COUNT = 5
    RETRY_DELAY = 2
    RETRY_STATUSES = (0, 408, 429, 500, 502, 503, 504)

    def __init__(self, plugin_version):
        self.__http = common.mglx_http.MglxHttp(user_agent='gog_gw2/%s' % plugin_version)
        self.__logger = logging.getLogger('gw2_api')

        self._api_key = None
        self._account_info = None

    async def shutdown(self):
        await self.__http.shutdown()

    # 
    # Getters
    #

    def get_api_key(self) -> str:
        return self._api_key

    def get_account_id(self) -> str:
        if self._account_info is None:
            self.__logger.error('get_account_id: account info is None', exc_info=True)
            return None

        return self._account_info['id']

    def get_account_name(self) -> str:
        if self._account_info is None:
            self.__logger.error('get_account_name: account info is None', exc_info=True)
            return None

        return self._account_info['name']

    def get_owned_games(self) -> List[str]:
        if self._account_info is None:
            self.__logger.error('get_owned_games: account info is None', exc_info=True)
            return list()

        return self._account_info['access']

    def get_account_age(self) -> int:
        if self._account_info is None:
            self.__logger.error('get_account_age: account info is None', exc_info=True)
            return None

        if 'age' not in self._account_info:
            return 0

        return self._account_info['age']

    async def get_account_achievements(self) -> List[int]:
        result = list()

        if not self._api_key:
            self.__logger.error('get_account_achievements: api_key is None', exc_info=True)
            return result

        (status, achievements_account) = await self.__api_get_response(self._api_key, self.API_URL_ACCOUNT_ACHIVEMENTS)
        if status != 200:
            self.__logger.warning('get_account_achievements: failed to get achievements %s' % status)
            return result

        for achievement in achievements_account:
            if achievement['done'] == True:
                result.append(achievement['id'])

        return result

    async def get_achievement_names(self, achievement_ids: Iterable[int]) -> Tuple[Dict[int, str], Set[int]]:
        '''
        returns names of the given achievements and the ids which could not be requested because of errors
        '''
        names = dict()
        failed = set()

        ids = list(achievement_ids)
        for i in range(0, len(ids), self.API_IDS_PER_REQUEST):
            chunk = ids[i:i + self.API_IDS_PER_REQUEST]
            (status, achievements) = await self.__api_get_response(self._api_key, self.API_URL_ACHIEVEMENTS, {'ids': ','.join(str(x) for x in chunk)})

            #404 means that none of the requested ids exists
            if status == 404:
                continue

            #206 means that some of the requested ids do not exist
            if status not in (200, 206) or not isinstance(achievements, list):
                self.__logger.warning('get_achievement_names: failed to get achievements info %s' % status)
                failed.update(chunk)
                continue

            for achievement in achievements:
                names[achievement['id']] = achievement['name']

        return (names, failed)

    #
    # Authorization server
    #

    async def do_auth_apikey(self, api_key : str) -> GW2AuthorizationResult:
        self._api_key = None
        self._account_info = None

        if not api_key: 
            self.__logger.warning('do_auth_apikey: api_key is is None')
            return GW2AuthorizationResult.FAILED

        (status_code, account_info) = await self.__api_get_response(api_key, self.API_URL_ACCOUNT)
 
        if status_code != 200:
            if (account_info is not None) and ('text' in account_info):
                if account_info['text'] == 'Invalid access token':
                    return GW2AuthorizationResult.FAILED_INVALID_TOKEN
                elif account_info['text'] == 'invalid key':
                    return GW2AuthorizationResult.FAILED_INVALID_KEY
                elif account_info['text'] == 'no game account':
                    return GW2AuthorizationResult.FAILED_NO_ACCOUNT
                elif account_info['text'] == 'ErrBadData':
                    return GW2AuthorizationResult.FAILED_BAD_DATA
                elif account_info['text'] == 'ErrTimeout':
                    return GW2AuthorizationResult.FAILED_TIMEOUT
                else:
                    self.__logger.error('do_auth_apikey: unknown error description %s, %s' % (status_code, account_info))

            self.__logger.warning('do_auth_apikey: %s, %s' % (status_code, account_info))
            return GW2AuthorizationResult.FAILED

        if account_info is None:
            self.__logger.warning('do_auth_apikey: account info is None')
            return GW2AuthorizationResult.FAILED

        self._api_key = api_key
        self._account_info = account_info
        return GW2AuthorizationResult.FINISHED


    async def __api_get_response(self, api_key, url, parameters = None):
        #update authorization header
        if api_key:
            self.__http.update_headers({'Authorization': 'Bearer ' + api_key})

        status = 0
        result = None
        for attempt in range(1, self.RETRIES_COUNT + 1):
            #send request
            try:
                resp = await self.__http.request_get(self.API_DOMAIN+url, params=parameters)
            except Exception:
                self.__logger.exception('__api_get_response: failed to perform GET request for url %s' % url)
                return (0, None)

            #parse response, error responses contain a JSON object with a 'text' field
            status = resp.status
            result = None
            if resp.text:
                try:
                    result = json.loads(resp.text)
                except ValueError:
                    if status in (200, 206):
                        self.__logger.exception('__api_get_response: failed to parse response, url=%s, status=%s, text=%s' % (url, status, resp.text))

            #retry on temporary errors
            retry = status in self.RETRY_STATUSES or (status == 400 and isinstance(result, dict) and result.get('text') == 'ErrTimeout')
            if retry and attempt < self.RETRIES_COUNT:
                self.__logger.warning('__api_get_response: temporary error, url=%s, status=%s, attempt=%s' % (url, status, attempt))
                await asyncio.sleep(self.RETRY_DELAY * attempt)
                continue

            if status not in (200, 206):
                self.__logger.warning('__api_get_response: error, url=%s, status=%s, text=%s' % (url, status, resp.text))
            break

        return (status, result)

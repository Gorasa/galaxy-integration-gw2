# (c) 2019-2020 Mikhail Paulyshka
# SPDX-License-Identifier: MIT

import asyncio
import logging
import json
import os
import re
import sys
import time
from typing import Any, List, Optional, Tuple
import webbrowser

#expand sys.path
thirdparty =  os.path.join(os.path.dirname(os.path.realpath(__file__)),'3rdparty_windows/')
if thirdparty not in sys.path and os.path.exists(thirdparty):
    sys.path.insert(0, thirdparty)

#read manifest
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json"), mode="r", encoding="utf-8") as manifest:
    manifest = json.load(manifest)

from galaxy.api.consts import OSCompatibility, Platform, LicenseType, LocalGameState
from galaxy.api.errors import BackendError, InvalidCredentials
from galaxy.api.plugin import Plugin, create_and_run_plugin
from galaxy.api.types import Achievement, Authentication, NextStep, Dlc, LicenseInfo, Game, GameTime, LocalGame
from galaxy.proc_tools import process_iter

import gw2.gw2_api
import gw2.gw2_authserver
import gw2.gw2_localgame

class GuildWars2Plugin(Plugin):
    """
    Guild Wars 2 Plugin for GOG Galaxy

    Implemented features:
      * ImportOwnedGames
      * ImportInstalledGames
      * LaunchGame
      * InstallGame
      * UninstallGame
      * ImportGameTime
      * ImportOSCompatibility
      * ImportAchievements
      * ImportLocalSize

    Missing features:
      * LaunchPlatformClient
      * ImportFriends
      * ImportUserPresence
      * ShutdownPlatformClient
      * ImportGameLibrarySettings
      * ImportSubscriptions
      * ImportSubscriptionGames
    """

    #constants
    GAME_ID = 'guild_wars_2'
    GAME_NAME = 'Guild Wars 2'
    SLEEP_CHECK_ACHIEVEMENTS = 1500

    DLC_NAMES = {
        'HeartOfThorns': 'Heart of Thorns',
        'PathOfFire': 'Path of Fire',
        'EndOfDragons': 'End of Dragons',
        'SecretsOfTheObscure': 'Secrets of the Obscure',
        'JanthirWilds': 'Janthir Wilds',
        'VisionsOfEternity': 'Visions of Eternity',
    }
    #Galaxy fails to upload large achievement lists to the GOG backend (1190 achievements never got
    #through, 200 did) and uploads only after its achievements import, not after single unlocks.
    #So not yet reported achievements are added to the import in batches of this size
    ACHIEVEMENTS_BATCH_SIZE = 200
    CACHE_KEY_REPORTED_ACHIEVEMENTS = 'achievements_reported_v2'
    #minimum time between two batches, so Galaxy can upload one batch before the next one is added
    ACHIEVEMENTS_BATCH_INTERVAL = 300
    SLEEP_CHECK_INSTANCES = 60
    SLEEP_CHECK_RUNNING = 5
    SLEEP_CHECK_RUNNING_ITER = 0.01


    def __init__(self, reader, writer, token):
        super().__init__(Platform(manifest['platform']), manifest['version'], reader, writer, token)

        self.__logger = logging.getLogger('plugin')

        self._gw2_api = gw2.gw2_api.GW2API(manifest['version'])
        self._game_instances = None

        self.__task_check_for_achievements = None
        self.__task_check_for_instances = None
        self._task_check_for_running  = None

        self._last_state = LocalGameState.None_
        self.__imported_achievements = None

        #achievements which were passed to Galaxy before, stored in the persistent cache
        self.__reported_achievements = set()
        self.__last_batch_time = 0

        #unlocked achievements which wait for one of the next batches, None until an import got achievements
        self.__achievements_backlog = None


        #achievement names, the offline DB is completed by names from the API
        self.__achievement_names = dict()
        self.__achievements_invalid = set()
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gw2/db/achievements.json"), mode="r", encoding="utf-8") as f:
                self.__achievement_names = {int(k): v for k, v in json.load(f).items()}
        except Exception:
            self.__logger.exception('__init__: failed to read achievements info DB')

    #
    # Authentication
    #

    async def authenticate(self, stored_credentials=None):
        #check stored credentials
        if stored_credentials:
            auth_result = await self._gw2_api.do_auth_apikey(stored_credentials['api_key'])
            if auth_result != gw2.gw2_api.GW2AuthorizationResult.FINISHED:
                self.__logger.warning('authenticate: stored credentials are invalid')
                raise InvalidCredentials()

            return Authentication(self._gw2_api.get_account_id(), self._gw2_api.get_account_name())

        #new auth
        self.__authserver = gw2.gw2_authserver.Gw2AuthServer(self._gw2_api)
        self.__logger.info('authenticate: no stored credentials')

        AUTH_PARAMS = {
            "window_title": "Login to Guild Wars 2",
            "window_width": 640,
            "window_height": 500,
            "start_uri": self.__authserver.get_uri(),
            "end_uri_regex": '.*finished'
        }
        if not await self.__authserver.start():
            self.__logger.error('authenticate: failed to start auth server', exc_info=True)
            raise BackendError()
        return NextStep("web_session", AUTH_PARAMS)


    async def pass_login_credentials(self, step, credentials, cookies):
        if self.__authserver is not None:
            await self.__authserver.shutdown()

        api_key = self._gw2_api.get_api_key()
        account_id = self._gw2_api.get_account_id()
        account_name = self._gw2_api.get_account_name()
        if (api_key is None) or (account_id is None) or (account_name is None):
            self.__logger.error('pass_login_credentials: invalid credentials')
            raise InvalidCredentials()

        self.store_credentials({'api_key': api_key})
        return Authentication(account_id, account_name)

    #
    # ImportOwnedGames
    #

    async def get_owned_games(self):
        free_to_play = False
        
        dlcs = list()
        for dlc in self._gw2_api.get_owned_games():
            if dlc == 'PlayForFree':
                free_to_play = True
                continue
            if dlc in ('GuildWars2', 'None'):
                continue

            dlcs.append(Dlc(dlc_id = dlc, dlc_title = self.__get_dlc_name(dlc), license_info = LicenseInfo(license_type = LicenseType.SinglePurchase)))

        license_type = LicenseType.SinglePurchase
        if free_to_play:
            license_type = LicenseType.FreeToPlay

        return [ Game(game_id = self.GAME_ID, game_title = self.GAME_NAME, dlcs = dlcs, license_info = LicenseInfo(license_type = license_type)) ]

    def __get_dlc_name(self, dlc_id: str) -> str:
        if dlc_id in self.DLC_NAMES:
            return self.DLC_NAMES[dlc_id]

        #unknown expansion, e.g. 'VisionsOfEternity' -> 'Visions of Eternity'
        words = re.findall('[A-Z][a-z]*|[a-z]+|[0-9]+', dlc_id)
        if not words:
            return dlc_id
        return ' '.join([words[0]] + [x.lower() if x.lower() in ('of', 'the', 'and') else x for x in words[1:]])

    #
    # ImportInstalledGames
    #

    async def get_local_games(self):
        self._game_instances = gw2.gw2_localgame.get_game_instances()
        if len(self._game_instances) == 0:
            self._last_state = LocalGameState.None_
            return []

        self._last_state = LocalGameState.Installed
        return [ LocalGame(game_id=self.GAME_ID, local_game_state = self._last_state) ]

    #
    # LaunchGame
    #

    async def launch_game(self, game_id):
        if game_id != self.GAME_ID:
            self.__logger.warning('plugin/launch_game: unknown game_id %s' % game_id)
            return
        
        try:
            self._game_instances[0].run_game()
        except FileNotFoundError:
            self.__logger.warning('plugin/launch_game: game executable is not found')
            self.update_local_game_status(LocalGame(game_id, LocalGameState.None_))

    #
    # InstallGame
    #

    async def install_game(self, game_id):
        if game_id != self.GAME_ID:
            self.__logger.warning('plugin/install_game: unknown game_id %s' % game_id)
            return
        webbrowser.open('https://account.arena.net/welcome')

    #
    # UninstallGame
    #

    async def uninstall_game(self, game_id):
        if game_id != self.GAME_ID:
            self.__logger.warning('plugin/uninstall_game: unknown game_id %s' % game_id)
            return
        try:
            self._game_instances[0].uninstall_game()
        except FileNotFoundError:
            self.__logger.warning('plugin/uninstall_game: game executable is not found')
            self.update_local_game_status(LocalGame(game_id, LocalGameState.None_))

    #
    # ImportGameTime
    #

    async def get_game_time(self, game_id, context):
        if game_id != self.GAME_ID:
            self.__logger.warning('plugin/get_game_time: unknown game_id %s' % game_id)
            return None

        time_played = int(self._gw2_api.get_account_age() / 60)
        last_played_time = self.__get_cached_time('last_played')

        return GameTime(game_id = game_id, time_played = time_played, last_played_time = last_played_time)

    #
    # ImportOSCompatibility
    #

    async def get_os_compatibility(self, game_id: str, context: Any) -> Optional[OSCompatibility]:      
        if game_id != self.GAME_ID:
            self.__logger.warning('plugin/get_game_time: unknown game_id %s' % game_id)
            return None

        return OSCompatibility.Windows

    #
    # ImportAchievements
    #

    async def get_unlocked_achievements(self, game_id: str, context: Any) -> List[Achievement]:
        if game_id != self.GAME_ID:
            self.__logger.warning('plugin/get_unlocked_achievements: unknown game_id %s' % game_id)
            return list()

        self.__imported_achievements = set()
        unlocked = await self.__get_unlocked_achievements()

        batch_size = 0
        if time.time() - self.__last_batch_time >= self.ACHIEVEMENTS_BATCH_INTERVAL:
            batch_size = self.ACHIEVEMENTS_BATCH_SIZE
            self.__last_batch_time = time.time()

        result = self.__import_achievements(unlocked, batch_size)

        #an empty list means that the API request failed (or that there are no achievements), keep the
        #previous backlog then, so that the background check does not report the backlog at once
        if unlocked:
            self.__achievements_backlog = {x for (x, _) in unlocked if x not in self.__reported_achievements}
        return result

    def __import_achievements(self, unlocked: List[Tuple[int, str]], batch_size: Optional[int]) -> List[Achievement]:
        '''
        returns the given achievements which were reported to Galaxy before plus up to batch_size (all if None)
        not yet reported ones, and marks them as imported
        '''
        cache_changed = False

        #the API does not provide unlock times, the time an achievement was seen first is used instead
        now = int(time.time())
        for (achievement_id, _) in unlocked:
            cache_key = 'achievement_%s' % achievement_id
            if cache_key not in self.persistent_cache:
                self.persistent_cache[cache_key] = now
                cache_changed = True

        #lowest ids first, they are most likely known to Galaxy
        new_ids = sorted(x for (x, _) in unlocked if x not in self.__reported_achievements)[:batch_size]
        if new_ids:
            self.__reported_achievements.update(new_ids)
            self.persistent_cache[self.CACHE_KEY_REPORTED_ACHIEVEMENTS] = ','.join(str(x) for x in sorted(self.__reported_achievements))
            cache_changed = True

            pending = len([x for (x, _) in unlocked if x not in self.__reported_achievements])
            self.__logger.info('__import_achievements: reporting %s new achievements, %s left for the next imports' % (len(new_ids), pending))

        result = list()
        for (achievement_id, achievement_name) in unlocked:
            if achievement_id not in self.__reported_achievements:
                continue

            self.__imported_achievements.add(achievement_id)

            #the Galaxy API defines achievement ids as strings
            result.append(Achievement(self.__get_cached_time('achievement_%s' % achievement_id), str(achievement_id), achievement_name))

        if cache_changed:
            self.push_cache()

        return result

    async def __get_unlocked_achievements(self) -> List[Tuple[int, str]]:
        '''
        returns id and name of unlocked account achievements
        '''
        achievement_ids = await self._gw2_api.get_account_achievements()
        await self.__resolve_achievement_names(achievement_ids)

        #achievements without known name are skipped, they will be retried on next check
        return [(x, self.__achievement_names[x]) for x in achievement_ids if x in self.__achievement_names]

    def __get_cached_time(self, cache_key: str) -> Optional[int]:
        '''
        returns a timestamp from the persistent cache, Galaxy stores cache values as strings
        '''
        try:
            return int(self.persistent_cache[cache_key])
        except (KeyError, TypeError, ValueError):
            return None

    async def __resolve_achievement_names(self, achievement_ids: List[int]) -> None:
        '''
        requests names of achievements which are missing in the offline DB (e.g. added after the DB was created)
        '''
        unknown_ids = [x for x in achievement_ids if x not in self.__achievement_names and x not in self.__achievements_invalid]
        if not unknown_ids:
            return

        (names, failed_ids) = await self._gw2_api.get_achievement_names(unknown_ids)
        self.__achievement_names.update(names)

        #ids which are unknown to the API as well are never imported
        self.__achievements_invalid.update(x for x in unknown_ids if x not in names and x not in failed_ids)
        self.__logger.info('__resolve_achievement_names: requested %s, resolved %s, failed %s' % (len(unknown_ids), len(names), len(failed_ids)))

    #
    # ImportLocalSize
    #

    async def get_local_size(self, game_id: str, context: Any) -> Optional[int]:   
        if game_id != self.GAME_ID:
            self.__logger.warning('plugin/get_local_size: unknown game_id %s' % game_id)
            return None

        if not self._game_instances:
            return None

        return await self._game_instances[0].get_app_size()

    #
    # Other
    #

    def handshake_complete(self) -> None:
        #drop unlock times of the former legendary armory pseudo achievements (ids 'legendary_<item id>[_<copy>]'
        #and 1000000000 + item id * 100 + copy), regular achievement ids are far below that range
        stale_keys = [x for x in self.persistent_cache if re.fullmatch(r'achievement_(legendary_.*|[0-9]{10,})', x)]
        for key in stale_keys:
            del self.persistent_cache[key]
        if stale_keys:
            self.__logger.info('handshake_complete: removed %s legendary unlock times from the cache' % len(stale_keys))
            self.push_cache()

        #reported achievements of a development version which added batches without Galaxy uploading them,
        #starting over costs a few imports in which Galaxy skips the already uploaded achievements
        if self.persistent_cache.pop('achievements_reported', None) is not None:
            self.__logger.info('handshake_complete: reset reported achievements')
            self.push_cache()

        try:
            self.__reported_achievements = {int(x) for x in self.persistent_cache.get(self.CACHE_KEY_REPORTED_ACHIEVEMENTS, '').split(',') if x}
        except ValueError:
            self.__logger.exception('handshake_complete: failed to read reported achievements')

    def tick(self):
        if not self._task_check_for_running or self._task_check_for_running.done():
            self._task_check_for_running = self.create_task(self.task_check_for_running_func(), "task_check_for_running_game")

        if not self.__task_check_for_instances or self.__task_check_for_instances.done():
            self.__task_check_for_instances = self.create_task(self.task_check_for_game_instances(), "task_check_for_instances")

        if not self.__task_check_for_achievements or self.__task_check_for_achievements.done():
            self.__task_check_for_achievements = self.create_task(self.task_check_for_achievements(), "task_check_for_achievements")

    async def shutdown(self) -> None:
        await self._gw2_api.shutdown()

    #
    # Internals
    #

    async def task_check_for_achievements(self):
        if self.__imported_achievements is not None and self.__achievements_backlog is not None:
            #only achievements unlocked since the last import, the backlog is added by the next imports
            unlocked = [x for x in await self.__get_unlocked_achievements() if x[0] not in self.__imported_achievements and x[0] not in self.__achievements_backlog]
            for achievement in self.__import_achievements(unlocked, None):
                self.unlock_achievement(self.GAME_ID, achievement)

        await asyncio.sleep(self.SLEEP_CHECK_ACHIEVEMENTS)


    async def task_check_for_game_instances(self):
        self._game_instances = gw2.gw2_localgame.get_game_instances()
        await asyncio.sleep(self.SLEEP_CHECK_INSTANCES)


    async def task_check_for_running_func(self):

        #skip status update if there is no instances
        if self._last_state == LocalGameState.None_ and not self._game_instances:
            await asyncio.sleep(self.SLEEP_CHECK_RUNNING)
            return

        #get exe names
        target_exes = list()
        for instance in self._game_instances:
            target_exes.append(instance.exe_name().lower())

        #check processes
        running = False
        if target_exes:    
            for proc_info in process_iter():
                if proc_info.binary_path is None:
                    continue
                if os.path.basename(proc_info.binary_path).lower() in target_exes:
                    running = True
                    break
                await asyncio.sleep(self.SLEEP_CHECK_RUNNING_ITER)

        #update state
        new_state = None
        if running:
            self.persistent_cache['last_played'] = int(time.time())
            self.push_cache()
            new_state = LocalGameState.Installed | LocalGameState.Running
        elif target_exes:
            new_state = LocalGameState.Installed
        else:
            new_state = LocalGameState.None_

        if self._last_state != new_state:
            self.update_local_game_status(LocalGame('guild_wars_2', new_state))
            self._last_state = new_state

        await asyncio.sleep(self.SLEEP_CHECK_RUNNING)


def main():
    create_and_run_plugin(GuildWars2Plugin, sys.argv)


if __name__ == "__main__":
    main()

import os
import sys
import time
import shlex
import shutil
import random
import inspect
import logging
import asyncio
import pathlib
import traceback
import math
import re

import aiohttp
import discord
import colorlog

from io import BytesIO, StringIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from collections import defaultdict

from discord.enums import ChannelType

from . import exceptions
from . import downloader

from .playlist import Playlist
from .player import MusicPlayer
from .entry import StreamPlaylistEntry
from .opus_loader import load_opus_lib
from .config import Config, ConfigDefaults
from .permissions import Permissions, PermissionsDefaults
from .aliases import Aliases, AliasesDefault
from .constructs import SkipState, Response
from .utils import load_file, write_file, fixg, ftimedelta, _func_, _get_variable
from .spotify import Spotify
from .json import Json

from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH

load_opus_lib()

log = logging.getLogger(__name__)


class MusicBot(discord.Client):
    def __init__(self, config_file=None, perms_file=None, aliases_file=None):
        try:
            sys.stdout.write("\x1b]2;MusicBot {}\x07".format(BOTVERSION))
        except:
            pass

        print()

        if config_file is None:
            config_file = ConfigDefaults.options_file

        if perms_file is None:
            perms_file = PermissionsDefaults.perms_file

        if aliases_file is None:
            aliases_file = AliasesDefault.aliases_file

        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None

        self.config = Config(config_file)

        self._setup_logging()

        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])
        self.str = Json(self.config.i18n_file)

        if self.config.usealias:
            self.aliases = Aliases(aliases_file)

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)

        self.aiolocks = defaultdict(asyncio.Lock)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        log.info('Starting MusicBot {}'.format(BOTVERSION))

        if not self.autoplaylist:
            log.warning("Autoplaylist is empty, disabling.")
            self.config.auto_playlist = False
        else:
            log.info("Loaded autoplaylist with {} entries".format(len(self.autoplaylist)))

        if self.blacklist:
            log.debug("Loaded blacklist with {} entries".format(len(self.blacklist)))

        ssd_defaults = {
            'last_np_msg': None,
            'auto_paused': False,
            'availability_paused': False
        }
        self.server_specific_data = defaultdict(ssd_defaults.copy)

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

        self.spotify = None
        if self.config._spotify:
            try:
                self.spotify = Spotify(self.config.spotify_clientid, self.config.spotify_clientsecret,
                                       aiosession=self.aiosession, loop=self.loop)
                if not self.spotify.token:
                    log.warning('Spotify did not provide us with a token. Disabling.')
                    self.config._spotify = False
                else:
                    log.info('Authenticated with Spotify successfully using client ID and secret.')
            except exceptions.SpotifyError as e:
                log.warning(
                    'There was a problem initialising the connection to Spotify. Is your client ID and secret correct? Details: {0}. Continuing anyway in 5 seconds...'.format(
                        e))
                self.config._spotify = False
                time.sleep(5)

    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("Only the owner can use this command.", expire_in=30)

        return wrapper

    def dev_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            orig_msg = _get_variable('message')

            if str(orig_msg.author.id) in self.config.dev_ids:
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("Only dev users can use this command.", expire_in=30)

        wrapper.dev_cmd = True
        return wrapper

    def ensure_appinfo(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            await self._cache_app_info()
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(self, *, server=None, voice=False):
        return discord.utils.find(
            lambda m: m.id == self.config.owner_id and (m.voice if voice else True),
            server.members if server else self.get_all_members()
        )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self):
        if len(logging.getLogger(__package__).handlers) > 1:
            log.debug("Skipping logger setup, already set up")
            return

        shandler = logging.StreamHandler(stream=sys.stdout)
        shandler.setFormatter(colorlog.LevelFormatter(
            fmt={
                'DEBUG': '{log_color}[{levelname}:{module}] {message}',
                'INFO': '{log_color}{message}',
                'WARNING': '{log_color}{levelname}: {message}',
                'ERROR': '{log_color}[{levelname}:{module}] {message}',
                'CRITICAL': '{log_color}[{levelname}:{module}] {message}',

                'EVERYTHING': '{log_color}[{levelname}:{module}] {message}',
                'NOISY': '{log_color}[{levelname}:{module}] {message}',
                'VOICEDEBUG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}',
                'FFMPEG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}'
            },
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'white',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'bold_red',

                'EVERYTHING': 'white',
                'NOISY': 'white',
                'FFMPEG': 'bold_purple',
                'VOICEDEBUG': 'purple',
            },
            style='{',
            datefmt=''
        ))
        shandler.setLevel(self.config.debug_level)
        logging.getLogger(__package__).addHandler(shandler)

        log.debug("Set logging level to {}".format(self.config.debug_level_str))

        if self.config.debug_mode:
            dlogger = logging.getLogger('discord')
            dlogger.setLevel(logging.DEBUG)
            dhandler = logging.FileHandler(filename='logs/discord.log', encoding='utf-8', mode='w')
            dhandler.setFormatter(logging.Formatter('{asctime}:{levelname}:{name}: {message}', style='{'))
            dlogger.addHandler(dhandler)

    @staticmethod
    def _check_if_empty(vchannel: discord.abc.GuildChannel, *, excluding_me=True, excluding_deaf=False):
        def check(member):
            if excluding_me and member == vchannel.guild.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            if member.bot:
                return False

            return True

        return not sum(1 for m in vchannel.members if check(m))

    async def _join_startup_channels(self, channels, *, autosummon=True):
        joined_servers = set()
        channel_map = {c.guild: c for c in channels}

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("Initial autopause in empty channel")

                player.pause()
                self.server_specific_data[player.voice_client.channel.guild]['auto_paused'] = True

        for guild in self.guilds:
            if guild.unavailable or guild in channel_map:
                continue

            if guild.me.voice:
                log.info("Found resumable voice channel {0.guild.name}/{0.name}".format(guild.me.voice.channel))
                channel_map[guild] = guild.me.voice.channel

            if autosummon:
                owner = self._get_owner(server=guild, voice=True)
                if owner:
                    log.info("Found owner in \"{}\"".format(owner.voice.channel.name))
                    channel_map[guild] = owner.voice.channel

        for guild, channel in channel_map.items():
            if guild in joined_servers:
                log.info("Already joined a channel in \"{}\", skipping".format(guild.name))
                continue

            if channel and isinstance(channel, discord.VoiceChannel):
                log.info("Attempting to join {0.guild.name}/{0.name}".format(channel))

                chperms = channel.permissions_for(guild.me)

                if not chperms.connect:
                    log.info("Cannot join channel \"{}\", no permission.".format(channel.name))
                    continue

                elif not chperms.speak:
                    log.info("Will not join channel \"{}\", no permission to speak.".format(channel.name))
                    continue

                try:
                    player = await self.get_player(channel, create=True, deserialize=self.config.persistent_queue)
                    joined_servers.add(guild)

                    log.info("Joined {0.guild.name}/{0.name}".format(channel))

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist:
                        if self.config.auto_pause:
                            player.once('play', lambda player, **_: _autopause(player))
                        if not player.playlist.entries:
                            await self.on_player_finished_playing(player)

                except Exception:
                    log.debug("Error joining {0.guild.name}/{0.name}".format(channel), exc_info=True)
                    log.error("Failed to join {0.guild.name}/{0.name}".format(channel))

            elif channel:
                log.warning("Not joining {0.guild.name}/{0.name}, that's a text channel.".format(channel))

            else:
                log.warning("Invalid channel thing: {}".format(channel))

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message, quiet=True)

    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        if msg.guild.me.voice:
            vc = msg.guild.me.voice.channel
        else:
            vc = None

        if not vc or (msg.author.voice and vc == msg.author.voice.channel):
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def _cache_app_info(self, *, update=False):
        if not self.cached_app_info and not update and self.user.bot:
            log.debug("Caching app info")
            self.cached_app_info = await self.application_info()

        return self.cached_app_info

    async def remove_from_autoplaylist(self, song_url: str, *, ex: Exception = None, delete_from_ap=False):
        if song_url not in self.autoplaylist:
            log.debug("URL \"{}\" not in autoplaylist, ignoring".format(song_url))
            return

        async with self.aiolocks[_func_()]:
            self.autoplaylist.remove(song_url)
            log.info("Removing unplayable song from session autoplaylist: %s" % song_url)

            with open(self.config.auto_playlist_removed_file, 'a', encoding='utf8') as f:
                f.write(
                    '# Entry removed {ctime}\n'
                    '# Reason: {ex}\n'
                    '{url}\n\n{sep}\n\n'.format(
                        ctime=time.ctime(),
                        ex=str(ex).replace('\n', '\n#' + ' ' * 10),
                        url=song_url,
                        sep='#' * 32
                    ))

            if delete_from_ap:
                log.info("Updating autoplaylist")
                write_file(self.config.auto_playlist_file, self.autoplaylist)

    @ensure_appinfo
    async def generate_invite_link(self, *, permissions=discord.Permissions(70380544), guild=None):
        return discord.utils.oauth_url(self.cached_app_info.id, permissions=permissions, guild=guild)

    async def get_voice_client(self, channel: discord.abc.GuildChannel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if not isinstance(channel, discord.VoiceChannel):
            raise AttributeError('Channel passed must be a voice channel')

        if channel.guild.voice_client:
            return channel.guild.voice_client
        else:
            return await channel.connect(timeout=60, reconnect=True)

    async def disconnect_voice_client(self, guild):
        vc = self.voice_client_in(guild)
        if not vc:
            return

        if guild.id in self.players:
            self.players.pop(guild.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.channel.guild)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        await self.ws.voice_state(vchannel.guild.id, vchannel.id, mute, deaf)

    def get_player_in(self, guild: discord.Guild) -> MusicPlayer:
        return self.players.get(guild.id)

    async def get_player(self, channel, create=False, *, deserialize=False) -> MusicPlayer:
        guild = channel.guild

        async with self.aiolocks[_func_() + ':' + str(guild.id)]:
            if deserialize:
                voice_client = await self.get_voice_client(channel)
                player = await self.deserialize_queue(guild, voice_client)

                if player:
                    log.debug("Created player via deserialization for guild %s with %s entries", guild.id,
                              len(player.playlist))
                    return self._init_player(player, guild=guild)

            if guild.id not in self.players:
                if not create:
                    raise exceptions.CommandError(
                        'The bot is not in a voice channel.  '
                        'Use %ssummon to summon it to your voice channel.' % self.config.command_prefix)

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self)
                player = MusicPlayer(self, voice_client, playlist)
                self._init_player(player, guild=guild)

        return self.players[guild.id]

    def _init_player(self, player, *, guild=None):
        player = player.on('play', self.on_player_play) \
            .on('resume', self.on_player_resume) \
            .on('pause', self.on_player_pause) \
            .on('stop', self.on_player_stop) \
            .on('finished-playing', self.on_player_finished_playing) \
            .on('entry-added', self.on_player_entry_added) \
            .on('error', self.on_player_error)

        player.skip_state = SkipState()

        if guild:
            self.players[guild.id] = player

        return player

    async def on_player_play(self, player, entry):
        log.debug('Running on_player_play')
        await self.update_now_playing_status(entry)
        player.skip_state.reset()

        await self.serialize_queue(player.voice_client.channel.guild)

        if self.config.write_current_song:
            await self.write_current_song(player.voice_client.channel.guild, entry)

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            author_perms = self.permissions.for_user(author)

            if author not in player.voice_client.channel.members and author_perms.skip_when_absent:
                newmsg = 'Skipping next song in `%s`: `%s` added by `%s` as queuer not in voice' % (
                    player.voice_client.channel.name, entry.title, entry.meta['author'].name)
                player.skip()
            elif self.config.now_playing_mentions:
                newmsg = '%s - your song `%s` is now playing in `%s`!' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = 'Now playing in `%s`: `%s` added by `%s`' % (
                    player.voice_client.channel.name, entry.title, entry.meta['author'].name)
        else:
            newmsg = 'Now playing automatically added entry `%s` in `%s`' % (
                entry.title, player.voice_client.channel.name)

        if newmsg:
            if self.config.dm_nowplaying and author:
                await self.safe_send_message(author, newmsg)
                return

            if self.config.no_nowplaying_auto and not author:
                return

            guild = player.voice_client.guild
            last_np_msg = self.server_specific_data[guild]['last_np_msg']

            if self.config.nowplaying_channels:
                for potential_channel_id in self.config.nowplaying_channels:
                    potential_channel = self.get_channel(potential_channel_id)
                    if potential_channel and potential_channel.guild == guild:
                        channel = potential_channel
                        break

            if channel:
                pass
            elif not channel and last_np_msg:
                channel = last_np_msg.channel
            else:
                log.debug('no channel to put now playing message into')
                return

            self.server_specific_data[guild]['last_np_msg'] = await self.safe_send_message(channel, newmsg)


    async def on_player_resume(self, player, entry, **_):
        log.debug('Running on_player_resume')
        await self.update_now_playing_status(entry)

    async def on_player_pause(self, player, entry, **_):
        log.debug('Running on_player_pause')
        await self.update_now_playing_status(entry, True)

    async def on_player_stop(self, player, **_):
        log.debug('Running on_player_stop')
        await self.update_now_playing_status()

    async def on_player_finished_playing(self, player, **_):
        log.debug('Running on_player_finished_playing')

        if self.config.delete_nowplaying:
            guild = player.voice_client.guild
            last_np_msg = self.server_specific_data[guild]['last_np_msg']
            if last_np_msg:
                await self.safe_delete_message(last_np_msg)

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("Player finished playing, autopaused in empty channel")

                player.pause()
                self.server_specific_data[player.voice_client.channel.guild]['auto_paused'] = True

        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            if not player.autoplaylist:
                if not self.autoplaylist:
                    log.warning("No playable songs in the autoplaylist, disabling.")
                    self.config.auto_playlist = False
                else:
                    log.debug("No content in current autoplaylist. Filling with new music...")
                    player.autoplaylist = list(self.autoplaylist)

            while player.autoplaylist:
                if self.config.auto_playlist_random:
                    random.shuffle(player.autoplaylist)
                    song_url = random.choice(player.autoplaylist)
                else:
                    song_url = player.autoplaylist[0]
                player.autoplaylist.remove(song_url)

                info = {}

                try:
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False,
                                                              process=False)
                except downloader.youtube_dl.utils.DownloadError as e:
                    if 'YouTube said:' in e.args[0]:
                        log.error("Error processing youtube url:\n{}".format(e.args[0]))

                    else:
                        log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))

                    await self.remove_from_autoplaylist(song_url, ex=e, delete_from_ap=self.config.remove_ap)
                    continue

                except Exception as e:
                    log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))
                    log.exception()

                    self.autoplaylist.remove(song_url)
                    continue

                if info.get('entries', None):
                    log.debug("Playlist found but is unsupported at this time, skipping.")

                if self.config.auto_pause:
                    player.once('play', lambda player, **_: _autopause(player))

                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    log.error("Error adding song from autoplaylist: {}".format(e))
                    log.debug('', exc_info=True)
                    continue

                break

            if not self.autoplaylist:
                log.warning("No playable songs in the autoplaylist, disabling.")
                self.config.auto_playlist = False

        else:
            await self.serialize_queue(player.voice_client.channel.guild)

        if not player.is_stopped and not player.is_dead:
            player.play(_continue=True)

    async def on_player_entry_added(self, player, playlist, entry, **_):
        log.debug('Running on_player_entry_added')
        if entry.meta.get('author') and entry.meta.get('channel'):
            await self.serialize_queue(player.voice_client.channel.guild)

    async def on_player_error(self, player, entry, ex, **_):
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nError from FFmpeg:\n{}\n```".format(ex)
            )
        else:
            log.exception("Player error", exc_info=ex)

    async def update_now_playing_status(self, entry=None, is_paused=False):
        game = None

        if not self.config.status_message:
            if self.user.bot:
                activeplayers = sum(1 for p in self.players.values() if p.is_playing)
                if activeplayers > 1:
                    game = discord.Game(type=0, name="music on %s guilds" % activeplayers)
                    entry = None

                elif activeplayers == 1:
                    player = discord.utils.get(self.players.values(), is_playing=True)
                    entry = player.current_entry

            if entry:
                prefix = u'\u275A\u275A ' if is_paused else ''

                name = u'{}{}'.format(prefix, entry.title)[:128]
                game = discord.Game(type=0, name=name)
        else:
            game = discord.Game(type=0, name=self.config.status_message.strip()[:128])

        async with self.aiolocks[_func_()]:
            if game != self.last_status:
                await self.change_presence(activity=game)
                self.last_status = game

    async def update_now_playing_message(self, guild, message, *, channel=None):
        lnp = self.server_specific_data[guild]['last_np_msg']
        m = None

        if message is None and lnp:
            await self.safe_delete_message(lnp, quiet=True)

        elif lnp:
            oldchannel = lnp.channel

            if lnp.channel == oldchannel:
                async for lmsg in lnp.channel.history(limit=1):
                    if lmsg != lnp and lnp:
                        await self.safe_delete_message(lnp, quiet=True)
                        m = await self.safe_send_message(channel, message, quiet=True)
                    else:
                        m = await self.safe_edit_message(lnp, message, send_if_fail=True, quiet=False)

            elif channel:
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(channel, message, quiet=True)

            else:
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(oldchannel, message, quiet=True)

        elif channel:
            m = await self.safe_send_message(channel, message, quiet=True)

        self.server_specific_data[guild]['last_np_msg'] = m

    async def serialize_queue(self, guild, *, dir=None):

        player = self.get_player_in(guild)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/queue.json' % guild.id

        async with self.aiolocks['queue_serialization' + ':' + str(guild.id)]:
            log.debug("Serializing queue for %s", guild.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None):
        coros = [self.serialize_queue(s, dir=dir) for s in self.guilds]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(self, guild, voice_client, playlist=None, *, dir=None) -> MusicPlayer:

        if playlist is None:
            playlist = Playlist(self)

        if dir is None:
            dir = 'data/%s/queue.json' % guild.id

        async with self.aiolocks['queue_serialization' + ':' + str(guild.id)]:
            if not os.path.isfile(dir):
                return None

            log.debug("Deserializing queue for %s", guild.id)

            with open(dir, 'r', encoding='utf8') as f:
                data = f.read()

        return MusicPlayer.from_json(data, self, voice_client, playlist)

    async def write_current_song(self, guild, entry, *, dir=None):
        player = self.get_player_in(guild)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/current.txt' % guild.id

        async with self.aiolocks['current_song' + ':' + str(guild.id)]:
            log.debug("Writing current song for %s", guild.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(entry.title)

    @ensure_appinfo
    async def _on_ready_sanity_checks(self):
        await self._scheck_ensure_env()

        await self._scheck_server_permissions()

        await self._scheck_autoplaylist()

        await self._scheck_configs()

    async def _scheck_ensure_env(self):
        log.debug("Ensuring data folders exist")
        for guild in self.guilds:
            pathlib.Path('data/%s/' % guild.id).mkdir(exist_ok=True)

        with open('data/server_names.txt', 'w', encoding='utf8') as f:
            for guild in sorted(self.guilds, key=lambda s: int(s.id)):
                f.write('{:<22} {}\n'.format(guild.id, guild.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                log.debug("Deleted old audio cache")
            else:
                log.debug("Could not delete old audio cache, moving on.")

    async def _scheck_server_permissions(self):
        log.debug("Checking server permissions")
        pass

    async def _scheck_autoplaylist(self):
        log.debug("Auditing autoplaylist")
        pass
    async def _scheck_configs(self):
        log.debug("Validating config")
        await self.config.async_validate(self)

        log.debug("Validating permissions config")
        await self.permissions.async_validate(self)


    async def safe_send_message(self, dest, content, **kwargs):
        tts = kwargs.pop('tts', False)
        quiet = kwargs.pop('quiet', False)
        expire_in = kwargs.pop('expire_in', 0)
        allow_none = kwargs.pop('allow_none', True)
        also_delete = kwargs.pop('also_delete', None)

        msg = None
        lfunc = log.debug if quiet else log.warning

        try:
            if content is not None or allow_none:
                if isinstance(content, discord.Embed):
                    msg = await dest.send(embed=content)
                else:
                    msg = await dest.send(content, tts=tts)

        except discord.Forbidden:
            lfunc("Cannot send message to \"%s\", no permission", dest.name)

        except discord.NotFound:
            lfunc("Cannot send message to \"%s\", invalid channel?", dest.name)

        except discord.HTTPException:
            if len(content) > DISCORD_MSG_CHAR_LIMIT:
                lfunc("Message is over the message size limit (%s)", DISCORD_MSG_CHAR_LIMIT)
            else:
                lfunc("Failed to send message")
                log.noise("Got HTTPException trying to send message to %s: %s", dest, content)

        finally:
            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await message.delete()

        except discord.Forbidden:
            lfunc("Cannot delete message \"{}\", no permission".format(message.clean_content))

        except discord.NotFound:
            lfunc("Cannot delete message \"{}\", message not found".format(message.clean_content))

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await message.edit(content=new)

        except discord.NotFound:
            lfunc("Cannot edit message \"{}\", message not found".format(message.clean_content))
            if send_if_fail:
                lfunc("Sending message instead")
                return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            return await destination.trigger_typing()
        except discord.Forbidden:
            log.warning("Could not send typing to {}, no permission".format(destination))

    async def restart(self):
        self.exit_signal = exceptions.RestartSignal()
        await self.logout()

    def restart_threadsafe(self):
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
            self.loop.run_until_complete(self.aiosession.close())
        except:
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except:
            pass

    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your token in the options file.  "
                "Remember that each field should be on their own line."
            )

        finally:
            try:
                self._cleanup()
            except Exception:
                log.error("Error in cleanup", exc_info=True)

            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            log.error("Exception in {}:\n{}".format(event, ex.message))

            await asyncio.sleep(2)
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            log.error("Exception in {}".format(event), exc_info=True)

    async def on_resumed(self):
        log.info("\nReconnected to discord.\n")

    async def on_ready(self):
        dlogger = logging.getLogger('discord')
        for h in dlogger.handlers:
            if getattr(h, 'terminator', None) == '':
                dlogger.removeHandler(h)
                print()

        log.debug("Connection established, ready to go.")

        self.ws._keep_alive.name = 'Gateway Keepalive'

        if self.init_ok:
            log.debug("Received additional READY event, may have failed to resume")
            return

        await self._on_ready_sanity_checks()

        self.init_ok = True


        log.info("Connected: {0}/{1}#{2}".format(
            self.user.id,
            self.user.name,
            self.user.discriminator
        ))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.guilds:
            log.info("Owner:     {0}/{1}#{2}\n".format(
                owner.id,
                owner.name,
                owner.discriminator
            ))

            log.info('Guild List:')
            unavailable_servers = 0
            for s in self.guilds:
                ser = ('{} (unavailable)'.format(s.name) if s.unavailable else s.name)
                log.info(' - ' + ser)
                if self.config.leavenonowners:
                    if s.unavailable:
                        unavailable_servers += 1
                    else:
                        check = s.get_member(owner.id)
                        if check == None:
                            await s.leave()
                            log.info('Left {} due to bot owner not found'.format(s.name))
            if unavailable_servers != 0:
                log.info(
                    'Not proceeding with checks in {} servers due to unavailability'.format(str(unavailable_servers)))

        elif self.guilds:
            log.warning("Owner could not be found on any guild (id: %s)\n" % self.config.owner_id)

            log.info('Guild List:')
            for s in self.guilds:
                ser = ('{} (unavailable)'.format(s.name) if s.unavailable else s.name)
                log.info(' - ' + ser)

        else:
            log.warning("Owner unknown, bot is not on any guilds.")
            if self.user.bot:
                log.warning(
                    "To make the bot join a guild, paste this link in your browser. \n"
                    "Note: You should be logged into your main account and have \n"
                    "manage server permissions on the guild you want the bot to join.\n"
                    "  " + await self.generate_invite_link()
                )

        print(flush=True)

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.VoiceChannel))

            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            if chlist:
                log.info("Bound to text channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                print("Not bound to any text channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Not binding to voice channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print(flush=True)

        else:
            log.info("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.TextChannel))

            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            if chlist:
                log.info("Autojoining voice channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                log.info("Not autojoining any voice channels")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("Cannot autojoin text channels:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            self.autojoin_channels = chlist

        else:
            log.info("Not autojoining any voice channels")
            self.autojoin_channels = set()

        if self.config.show_config_at_start:
            print(flush=True)
            log.info("Options:")

            log.info("  Command prefix: " + self.config.command_prefix)
            log.info("  Default volume: {}%".format(int(self.config.default_volume * 100)))
            log.info("  Skip threshold: {} votes or {}%".format(
                self.config.skips_required, fixg(self.config.skip_ratio_required * 100)))
            log.info("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
            log.info("  Auto-Summon: " + ['Disabled', 'Enabled'][self.config.auto_summon])
            log.info("  Auto-Playlist: " + ['Disabled', 'Enabled'][self.config.auto_playlist] + " (order: " +
                     ['sequential', 'random'][self.config.auto_playlist_random] + ")")
            log.info("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
            log.info("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
            if self.config.delete_messages:
                log.info("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
            log.info("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
            log.info("  Downloaded songs will be " + ['deleted', 'saved'][self.config.save_videos])
            if self.config.status_message:
                log.info("  Status message: " + self.config.status_message)
            log.info("  Write current songs to file: " + ['Disabled', 'Enabled'][self.config.write_current_song])
            log.info("  Author insta-skip: " + ['Disabled', 'Enabled'][self.config.allow_author_skip])
            log.info("  Embeds: " + ['Disabled', 'Enabled'][self.config.embeds])
            log.info("  Spotify integration: " + ['Disabled', 'Enabled'][self.config._spotify])
            log.info("  Legacy skip: " + ['Disabled', 'Enabled'][self.config.legacy_skip])
            log.info("  Leave non owners: " + ['Disabled', 'Enabled'][self.config.leavenonowners])

        print(flush=True)

        await self.update_now_playing_status()

        await self._join_startup_channels(self.autojoin_channels, autosummon=self.config.auto_summon)

        if self.config.missing_keys:
            log.warning('Your config file is missing some options. If you have recently updated, '
                        'check the example_options.ini file to see if there are new options available to you. '
                        'The options missing are: {0}'.format(self.config.missing_keys))
            print(flush=True)


    def _gen_embed(self):
        e = discord.Embed()
        e.colour = 7506394
        e.set_footer(text='peanutcorn/Kyunghee-portfolio ({})'.format(BOTVERSION), icon_url='https://i.imgur.com/gFHBoZA.png')
        e.set_author(name=self.user.name, url='https://github.com/peanutcorn/Kyunghee-portfolio',
                     icon_url=self.user.avatar_url)
        return e

    async def cmd_resetplaylist(self, player, channel):
        player.autoplaylist = list(set(self.autoplaylist))
        return Response(self.str.get('cmd-resetplaylist-response', '\N{OK HAND SIGN}'), delete_after=15)

    async def cmd_help(self, message, channel, command=None):
        self.commands = []
        self.is_all = False
        prefix = self.config.command_prefix

        if command:
            if command.lower() == 'all':
                self.is_all = True
                await self.gen_cmd_list(message, list_all_cmds=True)

            else:
                cmd = getattr(self, 'cmd_' + command, None)
                if cmd and not hasattr(cmd, 'dev_cmd'):
                    return Response(
                        "```\n{}```".format(
                            dedent(cmd.__doc__)
                        ).format(command_prefix=self.config.command_prefix),
                        delete_after=60
                    )
                else:
                    raise exceptions.CommandError(self.str.get('cmd-help-invalid', "No such command"), expire_in=10)

        elif message.author.id == self.config.owner_id:
            await self.gen_cmd_list(message, list_all_cmds=True)

        else:
            await self.gen_cmd_list(message)

        desc = '```\n' + ', '.join(self.commands) + '\n```\n' + self.str.get(
            'cmd-help-response', 'For information about a particular command, run `{}help [command]`\n'
                                 'For further help, see https://just-some-bots.github.io/MusicBot/').format(prefix)
        if not self.is_all:
            desc += self.str.get('cmd-help-all',
                                 '\nOnly showing commands you can use, for a list of all commands, run `{}help all`').format(
                prefix)

        return Response(desc, reply=True, delete_after=60)

    async def cmd_blacklist(self, message, user_mentions, option, something):

        if not user_mentions:
            raise exceptions.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                self.str.get('cmd-blacklist-invalid',
                             'Invalid option "{0}" specified, use +, -, add, or remove').format(option), expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] The owner cannot be blacklisted.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                self.str.get('cmd-blacklist-added', '{0} users have been added to the blacklist').format(
                    len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response(self.str.get('cmd-blacklist-none', 'None of those users are in the blacklist.'),
                                reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    self.str.get('cmd-blacklist-removed', '{0} users have been removed from the blacklist').format(
                        old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        if not user_mentions:
            return Response(self.str.get('cmd-id-self', 'Your ID is `{0}`').format(author.id), reply=True,
                            delete_after=35)
        else:
            usr = user_mentions[0]
            return Response(self.str.get('cmd-id-other', '**{0}**s ID is `{1}`').format(usr.name, usr.id), reply=True,
                            delete_after=35)

    async def cmd_save(self, player, url=None):
        if url or (player.current_entry and not isinstance(player.current_entry, StreamPlaylistEntry)):
            if not url:
                url = player.current_entry.url

            if url not in self.autoplaylist:
                self.autoplaylist.append(url)
                write_file(self.config.auto_playlist_file, self.autoplaylist)
                log.debug("Appended {} to autoplaylist".format(url))
                return Response(self.str.get('cmd-save-success', 'Added <{0}> to the autoplaylist.').format(url))
            else:
                raise exceptions.CommandError(
                    self.str.get('cmd-save-exists', 'This song is already in the autoplaylist.'))
        else:
            raise exceptions.CommandError(self.str.get('cmd-save-invalid', 'There is no valid song playing.'))

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        url = await self.generate_invite_link()
        return Response(
            self.str.get('cmd-joinserver-response', "Click here to add me to a server: \n{}").format(url),
            reply=True, delete_after=30
        )

    async def cmd_karaoke(self, player, channel, author):
        player.karaoke_mode = not player.karaoke_mode
        return Response("\N{OK HAND SIGN} Karaoke mode is now " + ['disabled', 'enabled'][player.karaoke_mode],
                        delete_after=15)

    async def _do_playlist_checks(self, permissions, player, author, testobj):
        num_songs = sum(1 for _ in testobj)

        if not permissions.allow_playlists and num_songs > 1:
            raise exceptions.PermissionsError(
                self.str.get('playlists-noperms', "You are not allowed to request playlists"), expire_in=30)

        if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
            raise exceptions.PermissionsError(
                self.str.get('playlists-big', "Playlist has too many entries ({0} > {1})").format(num_songs,
                                                                                                  permissions.max_playlist_length),
                expire_in=30
            )

        if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('playlists-limit',
                             "Playlist entries + your already queued songs reached limit ({0} + {1} > {2})").format(
                    num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                expire_in=30
            )
        return True

    async def cmd_play(self, message, player, channel, author, permissions, leftover_args, song_url):

        song_url = song_url.strip('<>')

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])
        leftover_args = None

        linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(song_url)
        song_url = song_url.replace('/', '%2F') if matchUrl is None else song_url

        playlistRegex = r'watch\?v=.+&(list=[^&]+)'
        matches = re.search(playlistRegex, song_url)
        groups = matches.groups() if matches is not None else []
        song_url = "https://www.youtube.com/playlist?" + groups[0] if len(groups) > 0 else song_url

        if self.config._spotify:
            if 'open.spotify.com' in song_url:
                song_url = 'spotify:' + re.sub('(http[s]?:\/\/)?(open.spotify.com)\/', '', song_url).replace('/', ':')
                song_url = re.sub('\?.*', '', song_url)
            if song_url.startswith('spotify:'):
                parts = song_url.split(":")
                try:
                    if 'track' in parts:
                        res = await self.spotify.get_track(parts[-1])
                        song_url = res['artists'][0]['name'] + ' ' + res['name']

                    elif 'album' in parts:
                        res = await self.spotify.get_album(parts[-1])
                        await self._do_playlist_checks(permissions, player, author, res['tracks']['items'])
                        procmesg = await self.safe_send_message(channel, self.str.get('cmd-play-spotify-album-process',
                                                                                      'Processing album `{0}` (`{1}`)').format(
                            res['name'], song_url))
                        for i in res['tracks']['items']:
                            song_url = i['name'] + ' ' + i['artists'][0]['name']
                            log.debug('Processing {0}'.format(song_url))
                            await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                        await self.safe_delete_message(procmesg)
                        return Response(
                            self.str.get('cmd-play-spotify-album-queued', "Enqueued `{0}` with **{1}** songs.").format(
                                res['name'], len(res['tracks']['items'])))

                    elif 'playlist' in parts:
                        res = []
                        r = await self.spotify.get_playlist_tracks(parts[-1])
                        while True:
                            res.extend(r['items'])
                            if r['next'] is not None:
                                r = await self.spotify.make_spotify_req(r['next'])
                                continue
                            else:
                                break
                        await self._do_playlist_checks(permissions, player, author, res)
                        procmesg = await self.safe_send_message(channel,
                                                                self.str.get('cmd-play-spotify-playlist-process',
                                                                             'Processing playlist `{0}` (`{1}`)').format(
                                                                    parts[-1], song_url))
                        for i in res:
                            song_url = i['track']['name'] + ' ' + i['track']['artists'][0]['name']
                            log.debug('Processing {0}'.format(song_url))
                            await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                        await self.safe_delete_message(procmesg)
                        return Response(self.str.get('cmd-play-spotify-playlist-queued',
                                                     "Enqueued `{0}` with **{1}** songs.").format(parts[-1], len(res)))

                    else:
                        raise exceptions.CommandError(
                            self.str.get('cmd-play-spotify-unsupported', 'That is not a supported Spotify URI.'),
                            expire_in=30)
                except exceptions.SpotifyError:
                    raise exceptions.CommandError(self.str.get('cmd-play-spotify-invalid',
                                                               'You either provided an invalid URI, or there was a problem.'))

        async with self.aiolocks[_func_() + ':' + str(author.id)]:
            if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
                raise exceptions.PermissionsError(
                    self.str.get('cmd-play-limit', "You have reached your enqueued song limit ({0})").format(
                        permissions.max_songs), expire_in=30
                )

            if player.karaoke_mode and not permissions.bypass_karaoke_mode:
                raise exceptions.PermissionsError(
                    self.str.get('karaoke-enabled', "Karaoke mode is enabled, please try again when its disabled!"),
                    expire_in=30
                )

            while True:
                try:
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False,
                                                              process=False)
                    try:
                        info_process = await self.downloader.extract_info(player.playlist.loop, song_url,
                                                                          download=False)
                    except:
                        info_process = None

                    log.debug(info)

                    if info_process and info and info_process.get('_type',
                                                                  None) == 'playlist' and 'entries' not in info and not info.get(
                            'url', '').startswith('ytsearch'):
                        use_url = info_process.get('webpage_url', None) or info_process.get('url', None)
                        if use_url == song_url:
                            log.warning("Determined incorrect entry type, but suggested url is the same.  Help.")
                            break

                        log.debug("Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                        log.debug("Using \"%s\" instead" % use_url)
                        song_url = use_url
                    else:
                        break

                except Exception as e:
                    if 'unknown url type' in str(e):
                        song_url = song_url.replace(':', '')
                        info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False,
                                                                  process=False)
                    else:
                        raise exceptions.CommandError(e, expire_in=30)

            if not info:
                raise exceptions.CommandError(
                    self.str.get('cmd-play-noinfo',
                                 "That video cannot be played. Try using the {0}stream command.").format(
                        self.config.command_prefix),
                    expire_in=30
                )

            if info.get('extractor', '') not in permissions.extractors and permissions.extractors:
                raise exceptions.PermissionsError(
                    self.str.get('cmd-play-badextractor',
                                 "You do not have permission to play media from this service."), expire_in=30
                )

            if info.get('url', '').startswith('ytsearch'):
                info = await self.downloader.extract_info(
                    player.playlist.loop,
                    song_url,
                    download=False,
                    process=True,
                    on_error=lambda e: asyncio.ensure_future(
                        self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                    retry_on_error=True
                )

                if not info:
                    raise exceptions.CommandError(
                        self.str.get('cmd-play-nodata',
                                     "Error extracting info from search string, youtubedl returned no data. "
                                     "You may need to restart the bot if this continues to happen."), expire_in=30
                    )

                if not all(info.get('entries', [])):
                    log.debug("Got empty list, no data")
                    return

                song_url = info['entries'][0]['webpage_url']
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)

            if 'entries' in info:
                await self._do_playlist_checks(permissions, player, author, info['entries'])

                num_songs = sum(1 for _ in info['entries'])

                if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                    try:
                        return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url,
                                                                   info['extractor'])
                    except exceptions.CommandError:
                        raise
                    except Exception as e:
                        log.error("Error queuing playlist", exc_info=True)
                        raise exceptions.CommandError(
                            self.str.get('cmd-play-playlist-error', "Error queuing playlist:\n`{0}`").format(e),
                            expire_in=30)

                t0 = time.time()

                wait_per_song = 1.2

                procmesg = await self.safe_send_message(
                    channel,
                    self.str.get('cmd-play-playlist-gathering-1',
                                 'Gathering playlist information for {0} songs{1}').format(
                        num_songs,
                        self.str.get('cmd-play-playlist-gathering-2', ', ETA: {0} seconds').format(fixg(
                            num_songs * wait_per_song)) if num_songs >= 10 else '.'))

                await self.send_typing(channel)


                entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

                tnow = time.time()
                ttime = tnow - t0
                listlen = len(entry_list)
                drop_count = 0

                if permissions.max_song_length:
                    for e in entry_list.copy():
                        if e.duration > permissions.max_song_length:
                            player.playlist.entries.remove(e)
                            entry_list.remove(e)
                            drop_count += 1
                    if drop_count:
                        print("Dropped %s songs" % drop_count)

                log.info("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
                    listlen,
                    fixg(ttime),
                    ttime / listlen if listlen else 0,
                    ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                    fixg(wait_per_song * num_songs))
                )

                await self.safe_delete_message(procmesg)

                if not listlen - drop_count:
                    raise exceptions.CommandError(
                        self.str.get('cmd-play-playlist-maxduration',
                                     "No songs were added, all songs were over max duration (%ss)") % permissions.max_song_length,
                        expire_in=30
                    )

                reply_text = self.str.get('cmd-play-playlist-reply',
                                          "Enqueued **%s** songs to be played. Position in queue: %s")
                btext = str(listlen - drop_count)

            else:
                if info.get('extractor', '').startswith('youtube:playlist'):
                    try:
                        info = await self.downloader.extract_info(player.playlist.loop,
                                                                  'https://www.youtube.com/watch?v=%s' % info.get('url',
                                                                                                                  ''),
                                                                  download=False, process=False)
                    except Exception as e:
                        raise exceptions.CommandError(e, expire_in=30)

                if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                    raise exceptions.PermissionsError(
                        self.str.get('cmd-play-song-limit', "Song duration exceeds limit ({0} > {1})").format(
                            info['duration'], permissions.max_song_length),
                        expire_in=30
                    )

                entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

                reply_text = self.str.get('cmd-play-song-reply', "Enqueued `%s` to be played. Position in queue: %s")
                btext = entry.title

            if position == 1 and player.is_stopped:
                position = self.str.get('cmd-play-next', 'Up next!')
                reply_text %= (btext, position)

            else:
                try:
                    time_until = await player.playlist.estimate_time_until(position, player)
                    reply_text += self.str.get('cmd-play-eta', ' - estimated time until playing: %s')
                except:
                    traceback.print_exc()
                    time_until = ''

                reply_text %= (btext, position, ftimedelta(time_until))

        return Response(reply_text, delete_after=30)

    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError(self.str.get('cmd-play-playlist-invalid', "That playlist cannot be played."))

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, self.str.get('cmd-play-playlist-process', "Processing {0} songs...").format(
                num_songs))
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(
                    self.str.get('cmd-play-playlist-queueerror', 'Error handling playlist {0} queuing.').format(
                        playlist_url), expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(
                    self.str.get('cmd-play-playlist-queueerror', 'Error handling playlist {0} queuing.').format(
                        playlist_url), expire_in=30)

        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                log.debug("Dropped %s songs" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.guild]['last_np_msg'])
                self.server_specific_data[channel.guild]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2

        log.info("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            fixg(ttime),
            ttime / num_songs if num_songs else 0,
            ttime / num_songs - wait_per_song if num_songs - wait_per_song else 0,
            fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = self.str.get('cmd-play-playlist-maxduration',
                                    "No songs were added, all songs were over max duration (%ss)") % permissions.max_song_length
            if skipped:
                basetext += self.str.get('cmd-play-playlist-skipped',
                                         "\nAdditionally, the current song was skipped for being too long.")

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response(
            self.str.get('cmd-play-playlist-reply-secs', "Enqueued {0} songs to be played in {1} seconds").format(
                songs_added, fixg(ttime, 1)), delete_after=30)

    async def cmd_stream(self, player, channel, author, permissions, song_url):

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('cmd-stream-limit', "You have reached your enqueued song limit ({0})").format(
                    permissions.max_songs), expire_in=30
            )

        if player.karaoke_mode and not permissions.bypass_karaoke_mode:
            raise exceptions.PermissionsError(
                self.str.get('karaoke-enabled', "Karaoke mode is enabled, please try again when its disabled!"),
                expire_in=30
            )

        await self.send_typing(channel)
        await player.playlist.add_stream_entry(song_url, channel=channel, author=author)

        return Response(self.str.get('cmd-stream-success', "Streaming."), delete_after=6)

    async def cmd_search(self, message, player, channel, author, permissions, leftover_args):
        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('cmd-search-limit', "You have reached your playlist item limit ({0})").format(
                    permissions.max_songs),
                expire_in=30
            )

        if player.karaoke_mode and not permissions.bypass_karaoke_mode:
            raise exceptions.PermissionsError(
                self.str.get('karaoke-enabled', "Karaoke mode is enabled, please try again when its disabled!"),
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                raise exceptions.CommandError(
                    self.str.get('cmd-search-noquery', "Please specify a search query.\n%s") % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError(
                self.str.get('cmd-search-noquote', "Please quote your search query properly."), expire_in=30)

        service = 'youtube'
        items_requested = 3
        max_items = permissions.max_search_items
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError(
                    self.str.get('cmd-search-searchlimit', "You cannot search for more than %s videos") % max_items)

        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.safe_send_message(channel,
                                                  self.str.get('cmd-search-searching', "Searching for videos..."))
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response(self.str.get('cmd-search-none', "No videos found."), delete_after=30)

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, self.str.get('cmd-search-result',
                                                                                "Result {0}/{1}: {2}").format(
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            def check(reaction, user):
                return user == message.author and reaction.message.id == result_message.id

            reactions = ['\u2705', '\U0001F6AB', '\U0001F3C1']
            for r in reactions:
                await result_message.add_reaction(r)

            try:
                reaction, user = await self.wait_for('reaction_add', timeout=30.0, check=check)
            except asyncio.TimeoutError:
                await self.safe_delete_message(result_message)
                return

            if str(reaction.emoji) == '\u2705':
                await self.safe_delete_message(result_message)
                await self.cmd_play(message, player, channel, author, permissions, [], e['webpage_url'])
                return Response(self.str.get('cmd-search-accept', "Alright, coming right up!"), delete_after=30)
            elif str(reaction.emoji) == '\U0001F6AB':
                await self.safe_delete_message(result_message)
                continue
            else:
                await self.safe_delete_message(result_message)
                break

        return Response(self.str.get('cmd-search-decline', "Oh well :("), delete_after=30)

    async def cmd_np(self, player, channel, guild, message):

        if player.current_entry:
            if self.server_specific_data[guild]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[guild]['last_np_msg'])
                self.server_specific_data[guild]['last_np_msg'] = None

            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))

            streaming = isinstance(player.current_entry, StreamPlaylistEntry)
            prog_str = ('`[{progress}]`' if streaming else '`[{progress}/{total}]`').format(
                progress=song_progress, total=song_total
            )
            prog_bar_str = ''

            percentage = 0.0
            if player.current_entry.duration > 0:
                percentage = player.progress / player.current_entry.duration

            progress_bar_length = 30
            for i in range(progress_bar_length):
                if (percentage < 1 / progress_bar_length * i):
                    prog_bar_str += '□'
                else:
                    prog_bar_str += '■'

            action_text = self.str.get('cmd-np-action-streaming', 'Streaming') if streaming else self.str.get(
                'cmd-np-action-playing', 'Playing')

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = self.str.get('cmd-np-reply-author',
                                       "Now {action}: **{title}** added by **{author}**\nProgress: {progress_bar} {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>").format(
                    action=action_text,
                    title=player.current_entry.title,
                    author=player.current_entry.meta['author'].name,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )
            else:

                np_text = self.str.get('cmd-np-reply-noauthor',
                                       "Now {action}: **{title}**\nProgress: {progress_bar} {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>").format(

                    action=action_text,
                    title=player.current_entry.title,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )

            self.server_specific_data[guild]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                self.str.get('cmd-np-none', 'There are no songs queued! Queue something with {0}play.').format(
                    self.config.command_prefix),
                delete_after=30
            )

    async def cmd_summon(self, channel, guild, author, voice_channel):

        if not author.voice:
            raise exceptions.CommandError(
                self.str.get('cmd-summon-novc', 'You are not connected to voice. Try joining a voice channel!'))

        voice_client = self.voice_client_in(guild)
        if voice_client and guild == author.voice.channel.guild:
            await voice_client.move_to(author.voice.channel)
        else:
            chperms = author.voice.channel.permissions_for(guild.me)

            if not chperms.connect:
                log.warning("Cannot join channel '{0}', no permission.".format(author.voice.channel.name))
                raise exceptions.CommandError(
                    self.str.get('cmd-summon-noperms-connect',
                                 "Cannot join channel `{0}`, no permission to connect.").format(
                        author.voice.channel.name),
                    expire_in=25
                )

            elif not chperms.speak:
                log.warning("Cannot join channel '{0}', no permission to speak.".format(author.voice.channel.name))
                raise exceptions.CommandError(
                    self.str.get('cmd-summon-noperms-speak',
                                 "Cannot join channel `{0}`, no permission to speak.").format(
                        author.voice.channel.name),
                    expire_in=25
                )

            player = await self.get_player(author.voice.channel, create=True, deserialize=self.config.persistent_queue)

            if player.is_stopped:
                player.play()

            if self.config.auto_playlist:
                await self.on_player_finished_playing(player)

        log.info("Joining {0.guild.name}/{0.name}".format(author.voice.channel))

        return Response(self.str.get('cmd-summon-reply', 'Connected to `{0.name}`').format(author.voice.channel))

    async def cmd_pause(self, player):

        if player.is_playing:
            player.pause()
            return Response(
                self.str.get('cmd-pause-reply', 'Paused music in `{0.name}`').format(player.voice_client.channel))

        else:
            raise exceptions.CommandError(self.str.get('cmd-pause-none', 'Player is not playing.'), expire_in=30)

    async def cmd_resume(self, player):

        if player.is_paused:
            player.resume()
            return Response(
                self.str.get('cmd-resume-reply', 'Resumed music in `{0.name}`').format(player.voice_client.channel),
                delete_after=15)

        else:
            raise exceptions.CommandError(self.str.get('cmd-resume-none', 'Player is not paused.'), expire_in=30)

    async def cmd_shuffle(self, channel, player):

        player.playlist.shuffle()

        cards = ['\N{BLACK SPADE SUIT}', '\N{BLACK CLUB SUIT}', '\N{BLACK HEART SUIT}', '\N{BLACK DIAMOND SUIT}']
        random.shuffle(cards)

        hand = await self.safe_send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            random.shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response(
            self.str.get('cmd-shuffle-reply', "Shuffled `{0}`'s queue.").format(player.voice_client.channel.guild),
            delete_after=15)

    async def cmd_clear(self, player, author):

        player.playlist.clear()
        return Response(
            self.str.get('cmd-clear-reply', "Cleared `{0}`'s queue").format(player.voice_client.channel.guild),
            delete_after=20)

    async def cmd_remove(self, user_mentions, message, author, permissions, channel, player, index=None):

        if not player.playlist.entries:
            raise exceptions.CommandError(self.str.get('cmd-remove-none', "There's nothing to remove!"), expire_in=20)

        if user_mentions:
            for user in user_mentions:
                if permissions.remove or author == user:
                    try:
                        entry_indexes = [e for e in player.playlist.entries if e.meta.get('author', None) == user]
                        for entry in entry_indexes:
                            player.playlist.entries.remove(entry)
                        entry_text = '%s ' % len(entry_indexes) + 'item'
                        if len(entry_indexes) > 1:
                            entry_text += 's'
                        return Response(
                            self.str.get('cmd-remove-reply', "Removed `{0}` added by `{1}`").format(entry_text,
                                                                                                    user.name).strip())

                    except ValueError:
                        raise exceptions.CommandError(
                            self.str.get('cmd-remove-missing', "Nothing found in the queue from user `%s`") % user.name,
                            expire_in=20)

                raise exceptions.PermissionsError(
                    self.str.get('cmd-remove-noperms',
                                 "You do not have the valid permissions to remove that entry from the queue, make sure you're the one who queued it or have instant skip permissions"),
                    expire_in=20)

        if not index:
            index = len(player.playlist.entries)

        try:
            index = int(index)
        except (TypeError, ValueError):
            raise exceptions.CommandError(
                self.str.get('cmd-remove-invalid', "Invalid number. Use {}queue to find queue positions.").format(
                    self.config.command_prefix), expire_in=20)

        if index > len(player.playlist.entries):
            raise exceptions.CommandError(
                self.str.get('cmd-remove-invalid', "Invalid number. Use {}queue to find queue positions.").format(
                    self.config.command_prefix), expire_in=20)

        if permissions.remove or author == player.playlist.get_entry_at_index(index - 1).meta.get('author', None):
            entry = player.playlist.delete_entry_at_index((index - 1))
            await self._manual_delete_check(message)
            if entry.meta.get('channel', False) and entry.meta.get('author', False):
                return Response(
                    self.str.get('cmd-remove-reply-author', "Removed entry `{0}` added by `{1}`").format(entry.title,
                                                                                                         entry.meta[
                                                                                                             'author'].name).strip())
            else:
                return Response(
                    self.str.get('cmd-remove-reply-noauthor', "Removed entry `{0}`").format(entry.title).strip())
        else:
            raise exceptions.PermissionsError(
                self.str.get('cmd-remove-noperms',
                             "You do not have the valid permissions to remove that entry from the queue, make sure you're the one who queued it or have instant skip permissions"),
                expire_in=20
            )

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel, param=''):

        if player.is_stopped:
            raise exceptions.CommandError(self.str.get('cmd-skip-none', "Can't skip! The player is not playing!"),
                                          expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    return Response(self.str.get('cmd-skip-dl',
                                                 "The next song (`%s`) is downloading, please wait.") % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("The next song will be played shortly.  Please wait.")
                else:
                    print("Something odd is happening.  "
                          "You might want to restart the bot if it doesn't start working.")
            else:
                print("Something strange is happening.  "
                      "You might want to restart the bot if it doesn't start working.")

        current_entry = player.current_entry

        if (param.lower() in ['force', 'f']) or self.config.legacy_skip:
            if permissions.instaskip \
                    or (self.config.allow_author_skip and author == player.current_entry.meta.get('author', None)):

                player.skip()
                await self._manual_delete_check(message)
                return Response(self.str.get('cmd-skip-force', 'Force skipped `{}`.').format(current_entry.title),
                                reply=True, delete_after=30)
            else:
                raise exceptions.PermissionsError(
                    self.str.get('cmd-skip-force-noperms', 'You do not have permission to force skip.'), expire_in=30)


        num_voice = sum(1 for m in voice_channel.members if not (
                m.voice.deaf or m.voice.self_deaf or m == self.user))
        if num_voice == 0: num_voice = 1

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(
            self.config.skips_required,
            math.ceil(self.config.skip_ratio_required / (1 / num_voice))
        ) - num_skips

        if skips_remaining <= 0:
            player.skip()
            return Response(
                self.str.get('cmd-skip-reply-skipped-1',
                             'Your skip for `{0}` was acknowledged.\nThe vote to skip has been passed.{1}').format(
                    current_entry.title,
                    self.str.get('cmd-skip-reply-skipped-2', ' Next song coming up!') if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            return Response(
                self.str.get('cmd-skip-reply-voted-1',
                             'Your skip for `{0}` was acknowledged.\n**{1}** more {2} required to vote to skip this song.').format(
                    current_entry.title,
                    skips_remaining,
                    self.str.get('cmd-skip-reply-voted-2', 'person is') if skips_remaining == 1 else self.str.get(
                        'cmd-skip-reply-voted-3', 'people are')
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_volume(self, message, player, new_volume=None):

        if not new_volume:
            return Response(self.str.get('cmd-volume-current', 'Current volume: `%s%%`') % int(player.volume * 100),
                            reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError(
                self.str.get('cmd-volume-invalid', '`{0}` is not a valid number').format(new_volume), expire_in=20)

        vol_change = None
        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response(
                self.str.get('cmd-volume-reply', 'Updated volume from **%d** to **%d**') % (old_volume, new_volume),
                reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    self.str.get('cmd-volume-unreasonable-relative',
                                 'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.').format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume),
                    expire_in=20)
            else:
                raise exceptions.CommandError(
                    self.str.get('cmd-volume-unreasonable-absolute',
                                 'Unreasonable volume provided: {}%. Provide a value between 1 and 100.').format(
                        new_volume), expire_in=20)

    @owner_only
    async def cmd_option(self, player, option, value):

        option = option.lower()
        value = value.lower()
        bool_y = ['on', 'y', 'enabled']
        bool_n = ['off', 'n', 'disabled']
        generic = ['save_videos', 'now_playing_mentions', 'auto_playlist_random',
                   'auto_pause', 'delete_messages', 'delete_invoking',
                   'write_current_song']
        if option in ['autoplaylist', 'auto_playlist']:
            if value in bool_y:
                if self.config.auto_playlist:
                    raise exceptions.CommandError(
                        self.str.get('cmd-option-autoplaylist-enabled', 'The autoplaylist is already enabled!'))
                else:
                    if not self.autoplaylist:
                        raise exceptions.CommandError(self.str.get('cmd-option-autoplaylist-none',
                                                                   'There are no entries in the autoplaylist file.'))
                    self.config.auto_playlist = True
                    await self.on_player_finished_playing(player)
            elif value in bool_n:
                if not self.config.auto_playlist:
                    raise exceptions.CommandError(
                        self.str.get('cmd-option-autoplaylist-disabled', 'The autoplaylist is already disabled!'))
                else:
                    self.config.auto_playlist = False
            else:
                raise exceptions.CommandError(
                    self.str.get('cmd-option-invalid-value', 'The value provided was not valid.'))
            return Response("The autoplaylist is now " + ['disabled', 'enabled'][self.config.auto_playlist] + '.')
        else:
            is_generic = [o for o in generic if o == option]
            if is_generic and (value in bool_y or value in bool_n):
                name = is_generic[0]
                log.debug('Setting attribute {0}'.format(name))
                setattr(self.config, name, True if value in bool_y else False)
                attr = getattr(self.config, name)
                res = "The option {0} is now ".format(option) + ['disabled', 'enabled'][attr] + '.'
                log.warning('Option overriden for this session: {0}'.format(res))
                return Response(res)
            else:
                raise exceptions.CommandError(
                    self.str.get('cmd-option-invalid-param', 'The parameters provided were invalid.'))

    async def cmd_queue(self, channel, player):
        lines = []
        unlisted = 0
        andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

        if player.is_playing:
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append(
                    self.str.get('cmd-queue-playing-author', "Currently playing: `{0}` added by `{1}` {2}\n").format(
                        player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
            else:
                lines.append(self.str.get('cmd-queue-playing-noauthor', "Currently playing: `{0}` {1}\n").format(
                    player.current_entry.title, prog_str))

        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = self.str.get('cmd-queue-entry-author', '{0} -- `{1}` by `{2}`').format(i, item.title,
                                                                                                  item.meta[
                                                                                                      'author'].name).strip()
            else:
                nextline = self.str.get('cmd-queue-entry-noauthor', '{0} -- `{1}`').format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)

            if (currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT) or (
                    i > self.config.queue_length):
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append(self.str.get('cmd-queue-more', '\n... and %s more') % unlisted)

        if not lines:
            lines.append(
                self.str.get('cmd-queue-none', 'There are no songs queued! Queue something with {}play.').format(
                    self.config.command_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)

    async def cmd_clean(self, message, channel, guild, author, search_range=50):

        try:
            float(search_range)
            search_range = min(int(search_range), 1000)
        except:
            return Response(
                self.str.get('cmd-clean-invalid', "Invalid parameter. Please provide a number of messages to search."),
                reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(guild.me).manage_messages:
                deleted = await channel.purge(check=check, limit=search_range, before=message)
                return Response(self.str.get('cmd-clean-reply', 'Cleaned up {0} message{1}.').format(len(deleted),
                                                                                                     's' * bool(
                                                                                                         deleted)),
                                delete_after=15)

    async def cmd_pldump(self, channel, author, song_url):

        try:
            info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

        if not info:
            raise exceptions.CommandError("Could not extract info from input url, no data.", expire_in=25)

        if not info.get('entries', None):

            if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
                raise exceptions.CommandError("This does not seem to be a playlist.", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube": lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp": lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError("Could not extract info from input url, unsupported playlist type.",
                                          expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await author.send("Here's the playlist dump for <%s>" % song_url,
                              file=discord.File(fcontent, filename='playlist.txt'))

        return Response("Sent a message with a playlist file.", delete_after=20)

    async def cmd_listids(self, guild, author, leftover_args, cat='all'):

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in guild.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in guild.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            await author.send(file=discord.File(sdata, filename='%s-ids-%s.txt' % (guild.name.replace(' ', '_'), cat)))

        return Response("Sent a message with a list of IDs.", delete_after=20)

    async def cmd_perms(self, author, user_mentions, channel, guild, message, permissions, target=None):

        if user_mentions:
            user = user_mentions[0]

        if not user_mentions and not target:
            user = author

        if not user_mentions and target:
            user = guild.get_member_named(target)
            if user == None:
                try:
                    user = await self.fetch_user(target)
                except discord.NotFound:
                    return Response("Invalid user ID or server nickname, please double check all typing and try again.",
                                    reply=False, delete_after=30)

        permissions = self.permissions.for_user(user)

        if user == author:
            lines = ['Command permissions in %s\n' % guild.name, '```', '```']
        else:
            lines = ['Command permissions for {} in {}\n'.format(user.name, guild.name), '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue
            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.safe_send_message(author, '\n'.join(lines))
        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=20)

    @owner_only
    async def cmd_setname(self, leftover_args, name):

        name = ' '.join([name, *leftover_args])

        try:
            await self.user.edit(username=name)

        except discord.HTTPException:
            raise exceptions.CommandError(
                "Failed to change name. Did you change names too many times?  "
                "Remember name changes are limited to twice per hour.")

        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("Set the bot's username to **{0}**".format(name), delete_after=20)

    async def cmd_setnick(self, guild, channel, leftover_args, nick):

        if not channel.permissions_for(guild.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await guild.me.edit(nick=nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("Set the bot's nickname to `{0}`".format(nick), delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        if message.attachments:
            thing = message.attachments[0].url
        elif url:
            thing = url.strip('<>')
        else:
            raise exceptions.CommandError("You must provide a URL or attach a file.", expire_in=20)

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.aiosession.get(thing, timeout=timeout) as res:
                await self.user.edit(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("Unable to change avatar: {}".format(e), expire_in=20)

        return Response("Changed the bot's avatar.", delete_after=20)

    async def cmd_disconnect(self, guild):
        await self.disconnect_voice_client(guild)
        return Response("Disconnected from `{0.name}`".format(guild), delete_after=20)

    async def cmd_restart(self, channel):
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN} Restarting. If you have updated your bot "
                                              "or its dependencies, you need to restart the bot properly, rather than using this command.")

        player = self.get_player_in(channel.guild)
        if player and player.is_paused:
            player.resume()

        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal()

    async def cmd_shutdown(self, channel):
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")

        player = self.get_player_in(channel.guild)
        if player and player.is_paused:
            player.resume()

        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal()

    async def cmd_leaveserver(self, val, leftover_args):
        if leftover_args:
            val = ' '.join([val, *leftover_args])

        t = self.get_guild(val)
        if t is None:
            t = discord.utils.get(self.guilds, name=val)
            if t is None:
                raise exceptions.CommandError('No guild was found with the ID or name as `{0}`'.format(val))
        await t.leave()
        return Response('Left the guild: `{0.name}` (Owner: `{0.owner.name}`, ID: `{0.id}`)'.format(t))

    @dev_only
    async def cmd_breakpoint(self, message):
        log.critical("Activating debug breakpoint")
        return

    @dev_only
    async def cmd_objgraph(self, channel, func='most_common_types()'):
        import objgraph

        await self.send_typing(channel)

        if func == 'growth':
            f = StringIO()
            objgraph.show_growth(limit=10, file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leaks':
            f = StringIO()
            objgraph.show_most_common_types(objects=objgraph.get_leaking_objects(), file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leakstats':
            data = objgraph.typestats(objects=objgraph.get_leaking_objects())

        else:
            data = eval('objgraph.' + func)

        return Response(data, codeblock='py')

    @dev_only
    async def cmd_debug(self, message, _player, *, data):
        codeblock = "```py\n{}\n```"
        result = None

        if data.startswith('```') and data.endswith('```'):
            data = '\n'.join(data.rstrip('`\n').split('\n')[1:])

        code = data.strip('` \n')

        scope = globals().copy()
        scope.update({'self': self})

        try:
            result = eval(code, scope)
        except:
            try:
                exec(code, scope)
            except Exception as e:
                traceback.print_exc(chain=False)
                return Response("{}: {}".format(type(e).__name__, e))

        if asyncio.iscoroutine(result):
            result = await result

        return Response(codeblock.format(result))

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            log.warning("Ignoring command from myself ({})".format(message.content))
            return

        if message.author.bot and message.author.id not in self.config.bot_exception_ids:
            log.warning("Ignoring command from other bot ({})".format(message.content))
            return

        if (not isinstance(message.channel, discord.abc.GuildChannel)) and (
        not isinstance(message.channel, discord.abc.PrivateChannel)):
            return

        command, *args = message_content.split(
            ' ')
        command = command[len(self.config.command_prefix):].lower().strip()

        if args:
            args = ' '.join(args).lstrip(' ').split(' ')
        else:
            args = []

        handler = getattr(self, 'cmd_' + command, None)
        if not handler:
            if self.config.usealias:
                command = self.aliases.get(command)
                handler = getattr(self, 'cmd_' + command, None)
                if not handler:
                    return
            else:
                return

        if isinstance(message.channel, discord.abc.PrivateChannel):
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                await self.safe_send_message(message.channel, 'You cannot use this bot in private messages.')
                return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels:
            if self.config.unbound_servers:
                for channel in message.guild.channels:
                    if channel.id in self.config.bound_channels:
                        return
            else:
                return

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            log.warning("User blacklisted: {0.id}/{0!s} ({1})".format(message.author, command))
            return

        else:
            log.info("{0.id}/{0!s}: {1}".format(message.author, message_content.replace('\n', '\n... ')))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        sentmsg = response = None

        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('guild', None):
                handler_kwargs['guild'] = message.guild

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('_player', None):
                handler_kwargs['_player'] = self.get_player_in(message.guild)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.guild.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.guild.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.guild.me.voice.channel if message.guild.me.voice else None

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):

                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                if param.kind == param.KEYWORD_ONLY and param.default == param.empty:
                    handler_kwargs[key] = ' '.join(args)
                    params.pop(key)
                    continue

                doc_key = '[{}={}]'.format(key, param.default) if param.default is not param.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    '```\n{}\n```'.format(docs.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                if not isinstance(response.content, discord.Embed) and self.config.embeds:
                    content = self._gen_embed()
                    content.title = command
                    content.description = response.content
                else:
                    content = response.content

                if response.reply:
                    if isinstance(content, discord.Embed):
                        content.description = '{} {}'.format(message.author.mention,
                                                             content.description if content.description is not discord.Embed.Empty else '')
                    else:
                        content = '{}: {}'.format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            log.error("Error in {0}: {1.__class__.__name__}: {1.message}".format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            if self.config.embeds:
                content = self._gen_embed()
                content.add_field(name='Error', value=e.message, inline=False)
                content.colour = 13369344
            else:
                content = '```\n{}\n```'.format(e.message)

            await self.safe_send_message(
                message.channel,
                content,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            log.error("Exception in on_message", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n{}\n```'.format(traceback.format_exc()))

        finally:
            if not sentmsg and not response and self.config.delete_invoking:
                await asyncio.sleep(5)
                await self.safe_delete_message(message, quiet=True)

    async def gen_cmd_list(self, message, list_all_cmds=False):
        for att in dir(self):
            if att.startswith('cmd_') and not hasattr(getattr(self, att), 'dev_cmd'):
                user_permissions = self.permissions.for_user(message.author)
                command_name = att.replace('cmd_', '').lower()
                whitelist = user_permissions.command_whitelist
                blacklist = user_permissions.command_blacklist
                if list_all_cmds:
                    self.commands.append('{}{}'.format(self.config.command_prefix, command_name))

                elif blacklist and command_name in blacklist:
                    pass

                elif whitelist and command_name not in whitelist:
                    pass

                else:
                    self.commands.append("{}{}".format(self.config.command_prefix, command_name))

    async def on_voice_state_update(self, member, before, after):
        if not self.init_ok:
            return

        if before.channel:
            channel = before.channel
        elif after.channel:
            channel = after.channel
        else:
            return

        if not self.config.auto_pause:
            return

        autopause_msg = "{state} in {channel.guild.name}/{channel.name} {reason}"

        auto_paused = self.server_specific_data[channel.guild]['auto_paused']

        try:
            player = await self.get_player(channel)
        except exceptions.CommandError:
            return

        def is_active(member):
            if not member.voice:
                return False

            if any([member.voice.deaf, member.voice.self_deaf, member.bot]):
                return False

            return True

        if not member == self.user and is_active(member):
            if player.voice_client.channel != before.channel and player.voice_client.channel == after.channel:
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state="Unpausing",
                        channel=player.voice_client.channel,
                        reason=""
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()
            elif player.voice_client.channel == before.channel and player.voice_client.channel != after.channel:
                if not any(is_active(m) for m in player.voice_client.channel.members):
                    if not auto_paused and player.is_playing:
                        log.info(autopause_msg.format(
                            state="Pausing",
                            channel=player.voice_client.channel,
                            reason="(empty channel)"
                        ).strip())

                        self.server_specific_data[player.voice_client.guild]['auto_paused'] = True
                        player.pause()
            elif player.voice_client.channel == before.channel and player.voice_client.channel == after.channel:
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state="Unpausing",
                        channel=player.voice_client.channel,
                        reason="(member undeafen)"
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()
        else:
            if any(is_active(m) for m in player.voice_client.channel.members):
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state="Unpausing",
                        channel=player.voice_client.channel,
                        reason=""
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()

            else:
                if not auto_paused and player.is_playing:
                    log.info(autopause_msg.format(
                        state="Pausing",
                        channel=player.voice_client.channel,
                        reason="(empty channel or member deafened)"
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = True
                    player.pause()

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if before.region != after.region:
            log.warning("Guild \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

    async def on_guild_join(self, guild: discord.Guild):
        log.info("Bot has been added to guild: {}".format(guild.name))
        owner = self._get_owner(voice=True) or self._get_owner()
        if self.config.leavenonowners:
            check = guild.get_member(owner.id)
            if check == None:
                await guild.leave()
                log.info('Left {} due to bot owner not found.'.format(guild.name))
                await owner.send(self.str.get('left-no-owner-guilds',
                                              'Left `{}` due to bot owner not being found in it.'.format(guild.name)))

        log.debug("Creating data folder for guild %s", guild.id)
        pathlib.Path('data/%s/' % guild.id).mkdir(exist_ok=True)

    async def on_guild_remove(self, guild: discord.Guild):
        log.info("Bot has been removed from guild: {}".format(guild.name))
        log.debug('Updated guild list:')
        [log.debug(' - ' + s.name) for s in self.guilds]

        if guild.id in self.players:
            self.players.pop(guild.id).kill()

    async def on_guild_available(self, guild: discord.Guild):
        if not self.init_ok:
            return
        log.debug("Guild \"{}\" has become available.".format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_paused:
            av_paused = self.server_specific_data[guild]['availability_paused']

            if av_paused:
                log.debug("Resuming player in \"{}\" due to availability.".format(guild.name))
                self.server_specific_data[guild]['availability_paused'] = False
                player.resume()

    async def on_guild_unavailable(self, guild: discord.Guild):
        log.debug("Guild \"{}\" has become unavailable.".format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_playing:
            log.debug("Pausing player in \"{}\" due to unavailability.".format(guild.name))
            self.server_specific_data[guild]['availability_paused'] = True
            player.pause()

    def voice_client_in(self, guild):
        for vc in self.voice_clients:
            if vc.guild == guild:
                return vc
        return None

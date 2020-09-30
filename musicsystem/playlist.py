import os.path
import logging
import datetime

from random import shuffle
from itertools import islice
from collections import deque

from urllib.error import URLError
from youtube_dl.utils import ExtractorError, DownloadError, UnsupportedError

from .utils import get_header
from .constructs import Serializable
from .lib.event_emitter import EventEmitter
from .entry import URLPlaylistEntry, StreamPlaylistEntry
from .exceptions import ExtractionError, WrongEntryTypeError

log = logging.getLogger(__name__)


class Playlist(EventEmitter, Serializable):

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.loop = bot.loop
        self.downloader = bot.downloader
        self.entries = deque()

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def shuffle(self):
        shuffle(self.entries)

    def clear(self):
        self.entries.clear()

    def get_entry_at_index(self, index):
        self.entries.rotate(-index)
        entry = self.entries[0]
        self.entries.rotate(index)
        return entry

    def delete_entry_at_index(self, index):
        self.entries.rotate(-index)
        entry = self.entries.popleft()
        self.entries.rotate(index)
        return entry

    async def add_entry(self, song_url, **meta):

        try:
            info = await self.downloader.extract_info(self.loop, song_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(song_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % song_url)

        # TODO: Sort out what happens next when this happens
        if info.get('_type', None) == 'playlist':
            raise WrongEntryTypeError("This is a playlist.", True,
                                      info.get('webpage_url', None) or info.get('url', None))

        if info.get('is_live', False):
            return await self.add_stream_entry(song_url, info=info, **meta)

        # TODO: Extract this to its own function
        if info['extractor'] in ['generic', 'Dropbox']:
            log.debug('Detected a generic extractor, or Dropbox')
            try:
                headers = await get_header(self.bot.aiosession, info['url'])
                content_type = headers.get('CONTENT-TYPE')
                log.debug("Got content type {}".format(content_type))
            except Exception as e:
                log.warning("Failed to get content type for url {} ({})".format(song_url, e))
                content_type = None

            if content_type:
                if content_type.startswith(('application/', 'image/')):
                    if not any(x in content_type for x in ('/ogg', '/octet-stream')):
                        raise ExtractionError("Invalid content type \"%s\" for url %s" % (content_type, song_url))

                elif content_type.startswith('text/html') and info['extractor'] == 'generic':
                    log.warning("Got text/html for content-type, this might be a stream.")
                    return await self.add_stream_entry(song_url, info=info, **meta)

                elif not content_type.startswith(('audio/', 'video/')):
                    log.warning("Questionable content-type \"{}\" for url {}".format(content_type, song_url))

        entry = URLPlaylistEntry(
            self,
            song_url,
            info.get('title', 'Untitled'),
            info.get('duration', 0) or 0,
            self.downloader.ytdl.prepare_filename(info),
            **meta
        )
        self._add_entry(entry)
        return entry, len(self.entries)

    async def add_stream_entry(self, song_url, info=None, **meta):
        if info is None:
            info = {'title': song_url, 'extractor': None}

            try:
                info = await self.downloader.extract_info(self.loop, song_url, download=False)

            except DownloadError as e:
                if e.exc_info[0] == UnsupportedError:
                    log.debug("Assuming content is a direct stream")

                elif e.exc_info[0] == URLError:
                    if os.path.exists(os.path.abspath(song_url)):
                        raise ExtractionError("This is not a stream, this is a file path.")

                    else:
                        raise ExtractionError("Invalid input: {0.exc_info[0]}: {0.exc_info[1].reason}".format(e))

                else:
                    raise ExtractionError("Unknown error: {}".format(e))

            except Exception as e:
                log.error('Could not extract information from {} ({}), falling back to direct'.format(song_url, e),
                          exc_info=True)

        if info.get('is_live') is None and info.get('extractor', None) is not 'generic':  # wew hacky
            raise ExtractionError("This is not a stream.")

        dest_url = song_url
        if info.get('extractor'):
            dest_url = info.get('url')

        if info.get('extractor', None) == 'twitch:stream':
            title = info.get('description')
        else:
            title = info.get('title', 'Untitled')


        entry = StreamPlaylistEntry(
            self,
            song_url,
            title,
            destination=dest_url,
            **meta
        )
        self._add_entry(entry)
        return entry, len(self.entries)

    async def import_from(self, playlist_url, **meta):

        position = len(self.entries) + 1
        entry_list = []

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        if info.get('extractor', None) == 'generic':
            url_field = 'url'
        else:
            url_field = 'webpage_url'

        baditems = 0
        for item in info['entries']:
            if item:
                try:
                    entry = URLPlaylistEntry(
                        self,
                        item[url_field],
                        item.get('title', 'Untitled'),
                        item.get('duration', 0) or 0,
                        self.downloader.ytdl.prepare_filename(item),
                        **meta
                    )

                    self._add_entry(entry)
                    entry_list.append(entry)
                except Exception as e:
                    baditems += 1
                    log.warning("Could not add item", exc_info=e)
                    log.debug("Item: {}".format(item), exc_info=True)
            else:
                baditems += 1

        if baditems:
            log.info("Skipped {} bad entries".format(baditems))

        return entry_list, position

    async def async_process_youtube_playlist(self, playlist_url, **meta):

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0

        for entry_data in info['entries']:
            if entry_data:
                baseurl = info['webpage_url'].split('playlist?list=')[0]
                song_url = baseurl + 'watch?v=%s' % entry_data['id']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)

                except ExtractionError:
                    baditems += 1

                except Exception as e:
                    baditems += 1
                    log.error("Error adding entry {}".format(entry_data['id']), exc_info=e)
            else:
                baditems += 1

        if baditems:
            log.info("Skipped {} bad entries".format(baditems))

        return gooditems

    async def async_process_sc_bc_playlist(self, playlist_url, **meta):

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0

        for entry_data in info['entries']:
            if entry_data:
                song_url = entry_data['url']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)

                except ExtractionError:
                    baditems += 1

                except Exception as e:
                    baditems += 1
                    log.error("Error adding entry {}".format(entry_data['id']), exc_info=e)
            else:
                baditems += 1

        if baditems:
            log.info("Skipped {} bad entries".format(baditems))

        return gooditems

    def _add_entry(self, entry, *, head=False):
        if head:
            self.entries.appendleft(entry)
        else:
            self.entries.append(entry)

        self.emit('entry-added', playlist=self, entry=entry)

        if self.peek() is entry:
            entry.get_ready_future()

    def remove_entry(self, index):
        del self.entries[index]

    async def get_next_entry(self, predownload_next=True):

        if not self.entries:
            return None

        entry = self.entries.popleft()

        if predownload_next:
            next_entry = self.peek()
            if next_entry:
                next_entry.get_ready_future()

        return await entry.get_ready_future()

    def peek(self):

        if self.entries:
            return self.entries[0]

    async def estimate_time_until(self, position, player):

        estimated_time = sum(e.duration for e in islice(self.entries, position - 1))

        if not player.is_stopped and player.current_entry:
            estimated_time += player.current_entry.duration - player.progress

        return datetime.timedelta(seconds=estimated_time)

    def count_for_user(self, user):
        return sum(1 for e in self.entries if e.meta.get('author', None) == user)

    def __json__(self):
        return self._enclose_json({
            'entries': list(self.entries)
        })

    @classmethod
    def _deserialize(cls, raw_json, bot=None):
        assert bot is not None, cls._bad('bot')
        pl = cls(bot)

        for entry in raw_json['entries']:
            pl.entries.append(entry)

        return pl
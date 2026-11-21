import random
from collections import deque

from diagnostics import log
from soundcloud import (
    GET_AUDIO,
    LOAD_RELATED,
    LOAD_STATION,
    OPEN_AUDIO,
    SoundCloudError,
    SoundCloudHTTPError,
    track_urn,
)


class Wave:
    def __init__(self, api):
        self.api = api
        self.seeds = []
        self.queue = deque()
        self.seen = set()
        self.station = None

    def connect(self, value):
        self.api.connect(value)
        self.seeds.clear()
        self.reset()

    def sign_out(self):
        self.api.sign_out()
        self.seeds.clear()
        self.reset()

    def reset(self):
        self.queue.clear()
        self.seen.clear()

    def next_track(self, station):
        if station != self.station:
            self.reset()
            self.station = station
        if not self.seeds:
            self.seeds = self.api.likes()
        if not self.seeds:
            raise SoundCloudError("В твоей библиотеке пока нет лайкнутых треков")

        track = self._take_playable()
        if track:
            return track

        last_denial = None
        loaded_selection = False
        for seed in random.sample(self.seeds, min(len(self.seeds), 5)):
            try:
                tracks = self.api.recommendations(seed, station)
            except SoundCloudHTTPError as error:
                if error.status != 403 or error.action not in (
                    LOAD_STATION,
                    LOAD_RELATED,
                ):
                    raise
                last_denial = error
                log.debug("Пропущена подборка от трека %s: HTTP 403", track_urn(seed))
                continue
            loaded_selection = True
            if not station:
                random.shuffle(tracks)
            self.queue.extend(tracks)
            track = self._take_playable()
            if track:
                return track

        self.reset()
        if not loaded_selection and last_denial:
            raise last_denial
        raise SoundCloudError(
            "В подборке больше нет доступных треков. Попробуй запустить волну снова."
        )

    def _take_playable(self):
        while self.queue:
            track = self.queue.popleft()
            key = track_urn(track)
            if key in self.seen or track.get("access") == "blocked":
                continue
            try:
                url = self.api.stream(track)
            except SoundCloudHTTPError as error:
                if error.status != 403 or error.action not in (GET_AUDIO, OPEN_AUDIO):
                    raise
                log.debug("Пропущен трек %s: аудио вернуло HTTP 403", key)
                self.seen.add(key)
                continue
            self.seen.add(key)
            if url:
                return track, url
        return None

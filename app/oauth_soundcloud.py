import json
import os
import time
from urllib.parse import unquote, urlsplit

import requests
from diagnostics import log_http
from soundcloud import (
    GET_AUDIO,
    LOAD_RELATED,
    LOAD_STATION,
    OPEN_AUDIO,
    SoundCloud,
    SoundCloudError,
    track_urn,
)

API = "https://api.soundcloud.com"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"


def soundcloud_client():
    client_id = os.getenv("SOUNDCLOUD_CLIENT_ID")
    client_secret = os.getenv("SOUNDCLOUD_CLIENT_SECRET")
    if client_id and client_secret:
        return OAuthSoundCloud(client_id, client_secret)
    return SoundCloud()


class OAuthSoundCloud(SoundCloud):
    api_url = API

    def __init__(self, client_id, client_secret):
        super().__init__()
        self.session_file = self.session_file.with_name("oauth-session.json")
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = None
        self.http = requests.Session()

    def connect(self, value):
        token = unquote(value.strip()).removeprefix("refresh_token=").strip()
        if not token:
            raise SoundCloudError("Вставь refresh-токен SoundCloud")
        self._refresh(token)
        self.restore_session()

    def _refresh(self, refresh_token):
        response = self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=20,
            allow_redirects=False,
        )
        if response.status_code in (400, 401):
            log_http(response, "обновление сессии")
            raise SoundCloudError(
                "SoundCloud не принял refresh-токен или ключи приложения"
            )
        self._check(response, "обновление сессии")
        tokens = response.json()
        self.tokens = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": time.time() + tokens["expires_in"],
            "client_id": self.client_id,
        }
        # Persist the rotated token before the next API call.
        self._save_session(self.tokens)

    def _ensure_tokens(self):
        if self.tokens is None:
            if not self.has_saved_session:
                raise SoundCloudError("Вставь refresh-токен SoundCloud на экране входа")
            saved = json.loads(self.session_file.read_text())
            if saved["client_id"] != self.client_id:
                raise SoundCloudError(
                    "Сессия создана для другого приложения SoundCloud"
                )
            self.tokens = saved
        if time.time() >= self.tokens["expires_at"] - 30:
            self._refresh(self.tokens["refresh_token"])

    def _get_response(self, url, *, action, params=None, stream=False):
        self._ensure_tokens()

        def send():
            return self.http.get(
                url,
                params=params,
                timeout=20,
                stream=stream,
                headers={"Authorization": f"OAuth {self.tokens['access_token']}"},
            )

        response = send()
        if response.status_code == 401:
            log_http(response, action)
            response.close()
            self._refresh(self.tokens["refresh_token"])
            response = send()
        try:
            self._check(response, action)
        except SoundCloudError:
            response.close()
            raise
        return response

    def get(self, path, *, action, **params):
        with self._get_response(
            self.api_url + path, action=action, params=params
        ) as response:
            return response.json() if response.content else None

    def restore_session(self):
        self.get("/me", action="проверка входа")

    def sign_out(self):
        super().sign_out()
        self.tokens = None
        self.http = requests.Session()

    def likes(self):
        return self._collection(
            "/me/likes/tracks", "загрузка лайков", limit=200, all_pages=True
        )

    def recommendations(self, seed, station):
        if station:
            station_id = (
                seed.get("station_urn")
                or f"track-stations:{track_urn(seed).split(':')[-1]}"
            )
            playlist = self.get(f"/system-playlists/{station_id}", action=LOAD_STATION)
            return playlist["tracks"]
        return self._collection(f"/tracks/{track_urn(seed)}/related", LOAD_RELATED)

    def stream(self, track):
        streams = self.get(f"/tracks/{track_urn(track)}/streams", action=GET_AUDIO)
        url = streams.get("hls_aac_160_url") or streams.get("hls_mp3_128_url")
        if not url or not url.startswith(API + "/"):
            return None
        with self._get_response(url, action=OPEN_AUDIO, stream=True) as response:
            destination = urlsplit(response.url)
            if destination.scheme == "https" and (destination.hostname or "").endswith(
                ".sndcdn.com"
            ):
                return response.url
        return None

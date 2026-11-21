import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qsl, unquote, urlsplit

import requests

from diagnostics import log, log_http

SITE = 'https://soundcloud.com'
API = 'https://api-v2.soundcloud.com'
GET_AUDIO = 'получение аудио'
OPEN_AUDIO = 'открытие аудиопотока'
LOAD_STATION = 'загрузка станции'
LOAD_RELATED = 'загрузка похожих треков'


def track_urn(track):
    return track.get('urn') or f"soundcloud:tracks:{track['id']}"


class SoundCloudError(ValueError):
    pass


class SoundCloudHTTPError(SoundCloudError):
    def __init__(self, status, action):
        self.status = status
        self.action = action
        if status == 401:
            message = 'SoundCloud отклонил авторизацию (401). Войди с новым токеном.'
        elif status == 403:
            message = f'SoundCloud отклонил запрос «{action}» (403).'
        else:
            message = f'Ошибка SoundCloud ({status}): {action}.'
        super().__init__(message)


def read_token(value):
    value = unquote(value.strip()).strip('"')
    value = re.sub(r'^authorization:\s*', '', value, flags=re.IGNORECASE)
    value = re.sub(r'^(oauth(?:\s+|$)|oauth_token=)', '', value, flags=re.IGNORECASE)
    token = ''.join(value.strip('"').split())
    if not token:
        raise SoundCloudError('Вставь токен SoundCloud')
    return token


def web_client_params(html):
    marker = re.search(r'window\.__sc_hydration\s*=\s*', html)
    version = re.search(r'"appVersion"\s*:\s*"?(\d+)', html)
    if not marker or not version:
        raise SoundCloudError('Не удалось прочитать параметры веб-клиента SoundCloud')
    items, _ = json.JSONDecoder().raw_decode(html[marker.end():])
    client_id = next((item['data']['id'] for item in items
                      if item.get('hydratable') == 'apiClient'), None)
    if not client_id:
        raise SoundCloudError('Не удалось определить идентификатор веб-клиента SoundCloud')
    return {'client_id': client_id, 'app_version': version[1], 'app_locale': 'en'}


class SoundCloud:
    api_url = API

    def __init__(self, session_file=None):
        self.session_file = session_file or Path.home() / '.local/share/sound-wave/session.json'
        self.http = None
        self.user_id = None

    @property
    def has_saved_session(self):
        return self.session_file.exists()

    def connect(self, value):
        self._authorize(read_token(value), save=True)

    def sign_out(self):
        self.session_file.unlink(missing_ok=True)
        if self.http:
            self.http.close()
        self.http = self.user_id = None

    def _authorize(self, token, *, save=False):
        candidate = requests.Session()
        try:
            page = candidate.get(SITE + '/', timeout=20)
            self._check(page, 'загрузка сайта')
            candidate.params.update(web_client_params(page.text))
            candidate.headers.update({
                'Authorization': f'OAuth {token}',
                'Accept': 'application/json',
                'Origin': SITE,
                'Referer': SITE + '/',
            })
            user = self._get(candidate, '/me', action='проверка входа')
            user_id = user['id']
            if save:
                self._save_session({'token': token})
        except Exception:
            candidate.close()
            raise

        if self.http:
            self.http.close()
        self.http, self.user_id = candidate, user_id

    def _save_session(self, data):
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        file = NamedTemporaryFile(mode='w', dir=self.session_file.parent, delete=False)
        temporary = Path(file.name)
        try:
            with file:
                json.dump(data, file)
            temporary.replace(self.session_file)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _check(response, action):
        log_http(response, action)
        if response.status_code >= 400:
            raise SoundCloudHTTPError(response.status_code, action)

    def _get(self, http, path, *, action, **params):
        response = http.get(self.api_url + path, params=params, timeout=20)
        self._check(response, action)
        return response.json() if response.content else None

    def restore_session(self):
        if self.http is None:
            if not self.session_file.exists():
                raise SoundCloudError('Войди в SoundCloud')
            saved = json.loads(self.session_file.read_text())
            self._authorize(saved['token'])

    def get(self, path, *, action, **params):
        self.restore_session()
        return self._get(self.http, path, action=action, **params)

    def _collection(self, path, action, *, limit=50, all_pages=False):
        data = self.get(path, action=action, limit=limit, linked_partitioning=1)
        items = data['collection']
        while all_pages and (next_page := data.get('next_href')):
            if not next_page.startswith(self.api_url + '/'):
                break
            page = urlsplit(next_page)
            data = self.get(page.path, action=action, **dict(parse_qsl(page.query)))
            items.extend(data['collection'])
        return items

    def likes(self):
        self.restore_session()
        items = self._collection(f'/users/{self.user_id}/likes', 'загрузка лайков', limit=200, all_pages=True)
        return [item['track'] for item in items if item.get('track')]

    def recommendations(self, seed, station):
        track_id = seed['id']
        if station:
            path = f'/stations/soundcloud:track-stations:{track_id}/tracks'
            return self._collection(path, LOAD_STATION)
        return self._collection(f'/tracks/{track_id}/related', LOAD_RELATED)

    def stream(self, track):
        transcodings = (track.get('media') or {}).get('transcodings', [])
        choices = [item for item in transcodings
                   if (item.get('format') or {}).get('protocol') in ('progressive', 'hls')
                   and (item.get('url') or '').startswith(API + '/')]
        track_key = track.get('urn') or track.get('id')
        if not choices:
            protected = any(((item.get('format') or {}).get('protocol') or '').startswith(('ctr-', 'cbc-'))
                            for item in transcodings)
            reason = 'предложены защищённые потоки' if protected else 'нет подходящего аудиоформата'
            log.debug('Пропущен трек %s: %s', track_key, reason)
            return None
        best = min(choices, key=lambda item: (
            bool(item.get('snipped')), item['format']['protocol'] != 'progressive'))
        log.debug('Формат трека %s: %s', track_key, best['format']['protocol'])
        authorization = track.get('track_authorization')
        params = {'track_authorization': authorization} if authorization else {}
        stream = self.get(best['url'].removeprefix(API), action=GET_AUDIO, **params)
        return stream.get('url') if isinstance(stream, dict) else None

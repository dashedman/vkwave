import asyncio
import typing
import logging
import re
import json
import time

from pprint import pprint, pformat
from bs4 import BeautifulSoup   # TODO: remove
from copy import deepcopy
from http.cookies import SimpleCookie

from .audio import Audio
from ._error import AccessDenied, AudioUrlDecodeError

logger = logging.getLogger(__name__)

RE_ALBUM_ID = re.compile(r'act=audio_playlist(-?\d+)_(\d+)')
RE_ACCESS_HASH = re.compile(r'access_hash=(\w+)')
RE_M3U8_TO_MP3 = re.compile(r'/[0-9a-f]+(/audios)?/([0-9a-f]+)/index.m3u8')

RPS_DELAY_RELOAD_AUDIO = 1.5
RPS_DELAY_LOAD_SECTION = 2.0

TRACKS_PER_USER_PAGE = 2000
TRACKS_PER_ALBUM_PAGE = 2000
ALBUMS_PER_USER_PAGE = 100


class AudioExtended(Audio):
    """ Модуль для получения аудиозаписей без использования официального API.
    :param vk: Объект
    :class:`Category`
    """

    DEFAULT_COOKIES = [
        {   # если не установлено, то первый запрос ломается
            # 'version': 0,
            'name': 'remixaudio_show_alert_today',
            'value': '0',
            'port': None,
            'port_specified': False,
            # 'domain': '.vk.com',
            'domain_specified': True,
            'domain_initial_dot': True,
            # 'path': '/',
            'path_specified': True,
            # 'secure': True,
            # 'expires': None,
            'discard': False,
            # 'comment': None,
            'comment_url': None,
            'rfc2109': False,
            'rest': {}
        }, {  # для аудио из постов
            # 'version': 0,
            'name': 'remixmdevice',
            'value': '1920/1080/2/!!-!!!!',
            'port': None,
            'port_specified': False,
            # 'domain': '.vk.com',
            'domain_specified': True,
            'domain_initial_dot': True,
            # 'path': '/',
            'path_specified': True,
            # 'secure': True,
            # 'expires': None,
            'discard': False,
            # 'comment': None,
            'comment_url': None,
            'rfc2109': False,
            'rest': {}
        }
    ]

    def __init__(self, name, api):
        super().__init__(name, api)
        self.convert_m3u8_links = True
        self.user_id = 0
        self.client = None

    async def set_user_id(self, user_id: int):
        self.user_id = int(user_id)

    async def set_convert_m3u8_links(self, convert_m3u8_links: bool):
        self.convert_m3u8_links = bool(convert_m3u8_links)

    async def set_client_session(self, client):
        self.session = client.http_client.session
        # load cookies
        # for cookies in self.DEFAULT_COOKIES:
        #    self.session.cookie_jar.update_cookies(cookies)
        self.session.cookie_jar.update_cookies({'remixsid': None})
        await self.session.get('https://m.vk.com/')
        for cookie in self.session.cookie_jar:
            print(cookie, cookie.key)

    async def get_iter(self, owner_id=None, album_id=None, access_hash=None) -> typing.AsyncGenerator[dict, None]:
        """ Получить список аудиозаписей пользователя (по частям)
        :param owner_id: ID владельца (отрицательные значения для групп)
        :param album_id: ID альбома
        :param access_hash: ACCESS_HASH альбома
        """

        if owner_id is None:
            owner_id = self.user_id

        if album_id is not None:
            offset_diff = TRACKS_PER_ALBUM_PAGE
        else:
            offset_diff = TRACKS_PER_USER_PAGE

        offset = 0
        while True:
            response = await self.session.post(
                'https://m.vk.com/audio',
                data={
                    'act': 'load_section',
                    'owner_id': owner_id,
                    'playlist_id': album_id if album_id else -1,
                    'offset': offset,
                    'type': 'playlist',
                    'access_hash': access_hash,
                    'is_loading_all': 1
                },
                allow_redirects=False
            )
            raw_result = (await response.json())

            if not raw_result['data'][0]:
                raise AccessDenied(
                    f'You don\'t have permissions to browse {owner_id}\'s audios',
                    {owner_id: owner_id, album_id: album_id, access_hash: access_hash}
                )

            ids = self._scrap_ids(
                raw_result['data'][0]['list']
            )
            if not ids:
                break

            tracks = self._scrap_tracks(ids)

            async for i in tracks:
                yield i

            if raw_result['data'][0]['hasMore']:
                offset += offset_diff
            else:
                break

    async def get_audio(self, owner_id=None, album_id=None, access_hash=None) -> typing.List[dict]:
        """ Получить список аудиозаписей пользователя
        :param owner_id: ID владельца (отрицательные значения для групп)
        :param album_id: ID альбома
        :param access_hash: ACCESS_HASH альбома
        """
        return [audio async for audio in self.get_iter(owner_id, album_id, access_hash)]

    async def get_albums_iter(self, owner_id=None) -> typing.AsyncGenerator[dict, None]:
        """ Получить список альбомов пользователя (по частям)
        :param owner_id: ID владельца (отрицательные значения для групп)
        """

        if owner_id is None:
            owner_id = self.user_id

        offset = 0

        while True:
            response = await self.session.get(
                'https://m.vk.com/audio?act=audio_playlists{}'.format(
                    owner_id
                ),
                params={
                    'offset': offset
                },
                allow_redirects=False
            )

            if not await response.text():
                raise AccessDenied(
                    f'You don\'t have permissions to browse {owner_id}\'s albums',
                    {owner_id: owner_id}
                )

            albums = self._scrap_albums(await response.text())

            if not albums:
                break

            async for i in albums:
                yield i

            offset += ALBUMS_PER_USER_PAGE

    async def get_albums(self, owner_id=None) -> typing.List[dict]:
        """ Получить список альбомов пользователя
        :param owner_id: ID владельца (отрицательные значения для групп)
        """
        return [audio async for audio in self.get_albums_iter(owner_id)]

    async def search_user(self, owner_id=None, q='') -> typing.List[dict]:
        """ Искать по аудиозаписям пользователя
        :param owner_id: ID владельца (отрицательные значения для групп)
        :param q: запрос
        """

        if owner_id is None:
            owner_id = self.user_id

        response = await self.session.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'section',
                'claim': 0,
                'is_layer': 0,
                'owner_id': owner_id,
                'section': 'search',
                'q': q
            }
        )
        json_response = json.loads((await response.text()).replace('<!--', ''))

        if not json_response['payload'][1]:
            raise AccessDenied(
                f'You don\'t have permissions to browse {owner_id}\'s audio',
                {owner_id: owner_id}
            )

        if json_response['payload'][1][1]['playlists']:

            ids = self._scrap_ids(
                json_response['payload'][1][1]['playlists'][0]['list']
            )

            tracks = self._scrap_tracks(ids)

            return [track async for track in tracks]
        else:
            return []

    async def search_iter(self, q, offset=0) -> typing.AsyncGenerator[dict, None]:
        """ Искать аудиозаписи (генератор)
        :param q: запрос
        """
        offset_left = 0

        response = await self.session.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'section',
                'claim': 0,
                'is_layer': 0,
                'owner_id': self.user_id,
                'section': 'search',
                'q': q
            }
        )
        json_response = json.loads((await response.text()).replace('<!--', ''))
        try:
            while json_response['payload'][1][1]['playlist']:

                ids = self._scrap_ids(
                    json_response['payload'][1][1]['playlist']['list']
                )

                if not ids:
                    break
                # len(tracks) <= 100
                if offset_left + len(ids) >= offset:
                    tracks = self._scrap_tracks(
                        ids if offset_left >= offset else ids[offset - offset_left:]
                    )
                    async for track in tracks:
                        yield track
                else:
                    pass

                offset_left += len(ids)
                response = await self.session.post(
                    'https://vk.com/al_audio.php',
                    data={
                        'al': 1,
                        'act': 'load_catalog_section',
                        'section_id': json_response['payload'][1][1]['sectionId'],
                        'start_from': json_response['payload'][1][1]['nextFrom'],
                        'offset': 100
                    }
                )
                json_response = json.loads((await response.text()).replace('<!--', ''))
        except TypeError:
            pprint(self.__dict__)
            pprint(json_response)
            raise

    async def search(self, q, count=100, offset=0) -> typing.List[dict]:
        """ Искать аудиозаписи
        :param q: запрос
        :param count: количество
        """
        audio_list = []
        agen = self.search_iter(q, offset=offset)
        for i in range(count):
            try:
                audio_list.append(await agen.__anext__())
            except StopAsyncIteration:
                pass
        return audio_list

    async def get_popular_iter(self, offset=0) -> typing.AsyncGenerator[dict, None]:
        """ Искать популярные аудиозаписи  (генератор)

        :param offset: смещение
        """
        response = await self.session.post(
            'https://vk.com/audio',
            data={
                'block': 'chart',
                'section': 'recoms'
            }
        )
        json_response = json.loads(self.scrap_json(await response.text()))

        ids = self._scrap_ids(
            json_response['sectionData']['recoms']['playlist']['list']
        )

        # len(tracks) <= 10
        tracks = self._scrap_tracks(
            ids[offset:] if offset else ids
        )
        async for track in tracks:
            yield track

    async def get_news_iter(self, offset=0) -> typing.AsyncGenerator[dict, None]:
        """ Искать популярные аудиозаписи  (генератор)

        :param offset: смещение
        """
        offset_left = 0

        response = await self.session.post(
            'https://vk.com/audio',
            data={
                'block': 'new_songs',
                'section': 'recoms'
            }
        )
        json_response = json.loads(self.scrap_json(await response.text()))

        ids = self._scrap_ids(
            json_response['sectionData']['recoms']['playlist']['list']
        )

        # len(tracks) <= 10
        if offset_left + len(ids) >= offset:
            tracks = self._scrap_tracks(
                ids if offset_left >= offset else ids[offset - offset_left:]
            )
            async for track in tracks:
                yield track

        offset_left += len(ids)

        while True:
            response = await self.session.post(
                'https://vk.com/al_audio.php',
                data={
                    'al': 1,
                    'act': 'load_catalog_section',
                    'section_id': json_response['sectionData']['recoms']['sectionId'],
                    'start_from': json_response['sectionData']['recoms']['nextFrom']
                }
            )

            json_response = json.loads((await response.text()).replace('<!--', ''))

            ids = self._scrap_ids(
                json_response['payload'][1][1]['playlist']['list']
            )
            if not ids:
                break

            # len(tracks) <= 10
            if offset_left + len(ids) >= offset:
                tracks = self._scrap_tracks(
                    ids if offset_left >= offset else ids[offset - offset_left:]
                )
                async for track in tracks:
                    yield track

            offset_left += len(ids)

    async def get_audio_by_id(self, owner_id, audio_id) -> dict:
        """ Получить аудиозапись по ID
        :param owner_id: ID владельца (отрицательные значения для групп)
        :param audio_id: ID аудио
        """
        response = await self.session.get(
            'https://m.vk.com/audio{}_{}'.format(owner_id, audio_id),
            allow_redirects=False
        )

        ids = self._scrap_ids_from_html(
            await response.text(),
            filter_root_el={'class': 'basisDefault'}
        )

        track = self._scrap_tracks(ids)

        try:
            return await track.__anext__()
        except StopAsyncIteration:
            return {}

    async def get_post_audio(self, owner_id, post_id) -> typing.List[dict]:
        """ Получить список аудиозаписей из поста пользователя или группы
        :param owner_id: ID владельца (отрицательные значения для групп)
        :param post_id: ID поста
        """
        response = await self.session.get(
            f'https://m.vk.com/wall{owner_id}_{post_id}'
        )

        ids = self._scrap_ids_from_html(
            await response.text(),
            filter_root_el={'class': 'audios_list'}
        )

        tracks = [track async for track in self._scrap_tracks(ids)]
        return tracks

    # internal functions
    def _scrap_json(self, html_page) -> str:
        """ Парсинг списка хэшей ауфдиозаписей новинок или популярных + nextFrom&sesionId """
        find_json_pattern = r"new AudioPage\(.*?(\{.*\})"
        fr = re.search(find_json_pattern, html_page).group(1)
        return fr

    def _scrap_ids(self, audio_data) -> typing.List[tuple]:
        """ Парсинг списка хэшей аудиозаписей из json объекта """
        ids = []

        for track in audio_data:
            audio_hashes = track[13].split("/")

            full_id = (
                str(track[1]), str(track[0]), audio_hashes[2], audio_hashes[5]
            )
            if all(full_id):
                ids.append(full_id)

        return ids

    def _scrap_ids_from_html(self, html, filter_root_el=None) -> typing.List[tuple]:
        """ Парсинг списка хэшей аудиозаписей из html страницы """

        if filter_root_el is None:
            filter_root_el = {'id': 'au_search_items'}

        soup = BeautifulSoup(html, 'html.parser')
        ids = []

        root_el = soup.find(**filter_root_el)

        if root_el is None:
            raise ValueError('Could not find root el for audio')

        playlist_snippets = soup.find_all('div', {'class': "audioPlaylistSnippet__list"})
        for playlist in playlist_snippets:
            playlist.decompose()

        for audio in root_el.find_all('div', {'class': 'audio_item'}):
            if 'audio_item_disabled' in audio['class']:
                continue

            data_audio = json.loads(audio['data-audio'])
            audio_hashes = data_audio[13].split("/")

            full_id = (
                str(data_audio[1]), str(data_audio[0]), audio_hashes[2], audio_hashes[5]
            )

            if all(full_id):
                ids.append(full_id)

        return ids

    async def _scrap_tracks(self, ids) -> typing.AsyncGenerator[dict, None]:

        last_request = 0.0

        for ids_group in [ids[i:i + 10] for i in range(0, len(ids), 10)]:
            delay = RPS_DELAY_RELOAD_AUDIO - (time.time() - last_request)

            if delay > 0:
                await asyncio.sleep(delay)

            result = await self.session.post(
                'https://m.vk.com/audio',
                data={'act': 'reload_audio', 'ids': ','.join(['_'.join(i) for i in ids_group])}
            ).json()

            last_request = time.time()
            if result['data']:
                data_audio = result['data'][0]
                for audio in data_audio:
                    artist = BeautifulSoup(audio[4], 'html.parser').text
                    title = BeautifulSoup(audio[3].strip(), 'html.parser').text
                    duration = audio[5]
                    link = audio[2]

                    if 'audio_api_unavailable' in link:
                        link = decode_audio_url(link, self.user_id)

                    if self.convert_m3u8_links and 'm3u8' in link:
                        link = RE_M3U8_TO_MP3.sub(r'\1/\2.mp3', link)

                    yield {
                        'id': audio[0],
                        'owner_id': audio[1],
                        'track_covers': audio[14].split(',') if audio[14] else [],
                        'url': link,

                        'artist': artist,
                        'title': title,
                        'duration': duration,
                    }

    async def _scrap_albums(self, html) -> typing.AsyncGenerator[dict, None]:
        """ Парсинг списка альбомов из html страницы """

        soup = BeautifulSoup(html, 'html.parser')
        albums = []

        for album in soup.find_all('div', {'class': 'audioPlaylistsPage__item'}):

            link = album.select_one('.audioPlaylistsPage__itemLink')['href']
            full_id = tuple(int(i) for i in RE_ALBUM_ID.search(link).groups())
            access_hash = RE_ACCESS_HASH.search(link)

            stats_text = album.select_one('.audioPlaylistsPage__stats').text

            # "1 011 прослушиваний"
            try:
                plays = int(stats_text.rsplit(' ', 1)[0].replace(' ', ''))
            except ValueError:
                plays = None

            albums.append({
                'id': full_id[1],
                'owner_id': full_id[0],
                'url': 'https://m.vk.com/audio?act=audio_playlist{}_{}'.format(
                    *full_id
                ),
                'access_hash': access_hash.group(1) if access_hash else None,

                'title': album.select_one('.audioPlaylistsPage__title').text,
                'plays': plays
            })

        return albums


# decoder from vk on js
VK_STR = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN0PQRSTUVWXYZO123456789+/="


def splice(l, a, b, c):
    """ JS's Array.prototype.splice

    var x = [1, 2, 3],
        y = x.splice(0, 2, 1337);

    eq

    x = [1, 2, 3]
    x, y = splice(x, 0, 2, 1337)
    """
    return l[:a] + [c] + l[a + b:], l[a:a + b]


def decode_audio_url(string, user_id):
    vals = string.split("?extra=", 1)[1].split("#")

    tstr = vk_o(vals[0])
    ops_list = vk_o(vals[1]).split('\x09')[::-1]

    for op_data in ops_list:

        split_op_data = op_data.split('\x0b')
        cmd = split_op_data[0]
        if len(split_op_data) > 1:
            arg = split_op_data[1]
        else:
            arg = None

        if cmd == 'v':
            tstr = tstr[::-1]
        elif cmd == 'r':
            tstr = vk_r(tstr, arg)
        elif cmd == 'x':
            tstr = vk_xor(tstr, arg)
        elif cmd == 's':
            tstr = vk_s(tstr, arg)
        elif cmd == 'i':
            tstr = vk_i(tstr, arg, user_id)
        else:
            raise AudioUrlDecodeError(f'Unknown decode cmd: "{cmd}"; Please send bugreport')
    return tstr


def vk_o(string):
    result = []
    index2 = 0

    for s in string:
        sym_index = VK_STR.find(s)

        if sym_index != -1:
            if index2 % 4 != 0:
                i = (i << 6) + sym_index
            else:
                i = sym_index

            if index2 % 4 != 0:
                index2 += 1
                shift = -2 * index2 & 6
                result += [chr(0xFF & (i >> shift))]
            else:
                index2 += 1

    return ''.join(result)


def vk_r(string, i):
    vk_str2 = VK_STR + VK_STR
    vk_str2_len = len(vk_str2)

    result = []

    for s in string:
        index = vk_str2.find(s)

        if index != -1:
            offset = index - int(i)

            if offset < 0:
                offset += vk_str2_len

            result += [vk_str2[offset]]
        else:
            result += [s]

    return ''.join(result)


def vk_xor(string, i):
    xor_val = ord(i[0])

    return ''.join(chr(ord(s) ^ xor_val) for s in string)


def vk_s_child(t, e):
    i = len(t)

    if not i:
        return []

    o = []
    e = int(e)

    for a in range(i - 1, -1, -1):
        e = (i * (a + 1) ^ e + a) % i
        o.append(e)

    return o[::-1]


def vk_s(t, e):
    i = len(t)

    if not i:
        return t

    o = vk_s_child(t, e)
    t = list(t)

    for a in range(1, i):
        t, y = splice(t, o[i - 1 - a], 1, t[a])
        t[a] = y[0]

    return ''.join(t)


def vk_i(t, e, user_id):
    return vk_s(t, int(e) ^ user_id)


"""
by arkhipovkm on 3 May 2020

Получение аудиозаписей простым "скрапингом" html страниц неэффективно и, как оказалось, не годится для получения ссылок.
Предлагаю перейти на на старый добрый PHP'шный вконтактовский бэкэнд- al_audio.php.

Как вы наверняка знаете, Вконтакте, как и Facebook, написан на PHP.
некоторые (многие) функции Вконтакте доступны по соответствующим сервису al_<service_name>.php пути:

    al_wall.php
    al_job.php
    al_profile.php
    al_video.php
    al_audio.php
    и т.д.

Вереницы этих запросов хорошо видны, если зайти на сайт, включить консоль разработчика и во вкладке запросов ввести "al_" в поле фильтра.

В принципе, нет особых причин пользоваться этим бэкендом - Вконтакте и так предоставляет API для разработчиков, дублирующее эти сервисы.
За исключением, конечно, мызыки - ведь она как-раз по API недоступна.

Музыкальный функционал ВК, логично предположить, доступен по пути al_audio.php

У этого сервиса 3 основных "акта" - метода:

    section
    Поисковой метод. Принимает запрос (query) и возвращает результаты поиска: аудиозаписи, альбомы, исполнители.
    На выбор 4 типа секций: search, playlists, recoms, updates - поиск, плейлисты пользователя, рекомендации пользователя, новые добавления друзей пользователя.

        Этот метод не ограничен лимитами

    POST /al_audio.php HTTP/1.1
    Host: vk.com
    Cookie: remixsid=<remixsid>
    Content-Type: application/x-www-form-urlencoded

    al:1
    act:section
    owner_id:<owner_id>
    section:[search || playlists || recoms || updates]
    q:<query>
    performer: 0 || 1 (optional)

        Структура ответа зависит от типа секции

    load_section
    Этот метод используется для загрузки плейлистов (альбомов) - всей информации о плейлисте, в том числе списка аудиозаписей со всей информацией о каждой из них (кроме URL).

        Ограничен лимитом в 20 запросов в 10 секунд

    POST /al_audio.php HTTP/1.1
    Host: vk.com
    Cookie: remixsid=<remixsid>
    Content-Type: application/x-www-form-urlencoded

    al:1
    act:load_section
    owner_id:<owner_id>
    playlist_id:<playlist_id>
    offset:0
    type:playlist
    access_hash:<access_hash>
    is_loading_all:1

    reload_audio
    Этот метод позволяет запросить URL'ы аудиозаписей по их id, до 10 id в запросе.

        Ограничен лимитом в 40 запросов в 60 секунд

    POST /al_audio.php HTTP/1.1
    Host: vk.com
    Cookie: remixsid=<remixsid>
    Content-Type: application/x-www-form-urlencoded

    al:1
    act:reload_audio
    ids:<id>,<id>,<id>

Обратите внимание: единственный требующийся "Сookie" - хорошо нам всем знакомый remixsid, а единственный "header" - Content-Type. API, как видно, не привиредливый.

Типичный "workflow" с этим API таков:

    (опционально). act:section .Поиск алюбомов по поисковому запросу -> получаем список альбомов и список аудиозаписей, подходящих под запрос. Эти альбомы и аудиозаписи - пустые - содержат только неполные id. Нужно загрузить интересущий альбом по id для получения полной информации об альбоме.
    act:load_section. Загрузка полного альбома по его id, полученного из шага 1 или заданного вручную (например аудиозаписи пользователя: <owner_id>_-1). Возвращает полную информацию об альбоме включая список аудиозаписей. Аудиозаписи не имеют URL'ов.
    act:reload_audio. Пере-загрузка аудиозаписей по их id пачками по 10 штук для получения (зашифрованных) URL этих аудиозаписей. Обычно эти id аудиозаписей берутся из пердыдущего шага.

API отвечает простым JSON-ом и его достаточно легко обрабатывать. Приведу для примера ответ на запрос load_section:

<!--{
    "paylod": [
        0, // Error Code; 0 means no error
        [  // Complete playlist object
            {
            "type": "playlist",
            "ownerId": -2000880598,
            "id": 3880598,
            "isOfficial": 1,
            "title": "Simulation Theory",
            "titleLang": 3,
            "subTitle": "Super Deluxe",
            "description": "",
            "rawDescription": "",
            "authorLine": "<a class=\"audio_pl_snippet__artist_link\" href=\"https://vk.com/artist/muse\">Muse</a>",
            "authorHref": "",
            "authorName": "<a class=\"audio_pl_snippet__artist_link\" href=\"https://vk.com/artist/muse\">Muse</a>",
            "infoLine1": "Альтернатива<span class=\"dvd\"></span>2018",
            "infoLine2": "2,388,485 plays<span class=\"dvd\"></span>21 tracks",
            "lastUpdated": 1566877650,
            "listens": "2388485",
            "coverUrl": "https://sun9-6.userapi.com/c849216/v849216671/15bf9e/W7-U7QQReW4.jpg",
            "editHash": "",
            "isFollowed": false,
            "followHash": "fe9a577548cde795d9",
            "accessHash": "9b580930deb7e8fff9",
            "addClasses": "audio_pl__has_thumb audio_pl__canfollow official audio_numeric audio_pl__type_main_only audio_pl__has_shuffle audio_pl__has_subtitle audio_pl__can_add_to_clubs",
            "gridCovers": "",
            "isBlocked": false,
            "list": [
                [
                    45200941,
                    -2001200941,
                    "",
                    "Algorithm",
                    "Muse",
                    246,
                    -1,
                    0,
                    "",
                    0,
                    66,
                    "",
                    "[]",
                    "8e432c93bee2c9890b//276f6fedee8f006daf///edb03441653b58741c/",
                    "https://sun9-41.userapi.com/c849216/v849216671/15bfa4/NYSqlB1vjNk.jpg,https://sun9-38.userapi.com/c849216/v849216671/15bfa1/igEKSaWB7eA.jpg",
                    {
                        "duration": 246,
                        "content_id": "-2001200941_45200941",
                        "puid22": 14,
                        "account_age_type": 3,
                        "_SITEID": 276,
                        "vk_id": 0,
                        "ver": 251116
                    },
                    "",
                    [
                        {
                            "id": "8099015328710656381",
                            "name": "Muse"
                        }
                    ],
                    "",
                    [
                        -2000880598,
                        3880598,
                        "9b580930deb7e8fff9"
                    ],
                    "d867bf7fR0hEm9h5CWqmPjx7h7GpfL4rpOKtvzW9seTA_VfYfeud_4MCvA",
                    0,
                    1,
                    true,
                    "945b0eed31293b1abe",
                    false
                ],
                // ... other audio objects
            ],
            "hasMore": 0,
            "nextOffset": 2000,
            "blockId": false,
            "totalCount": 21,
            "totalCountHash": ""
        },
            "html blob"
        ]
    ],
    "statsMeta": {}, // etc
    "langPack" // etc
    // ...
    // other metadata and template fields
}

Поле list - список аудиозаписей плейлиста (все аудиозаписи до nextOffset, который по умолчанию - 2000). Сами аудиозаписи представлены в привычном формате простого списка - такой же, как и в атрибутах data-audio.

Этот список распаковывается в объект, используя индекс:

{
    "AUDIO_ITEM_INDEX_ID":           0,
    "AUDIO_ITEM_INDEX_OWNER_ID":     1,
    "AUDIO_ITEM_INDEX_URL":          2,
    "AUDIO_ITEM_INDEX_TITLE":        3,
    "AUDIO_ITEM_INDEX_PERFORMER":    4,
    "AUDIO_ITEM_INDEX_DURATION":     5,
    "AUDIO_ITEM_INDEX_ALBUM_ID":     6,
    "AUDIO_ITEM_INDEX_AUTHOR_LINK":  8,
    "AUDIO_ITEM_INDEX_LYRICS":       9,
    "AUDIO_ITEM_INDEX_FLAGS":        10,
    "AUDIO_ITEM_INDEX_CONTEXT":      11,
    "AUDIO_ITEM_INDEX_EXTRA":        12,
    "AUDIO_ITEM_INDEX_HASHES":       13,
    "AUDIO_ITEM_INDEX_COVER_URL":    14,
    "AUDIO_ITEM_INDEX_ADS":          15,
    "AUDIO_ITEM_INDEX_SUBTITLE":     16,
    "AUDIO_ITEM_INDEX_MAIN_ARTISTS": 17,
    "AUDIO_ITEM_INDEX_FEAT_ARTISTS": 18,
    "AUDIO_ITEM_INDEX_ALBUM":        19,
    "AUDIO_ITEM_INDEX_TRACK_CODE":   20,

    "AUDIO_ITEM_HAS_LYRICS_BIT":     1,
    "AUDIO_ITEM_CAN_ADD_BIT":        2,
    "AUDIO_ITEM_CLAIMED_BIT":        4,
    "AUDIO_ITEM_HQ_BIT":             16,
    "AUDIO_ITEM_LONG_PERFORMER_BIT": 32,
    "AUDIO_ITEM_UMA_BIT":            128,
    "AUDIO_ITEM_REPLACEABLE":        512,
    "AUDIO_ITEM_EXPLICIT_BIT":       1024,
}

В конце индеска присутствуют "маски" флагов. Их нужно битово помножить на значение "AUDIO_ITEM_INDEX_FLAGS" для получения соответствующего bool значения, например

hasLyrics = data_audio[index["AUDIO_ITEM_INDEX_FLAGS"]] & index["AUDIO_ITEM_HAS_LYRICS_BIT"]

В свою очередь, HASHES (элемент [13]), раскладывается на следующие "хэши" разложением по слэшу "/" со следующими индексами:

{
    "addHash": 0,
    "editHash": 1,
    "actionHash": 2,
    "deleteHash": 3,
    "teplaceHash": 4,
    "urlHash": 5
}

Например хэши из примера выше - 8e432c93bee2c9890b//276f6fedee8f006daf///edb03441653b58741c/ - разложатся следующим образом:
слэшу "/":

{
    "addHash": "8e432c93bee2c9890b",
    "editHash": "",
    "actionHash": "276f6fedee8f006daf",
    "deleteHash": "",
    "replaceHash": "",
    "urlHash": "edb03441653b58741c"
}

После всей этой обработки, получается объект аудиозаписи (названия полей исключительно на усмотрение имплементирующего):

{
    "AudioID": 45200941,
    "OwnerID": -2001200941,
    "Title": "Algorithm",
    "Subtitle": "",
    "Performer": "Muse",
    "Duration": 246,
    "Lyrics": 0,
    "URL": "",
    "Context": "",
    "Extra": "[]",
    "AddHash": "8e432c93bee2c9890b",
    "EditHash": "",
    "ActionHash": "276f6fedee8f006daf",
    "DeleteHash": "",
    "ReplaceHash": "",
    "URLHash": "edb03441653b58741c",
    "CoverURLs": "https://sun6-19.userapi.com/c849216/v849216671/15bfa4/NYSqlB1vjNk.jpg",
    "CoverURLp": "https://sun6-14.userapi.com/c849216/v849216671/15bfa1/igEKSaWB7eA.jpg",
    "CanEdit": false,
    "CanDelete": false,
    "CanAdd": false,
    "IsLongPerformer": false,
    "IsClaimed": false,
    "IsExplicit": false,
    "IsUMA": false,
    "IsReplaceable": false,
    "Album": "-2000880598_3880598_9b580930deb7e8fff9",
    "AlbumID": -1,
    "TrackCode": "8ea2740blEmHtZUq-i6iNAnsl3cdpog"
}

    CoverURLs и CoverURLp: мальнькая и средняя обложки альбома. Полноразмерная обложка находится в поле "CoverUrl" самого плейлиста (предыдущий ответ)

В этом объекте не хватает нескольких полей, в том числе URL, это нормально. Теперь нужно отправить запрос reload_audio указав id аудио, перечисленных через запятую.

id аудиозаписи складывается следующим образом: <ownerID>_<audioID>_<actionHash>_<URLHash>.

    Если не хватает какого-нибудь хэша хотя бы в одном id из 10 возможных - АПИ выдаст ошибку на весь запрос.

Пример ответа на запрос reload_audio:

<!--{
    "payload": [
        0, // Error Code. 0 - no error
        [  // Array of requested audios, here we requested only one.
            [
                [
                    45200941,
                    -2001200941,
                    "https://vk.com/mp3/audio_api_unavailable.mp3?extra=yu51n182ntHWuZnqowvUngiUEgrmohvOqwzLAfKWxOmZzLLTzMHoAu1Hmffkuc1Vyt0Yvs91tMfMmc1imd1QnZr1nevjAMrZDLq5A19lzgnYre94vdvSnMKTDuLArgiTAhq5nI9SmuHvtJjstZyXBNnOC1i1DxyVDe42vNGUveTYnLHSlKS4EunvowvRm2fMAKH3BuLLqxyVDfPWyZqYtI9FwwTpoeK4n3KWBxjFnKDbtMHczurFrd8Wy3rLrdLyBw9rsgLLnZH0C292uuu5vwzuAZe1BI1eAwvkwM1wv21nwMGVuG#AqSOota",
                    "Algorithm",
                    "Muse",
                    246,
                    -1,
                    0,
                    "",
                    0,
                    66,
                    "",
                    "[]",
                    "8e432c93bee2c9890b//276f6fedee8f006daf///edb03441653b58741c/",
                    "https://sun6-19.userapi.com/c849216/v849216671/15bfa4/NYSqlB1vjNk.jpg,https://sun6-14.userapi.com/c849216/v849216671/15bfa1/igEKSaWB7eA.jpg",
                    {
                        "duration": 246,
                        "content_id": "-2001200941_45200941",
                        "puid22": 14,
                        "account_age_type": 3,
                        "_SITEID": 276,
                        "vk_id": 0,
                        "ver": 251116
                    },
                    "",
                    [
                        {
                            "id": "8099015328710656381",
                            "name": "Muse"
                        }
                    ],
                    "",
                    [
                        -2000880598,
                        3880598,
                        "9b580930deb7e8fff9"
                    ],
                    "7a4949900lsmB7Y1u_uW1wcFWIaiO34",
                    0,
                    0,
                    true,
                    "945b0eed31293b1abe",
                    false
                ]
            ],
            false,
            "b9b7eb1b5dfd9870af",
            []
        ]
    ],
    "statsMeta": {}, // etc
    "loaderVersion": "" // etc
    // ...
    // etc
}

В ответе мы находим то же самое "сырое" аудио, что уже получили раньще в составе плейлиста, но уже с (зашифрованной) URL. Осталось только расшифровать хорошо нам всем знакомым перестановочным шифром цезаря и заменить ссылку с .m3u8 (HLS) на .mp3.

Последний пункт удобен и быстр, но необязателен. Трек можно скачать и "сшить" в .mp3 использую FFmpeg (#298 )

Лимит в 40 запросов в минуту не позволяет таким образом в одночасье "скачать" большие алюбомы (личные аудиозаписи некоторых заядлых меломанов), но подходит для подавляющего больщинства пользователей и уж тем более студийных альбомов.

Если все-таки нужно сделать много запросов в ограниченное время, то есть решение. Сервер применяет лимиты на запросы не по IP адресу, а по remixsid. Если в какой-то момент программа "уперлась" в потолок - можно просто переавторизоваться, или иметь "под боком" несколько предавторизованных remixsid токенов и подменить его на условном 41-ом запросе reload_audio.
"""

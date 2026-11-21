import threading
from collections import deque

import gi
import requests

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gio, GLib, Gst, Gtk

from artwork import fetch_image, texture
from diagnostics import configure_debug, debug_enabled, log
from mpris import MediaControls
from oauth_soundcloud import OAuthSoundCloud, soundcloud_client
from soundcloud import SoundCloudError, track_urn
from wave_queue import Wave

WINDOW_SIZE = 260
MAX_PLAYBACK_FAILURES = 5
WAVE_MODES = {"station": "Станция SoundCloud", "related": "Похожие по лайкам"}
STYLE = """
window.wave-window {
    background: transparent;
    padding: 0;
    border: 0;
    box-shadow: none;
}
.wave-frame, .wave-player, .wave-cover, .wave-cover picture, .wave-shade {
    border-radius: 16px;
}
.wave-frame {
    background-color: var(--window-bg-color);
}
.wave-player {
    color: white;
    background-color: #222226;
}
.wave-cover {
    padding: 0;
    border: 0;
    background: transparent;
    box-shadow: none;
}
.wave-shade {
    background-color: rgba(0, 0, 0, 0.52);
}
.wave-toolbar {
    padding: 4px;
}
.wave-toolbar.on-cover, .wave-toolbar.on-cover button {
    color: white;
}
.wave-details {
    padding: 6px 12px 10px;
}
.wave-status {
    color: white;
    padding: 3px 6px;
    font-size: 0.86em;
}
.wave-title {
    color: white;
    padding: 0;
    min-height: 0;
    font-size: 0.95em;
    font-weight: bold;
}
.wave-controls button {
    color: white;
}
"""


class ScrollingLabel(Gtk.ScrolledWindow):
    def __init__(self, text, css_class):
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        self.label = Gtk.Label(label=text, single_line_mode=True, xalign=0)
        self.label.add_css_class(css_class)
        self.set_child(self.label)
        self.scroll = self.get_hadjustment()
        self.animation = Adw.TimedAnimation.new(
            self, 0, 0, 0, Adw.PropertyAnimationTarget.new(self.scroll, "value")
        )
        self.animation.set_easing(Adw.Easing.LINEAR)
        self.animation.set_repeat_count(0)
        self.animation.set_alternate(True)
        self.scroll.connect("changed", self.update_animation)
        self.connect("map", self.update_animation)

    def set_text(self, text):
        self.label.set_text(text)
        self.update_animation()

    def update_animation(self, *_):
        self.animation.reset()
        distance = self.scroll.get_upper() - self.scroll.get_page_size()
        self.set_tooltip_text(self.label.get_text() if distance > 0 else None)
        if distance > 0 and self.get_mapped():
            self.animation.set_value_to(distance)
            self.animation.set_duration(max(1, round(distance * 40)))
            self.animation.play()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(
            application=app, title="SoundWave", decorated=False, resizable=False,
            default_width=WINDOW_SIZE, default_height=WINDOW_SIZE,
        )
        self.add_css_class("wave-window")
        self.wave = Wave(soundcloud_client())
        self.signed_in = False
        self.track = None
        self.jobs = deque()
        self.pending_jobs = set()
        self.active_job = None
        self.closed = False
        self.playback_generation = 0
        self.playing = False
        self.playback_failures = 0
        self.history_timer = None
        self.history_recorded = False
        self.pointer_watch = None
        self.debug = debug_enabled()
        self.player = Gst.ElementFactory.make("playbin")
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)

        self.frame = Gtk.Overlay(overflow=Gtk.Overflow.HIDDEN)
        self.frame.add_css_class("wave-frame")
        self.frame.set_size_request(WINDOW_SIZE, WINDOW_SIZE)
        self.frame.set_child(Gtk.Box())
        self.screen = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.screen.add_named(self.build_player(), "player")
        self.screen.add_named(self.build_login(), "login")
        self.frame.add_overlay(self.screen)
        self.status = Gtk.Label(wrap=True, valign=Gtk.Align.START, margin_top=44,
                                can_target=False, visible=False)
        self.status.add_css_class("wave-status")
        self.frame.add_overlay(self.status)
        self.header = self.build_header()
        self.frame.add_overlay(self.header)
        self.pointer = Gtk.EventControllerMotion()
        self.pointer.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.pointer.connect("notify::contains-pointer", self.reveal_controls)
        self.pointer.connect("motion", self.on_pointer_motion)
        self.add_controller(self.pointer)
        self.toasts = Adw.ToastOverlay(child=self.frame)
        self.copy_toast = Adw.Toast(title="Скопировано", timeout=2)
        self.set_child(self.toasts)
        self.media = MediaControls(self)
        self.connect("close-request", self.on_close)
        self.show_screen()
        self.update_controls()
        if self.wave.api.has_saved_session:
            self.restore_login()

    def build_player(self):
        artwork = Gtk.Overlay(overflow=Gtk.Overflow.HIDDEN)
        artwork.add_css_class("wave-player")
        self.cover = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self.cover.set_can_target(False)
        cover_button = Gtk.Button(child=self.cover, has_frame=False)
        cover_button.add_css_class("wave-cover")
        cover_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Воспроизведение / пауза"]
        )
        cover_button.connect("clicked", self.on_play)
        artwork.set_child(cover_button)
        self.cover_placeholder = Gtk.Image(
            icon_name="audio-x-generic-symbolic", pixel_size=54,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, can_target=False,
        )
        self.cover_placeholder.add_css_class("dim-label")
        artwork.add_overlay(self.cover_placeholder)

        shade = Gtk.Box()
        shade.add_css_class("wave-shade")
        self.shade = Gtk.Revealer(
            child=shade, can_target=False,
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            transition_duration=200,
        )
        artwork.add_overlay(self.shade)

        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                               valign=Gtk.Align.END)
        self.details.add_css_class("wave-details")
        self.title_label = ScrollingLabel("Твоя волна", "heading")
        self.title_label.set_can_target(False)
        self.title_label.set_focusable(False)
        self.copy_title = Gtk.Button(child=self.title_label, has_frame=False)
        self.copy_title.add_css_class("wave-title")
        self.copy_title.connect("clicked", self.copy_track)
        self.details.append(self.copy_title)
        self.artist_label = ScrollingLabel("Музыка на основе твоих лайков", "dim-label")
        self.details.append(self.artist_label)
        artwork.add_overlay(self.details)
        controls = Gtk.CenterBox(margin_start=10, margin_end=10,
                                 valign=Gtk.Align.CENTER)
        controls.add_css_class("wave-controls")
        self.track_link = Gtk.LinkButton(
            uri="",
            icon_name="insert-link-symbolic",
            tooltip_text="Открыть трек на SoundCloud",
            valign=Gtk.Align.CENTER,
        )
        self.track_link.add_css_class("flat")
        self.track_link.add_css_class("circular")
        self.track_link.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Открыть трек на SoundCloud"]
        )
        controls.set_start_widget(self.track_link)
        self.play = Gtk.Button(valign=Gtk.Align.CENTER)
        self.play.add_css_class("flat")
        self.play.add_css_class("circular")
        self.play.connect("clicked", self.on_play)
        controls.set_center_widget(self.play)
        self.next = Gtk.Button(
            icon_name="media-skip-forward-symbolic",
            tooltip_text="Следующий трек",
            valign=Gtk.Align.CENTER,
        )
        self.next.add_css_class("flat")
        self.next.add_css_class("circular")
        self.next.connect("clicked", self.request_next_track)
        controls.set_end_widget(self.next)
        self.transport = controls
        artwork.add_overlay(controls)
        return artwork

    def resize_square(self, size):
        self.frame.set_size_request(size, size)
        self.set_default_size(size, size)

    def copy_track(self, *_):
        if self.track:
            title = self.title_label.label.get_text()
            artist = self.artist_label.label.get_text()
            self.get_clipboard().set(f"{title}\n{artist}" if artist else title)
            self.toasts.add_toast(self.copy_toast)

    def reveal_controls(self, *_):
        surface = self.get_surface()
        seat = self.get_display().get_default_seat()
        pointer = seat.get_pointer() if seat else None
        pointer_inside = (
            surface is not None and pointer is not None
            and pointer.get_surface_at_position()[0] == surface
        )
        show_chrome = not self.signed_in or not self.track or pointer_inside or self.mode.get_active()
        for widget in (self.header, self.details, self.transport):
            widget.set_opacity(float(show_chrome))
        self.shade.set_reveal_child(
            self.signed_in and show_chrome and (self.track is not None or bool(self.status.get_text()))
        )
        self.status.set_visible(
            self.debug and self.signed_in and show_chrome and bool(self.status.get_text())
        )
        if self.track and show_chrome and self.pointer_watch is None:
            # Resizing while a popover closes can suppress the pointer leave event.
            self.pointer_watch = GLib.timeout_add(100, self.watch_pointer)

    def watch_pointer(self):
        if self.closed or not self.track or not self.header.get_opacity():
            self.pointer_watch = None
            return GLib.SOURCE_REMOVE
        self.reveal_controls()
        return GLib.SOURCE_CONTINUE

    def on_pointer_motion(self, *_):
        if not self.header.get_opacity():
            self.reveal_controls()

    def build_login(self):
        oauth = isinstance(self.wave.api, OAuthSoundCloud)
        form = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            valign=Gtk.Align.CENTER,
            margin_start=16,
            margin_end=16,
            margin_top=8,
            margin_bottom=12,
        )
        title = Gtk.Label(label="Вход в SoundCloud")
        title.add_css_class("title-4")
        form.append(title)
        self.login_prompt = (
            "Вставь refresh-токен своего приложения SoundCloud."
            if oauth
            else "Войди на сайте. Скопируй oauth_token из Cookies в инструментах разработчика."
        )
        self.login_hint = Gtk.Label(
            label=self.login_prompt,
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        self.login_hint.add_css_class("dim-label")
        form.append(self.login_hint)
        form.append(
            Gtk.LinkButton(
                uri="https://soundcloud.com/",
                label="Открыть SoundCloud",
                halign=Gtk.Align.CENTER,
            )
        )
        self.token_entry = Gtk.PasswordEntry(
            show_peek_icon=True,
            placeholder_text="refresh_token" if oauth else "oauth_token",
        )
        self.token_entry.connect("activate", self.submit_login)
        self.token_entry.connect("changed", self.login_text_changed)
        form.append(self.token_entry)
        self.login = Gtk.Button(label="Войти", halign=Gtk.Align.CENTER)
        self.login.add_css_class("suggested-action")
        self.login.connect("clicked", self.submit_login)
        form.append(self.login)
        return Gtk.ScrolledWindow(
            child=form, hscrollbar_policy=Gtk.PolicyType.NEVER, margin_top=46,
        )

    def build_header(self):
        header = Gtk.CenterBox(valign=Gtk.Align.START)
        header.add_css_class("wave-toolbar")
        self.wave_mode = Gio.SimpleAction.new_stateful(
            "wave-mode", GLib.VariantType.new("s"), GLib.Variant("s", "station")
        )
        self.wave_mode.connect("change-state", self.change_mode)
        self.add_action(self.wave_mode)
        self.logout = Gio.SimpleAction.new("logout", None)
        self.logout.connect("activate", self.on_sign_out)
        self.add_action(self.logout)
        modes = Gio.Menu()
        for key, label in WAVE_MODES.items():
            modes.append(label, f"win.wave-mode::{key}")
        menu = Gio.Menu()
        menu.append_section(None, modes)
        sizes = Gio.Menu()
        for size in (180, 220, WINDOW_SIZE):
            name = f"size-{size}"
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self.change_size, size)
            self.add_action(action)
            sizes.append(f"{size} × {size}", f"win.{name}")
        menu.append_section("Размер окна", sizes)
        account_actions = Gio.Menu()
        account_actions.append("Выйти из SoundCloud", "win.logout")
        menu.append_section(None, account_actions)
        self.mode = Gtk.MenuButton(
            icon_name="open-menu-symbolic", has_frame=False,
            tooltip_text="Меню SoundWave", menu_model=menu,
        )
        self.mode.connect("notify::active", self.reveal_controls)
        self.mode.update_property([Gtk.AccessibleProperty.LABEL], ["Меню SoundWave"])
        header.set_start_widget(self.mode)
        title = Gtk.Label(label="SoundWave")
        title.add_css_class("heading")
        header.set_center_widget(Gtk.WindowHandle(child=title, hexpand=True))
        close = Gtk.Button(icon_name="window-close-symbolic", has_frame=False,
                           tooltip_text="Закрыть")
        close.connect("clicked", lambda *_: self.close())
        header.set_end_widget(close)
        return header

    def change_size(self, _action, _parameter, size):
        self.mode.popdown()
        self.resize_square(size)

    def show_screen(self):
        self.screen.set_visible_child_name("player" if self.signed_in else "login")
        self.mode.set_visible(self.signed_in)
        if self.signed_in:
            self.header.add_css_class("on-cover")
        else:
            self.header.remove_css_class("on-cover")
            self.resize_square(WINDOW_SIZE)
        self.reveal_controls()

    def can_play(self):
        return self.signed_in and "logout" not in self.pending_jobs

    def restore_login(self):
        self.login_feedback("Проверяем сохранённый вход…")
        self.run("login", self.wave.api.profile, self.show_player, self.login_feedback)

    def submit_login(self, *_):
        if "login" in self.pending_jobs:
            return
        token = self.token_entry.get_text().strip()
        if not token:
            if self.wave.api.has_saved_session:
                self.restore_login()
            return
        self.login_feedback("Проверяем вход…")
        self.run(
            "login",
            lambda: self.wave.connect(token),
            self.connected,
            self.login_feedback,
        )

    def login_feedback(self, message):
        self.login_hint.set_text(message or self.login_prompt)

    def login_text_changed(self, *_):
        if "login" not in self.pending_jobs:
            self.login_feedback("")
        self.update_controls()

    def change_mode(self, action, value):
        mode = value.get_string()
        if action.get_state().get_string() == mode:
            return
        action.set_state(value)
        if self.track or ("track", self.playback_generation) in self.pending_jobs:
            self.next_track()

    def connected(self, profile):
        self.request_stop()
        self.show_player(profile)
        self.set_status("SoundCloud подключён")

    def show_player(self, _profile):
        self.signed_in = True
        self.login_feedback("")
        self.token_entry.set_text("")
        self.show_screen()

    def on_sign_out(self, *_):
        self.mode.popdown()
        self.run("logout", self.wave.sign_out, self.signed_out)

    def signed_out(self, _result):
        self.signed_in = False
        self.request_stop()
        self.show_screen()
        self.set_status("Вы вышли из SoundCloud")

    @staticmethod
    def load_image(url, apply):
        def worker():
            pixbuf = fetch_image(url)
            GLib.idle_add(apply, pixbuf)

        if url:
            threading.Thread(target=worker, daemon=True).start()

    def show_cover(self, pixbuf):
        self.cover.set_paintable(texture(pixbuf))
        self.cover_placeholder.set_visible(pixbuf is None)

    def update_controls(self):
        saved = self.wave.api.has_saved_session
        token_entered = bool(self.token_entry.get_text().strip())
        self.login.set_label(
            "Проверить вход" if saved and not token_entered else "Войти"
        )
        self.login.set_sensitive(
            "login" not in self.pending_jobs and (token_entered or saved)
        )
        self.logout.set_enabled("logout" not in self.pending_jobs)
        self.play.set_sensitive(self.can_play() or self.track is not None)
        self.play.set_icon_name(
            "media-playback-pause-symbolic"
            if self.playing
            else "media-playback-start-symbolic"
        )
        play_action = (
            "Пауза"
            if self.playing
            else "Продолжить"
            if self.track
            else "Запустить волну"
        )
        self.play.set_tooltip_text(play_action)
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [play_action])
        self.next.set_visible(self.track is not None)
        self.next.set_sensitive(self.can_play())
        permalink = (self.track or {}).get("permalink_url") or ""
        if self.track_link.get_uri() != permalink:
            self.track_link.set_uri(permalink)
        self.track_link.set_visible(bool(permalink))
        self.copy_title.set_can_target(self.track is not None)
        self.copy_title.set_focusable(self.track is not None)
        self.copy_title.set_tooltip_text(
            "Скопировать название и исполнителя" if self.track else None
        )
        self.reveal_controls()
        self.media.changed()

    def set_status(self, message):
        if self.debug:
            self.status.set_text(message)
            self.reveal_controls()
            if message:
                log.debug("%s", message)

    def run(self, key, work, done, failed=None):
        if key in self.pending_jobs:
            return
        self.pending_jobs.add(key)
        self.jobs.append((key, work, done, failed))
        self.update_controls()
        self.start_job()

    def start_job(self):
        if self.closed or self.active_job is not None or not self.jobs:
            return
        key, work, done, failed = self.jobs.popleft()
        self.active_job = key
        self.set_status("Загрузка…")

        def finish(result, error):
            if self.closed:
                return
            self.active_job = None
            self.pending_jobs.remove(key)
            self.set_status(error or "")
            if error and failed:
                failed(error)
            elif not error:
                done(result)
            self.update_controls()
            self.start_job()

        def worker():
            try:
                result = work()
            except SoundCloudError as error:
                GLib.idle_add(finish, None, str(error))
            except requests.RequestException:
                GLib.idle_add(finish, None, "Не удалось связаться с SoundCloud")
            except (ValueError, KeyError, TypeError):
                GLib.idle_add(finish, None, "Не удалось прочитать данные SoundCloud")
            except OSError:
                GLib.idle_add(finish, None, "Не удалось открыть или сохранить сессию")
            else:
                GLib.idle_add(finish, result, None)

        threading.Thread(target=worker, daemon=True).start()

    def next_track(self):
        if not self.can_play():
            return
        station = self.wave_mode.get_state().get_string() == "station"
        generation = self.playback_generation

        def apply(result):
            if generation != self.playback_generation or not self.can_play():
                return
            if station != (self.wave_mode.get_state().get_string() == "station"):
                self.next_track()
            elif result:
                self.show_track(result, autoplay=self.track is None or self.playing)

        log.debug(
            "Подбор следующего трека: %s", "станция" if station else "похожие по лайкам"
        )
        self.run(
            ("track", generation),
            lambda: self.wave.next_track(station),
            apply,
            lambda _error: apply(None),
        )

    def request_next_track(self, *_):
        self.playback_failures = 0
        self.next_track()

    def request_stop(self):
        self.playback_generation += 1
        self.stop_playback()
        self.update_controls()

    def stop_playback(self):
        self.cancel_history_timer()
        self.player.set_state(Gst.State.NULL)
        self.media.reset_stream()
        self.track = None
        self.playing = False
        self.show_cover(None)
        self.title_label.set_text("Твоя волна")
        self.artist_label.set_text("Музыка на основе твоих лайков")

    def show_track(self, result, *, autoplay=True):
        track, url = result
        self.cancel_history_timer()
        self.history_recorded = False
        self.track = track
        log.debug("Открытие потока в GStreamer: трек %s", track_urn(track))
        self.player.set_state(Gst.State.NULL)
        self.media.reset_stream()
        self.player.set_property("uri", url)
        self.playing = autoplay
        self.player.set_state(Gst.State.PLAYING if autoplay else Gst.State.PAUSED)
        self.title_label.set_text(track["title"])
        self.artist_label.set_text((track.get("user") or {}).get("username", ""))
        self.show_cover(None)

        def apply(pixbuf):
            if self.track is track:
                self.show_cover(pixbuf)

        self.load_image(track.get("artwork_url"), apply)

    def on_play(self, *_):
        if not self.track:
            self.request_next_track()
        else:
            self.playing = not self.playing
            if not self.playing:
                self.cancel_history_timer()
            self.player.set_state(
                Gst.State.PLAYING if self.playing else Gst.State.PAUSED
            )
            self.update_controls()

    def advance_track(self):
        self.stop_playback()
        self.update_controls()
        self.next_track()

    def on_close(self, *_):
        self.closed = True
        self.jobs.clear()
        self.cancel_history_timer()
        self.player.set_state(Gst.State.NULL)
        self.media.close()
        return False

    def cancel_history_timer(self):
        if self.history_timer is not None:
            GLib.source_remove(self.history_timer)
            self.history_timer = None

    def watch_play_history(self):
        if self.history_recorded or self.history_timer is not None:
            return

        def check_position():
            available, position = self.player.query_position(Gst.Format.TIME)
            if not available or position < Gst.SECOND:
                return GLib.SOURCE_CONTINUE
            self.history_timer = None
            self.record_play()
            return GLib.SOURCE_REMOVE

        self.history_timer = GLib.timeout_add(250, check_position)

    def record_play(self):
        self.cancel_history_timer()
        if self.history_recorded or self.track is None:
            return
        self.history_recorded = True
        job = self.wave.api.play_history_job(self.track)
        if job is not None:
            threading.Thread(target=job, daemon=True).start()

    def on_message(self, _bus, message):
        if not self.track:
            return
        if message.type == Gst.MessageType.ASYNC_DONE and message.src == self.player:
            self.media.stream_changed()
            self.media.seek_done()
        elif message.type == Gst.MessageType.DURATION_CHANGED:
            self.media.stream_changed()
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == self.player:
            _, state, _ = message.parse_state_changed()
            if state == Gst.State.PLAYING:
                self.playback_failures = 0
                log.debug(
                    "GStreamer начал воспроизведение трека %s", track_urn(self.track)
                )
                if self.playing:
                    self.watch_play_history()
            else:
                self.cancel_history_timer()
        elif message.type == Gst.MessageType.EOS:
            if self.history_timer is not None:
                self.record_play()
            self.playback_failures = 0
            self.advance_track()
        elif message.type == Gst.MessageType.ERROR:
            error, _ = message.parse_error()
            log.debug(
                "Ошибка GStreamer для трека %s: %s/%d",
                track_urn(self.track),
                error.domain,
                error.code,
            )
            source_error = any(
                error.matches(Gst.ResourceError.quark(), code)
                for code in (
                    Gst.ResourceError.NOT_AUTHORIZED,
                    Gst.ResourceError.NOT_FOUND,
                    Gst.ResourceError.OPEN_READ,
                    Gst.ResourceError.READ,
                )
            ) or error.domain == GLib.quark_to_string(Gst.StreamError.quark())
            self.playback_failures += 1
            if source_error and self.playback_failures < MAX_PLAYBACK_FAILURES:
                log.debug(
                    "Пропуск после ошибки GStreamer, попытка %d", self.playback_failures
                )
                self.advance_track()
                return
            self.stop_playback()
            self.update_controls()
            self.set_status("Не удалось воспроизвести трек")


class App(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="ru.falbue.SoundWave", flags=Gio.ApplicationFlags.NON_UNIQUE
        )

    def do_activate(self):
        if window := self.get_active_window():
            window.present()
            return
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")
        window = Window(self)
        style = Gtk.CssProvider()
        style.load_from_string(STYLE)
        Gtk.StyleContext.add_provider_for_display(
            window.get_display(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        window.present()


if __name__ == "__main__":
    configure_debug()
    Gst.init(None)
    App().run(None)

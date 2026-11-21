from gi.repository import Gio, GLib, Gst
from soundcloud import track_urn

ROOT = "org.mpris.MediaPlayer2"
PLAYER = ROOT + ".Player"
PATH = "/org/mpris/MediaPlayer2"

ROOT_PROPERTIES = {
    "CanQuit": GLib.Variant("b", True),
    "CanRaise": GLib.Variant("b", True),
    "HasTrackList": GLib.Variant("b", False),
    "Identity": GLib.Variant("s", "SoundWave"),
    "SupportedUriSchemes": GLib.Variant("as", []),
    "SupportedMimeTypes": GLib.Variant("as", []),
}

METHODS = ("Next", "Previous", "Pause", "PlayPause", "Stop", "Play")
PLAYER_TYPES = {
    "PlaybackStatus": "s",
    "Metadata": "a{sv}",
    "Position": "x",
    "Volume": "d",
    "Rate": "d",
    "MinimumRate": "d",
    "MaximumRate": "d",
    "CanControl": "b",
    "CanPlay": "b",
    "CanPause": "b",
    "CanGoNext": "b",
    "CanGoPrevious": "b",
    "CanSeek": "b",
}


def property_xml(name, kind, writable=False):
    access = "readwrite" if writable else "read"
    return f'<property name="{name}" type="{kind}" access="{access}"/>'


def interface_xml(name, methods, properties):
    return (
        f'<interface name="{name}">'
        + "".join(f'<method name="{method}"/>' for method in methods)
        + properties
        + "</interface>"
    )


INTERFACES = Gio.DBusNodeInfo.new_for_xml(
    "<node>"
    + interface_xml(
        ROOT,
        ("Raise", "Quit"),
        "".join(
            property_xml(name, value.get_type_string())
            for name, value in ROOT_PROPERTIES.items()
        ),
    )
    + interface_xml(
        PLAYER,
        METHODS,
        "".join(
            property_xml(name, kind, name in ("Volume", "Rate"))
            for name, kind in PLAYER_TYPES.items()
        )
        + '<method name="Seek"><arg type="x" direction="in"/></method>'
        '<method name="SetPosition"><arg type="o" direction="in"/><arg type="x" direction="in"/></method>'
        '<signal name="Seeked"><arg type="x"/></signal>'
        '<method name="OpenUri"><arg type="s" direction="in"/></method>',
    )
    + "</node>"
)


class MediaControls:
    def __init__(self, window):
        self.window = window
        self.connection = None
        self.registrations = []
        self.last_properties = {}
        self.reset_stream()
        self.owner = Gio.bus_own_name(
            Gio.BusType.SESSION,
            ROOT + ".soundwave",
            Gio.BusNameOwnerFlags.NONE,
            self.register,
            None,
            None,
        )

    def register(self, connection, _name):
        self.connection = connection
        self.registrations = [
            connection.register_object(
                PATH, interface, self.call, self.get_property, self.set_property
            )
            for interface in INTERFACES.interfaces
        ]

    def close(self):
        Gio.bus_unown_name(self.owner)
        if self.connection:
            for registration in self.registrations:
                self.connection.unregister_object(registration)
            self.connection = None

    def reset_stream(self):
        self.seekable = False
        self.stream_length = 0
        self.seek_pending = False

    def stream_changed(self):
        player = self.window.player
        query = Gst.Query.new_seeking(Gst.Format.TIME)
        self.seekable = player.query(query) and query.parse_seeking().seekable
        known, duration = player.query_duration(Gst.Format.TIME)
        if known and duration > 0:
            self.stream_length = duration // 1000
        self.changed()

    def position(self):
        ok, position = self.window.player.query_position(Gst.Format.TIME)
        return max(0, position // 1000) if ok and self.window.track else 0

    def track_path(self):
        key = str(track_urn(self.window.track)).rsplit(":", 1)[-1]
        return PATH + "/Track/t" + key

    def track_length(self):
        return self.stream_length or int(self.window.track.get("duration") or 0) * 1000

    def seek(self, position):
        if self.window.player.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            position * 1000,
        ):
            self.seek_pending = True

    def seek_done(self):
        if self.seek_pending:
            self.seek_pending = False
            if self.connection:
                self.connection.emit_signal(
                    None,
                    PATH,
                    PLAYER,
                    "Seeked",
                    GLib.Variant("(x)", (self.position(),)),
                )

    def metadata(self):
        track = self.window.track
        if not track:
            return {}
        info = {
            "mpris:trackid": GLib.Variant("o", self.track_path()),
            "xesam:title": GLib.Variant("s", track["title"]),
            "xesam:artist": GLib.Variant(
                "as", [(track.get("user") or {}).get("username") or ""]
            ),
        }
        for field, property_name in (
            ("artwork_url", "mpris:artUrl"),
            ("permalink_url", "xesam:url"),
        ):
            if track.get(field):
                info[property_name] = GLib.Variant("s", track[field])
        if length := self.track_length():
            info["mpris:length"] = GLib.Variant("x", length)
        return info

    def properties(self):
        window = self.window
        status = (
            ("Playing" if window.playing else "Paused") if window.track else "Stopped"
        )
        return {
            "PlaybackStatus": GLib.Variant("s", status),
            "Metadata": GLib.Variant("a{sv}", self.metadata()),
            "Volume": GLib.Variant("d", window.player.get_property("volume")),
            "Rate": GLib.Variant("d", 1.0),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanPlay": GLib.Variant("b", window.can_play()),
            "CanPause": GLib.Variant("b", window.track is not None),
            "CanGoNext": GLib.Variant("b", window.can_play()),
            "CanGoPrevious": GLib.Variant("b", False),
            "CanSeek": GLib.Variant(
                "b", bool(window.track and self.seekable and self.track_length())
            ),
            "CanControl": GLib.Variant("b", True),
        }

    def get_property(self, _connection, _sender, _path, interface, name):
        if interface == ROOT:
            return ROOT_PROPERTIES[name]
        if name == "Position":
            return GLib.Variant("x", self.position())
        return self.properties()[name]

    def set_property(self, _connection, _sender, _path, interface, name, value):
        if interface == PLAYER and name == "Volume":
            self.window.player.set_property("volume", max(0.0, value.unpack()))
            self.changed()
            return True
        return interface == PLAYER and name == "Rate" and value.unpack() == 1.0

    def call(self, _connection, _sender, _path, interface, method, params, invocation):
        window = self.window
        if interface == ROOT:
            (window.present if method == "Raise" else window.close)()
        elif method == "Next" and window.can_play():
            window.request_next_track()
        elif method == "Play" and window.can_play() and not window.playing:
            window.on_play()
        elif method == "Pause" and window.playing:
            window.on_play()
        elif method == "PlayPause" and window.can_play():
            window.on_play()
        elif method == "Stop":
            window.request_stop()
        elif method in ("Seek", "SetPosition") and window.track and self.seekable:
            length = self.track_length()
            if method == "Seek" and length:
                target = max(0, self.position() + params.unpack()[0])
                if target > length:
                    window.request_next_track()
                else:
                    self.seek(target)
            elif method == "SetPosition" and length:
                path, target = params.unpack()
                if path == self.track_path() and 0 <= target <= length:
                    self.seek(target)
        elif method == "OpenUri":
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.NotSupported", "OpenUri is unavailable"
            )
            return
        invocation.return_value(None)

    def changed(self):
        if self.connection is None:
            return
        current = self.properties()
        changes = {
            name: value
            for name, value in current.items()
            if name not in self.last_properties
            or not value.equal(self.last_properties[name])
        }
        self.last_properties = current
        if changes:
            self.connection.emit_signal(
                None,
                PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (PLAYER, changes, [])),
            )

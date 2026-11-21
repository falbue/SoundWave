import re
from urllib.parse import urlsplit

import gi
import requests

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib


def fetch_image(url):
    if not url:
        return None
    address = urlsplit(url)
    if address.scheme != "https" or not (address.hostname or "").endswith(
        ".sndcdn.com"
    ):
        return None
    url = re.sub(r"-large(?=\.\w+(?:\?|$))", "-t500x500", url)
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(response.content)
        loader.close()
        return loader.get_pixbuf()
    except (requests.RequestException, GLib.Error):
        return None


def texture(pixbuf):
    return Gdk.Texture.new_for_pixbuf(pixbuf) if pixbuf else None

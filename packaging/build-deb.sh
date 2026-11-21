#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

version=${1#v}
dpkg --validate-version "$version"
output=${2:-dist}
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

install -d "$stage/DEBIAN" "$output"
cat packaging/control > "$stage/DEBIAN/control"
printf 'Version: %s\n' "$version" >> "$stage/DEBIAN/control"
install -Dm755 packaging/soundwave "$stage/usr/bin/soundwave"
install -Dm644 -t "$stage/usr/lib/soundwave" app/*.py
install -Dm644 -t "$stage/usr/share/applications" packaging/*.desktop

icon=packaging/ru.falbue.SoundWave.svg
if [ -f "$icon" ]; then
    install -Dm644 "$icon" "$stage/usr/share/icons/hicolor/scalable/apps/${icon##*/}"
fi

dpkg-deb --root-owner-group --build "$stage" "$output"

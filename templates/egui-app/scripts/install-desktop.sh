#!/bin/sh
# Builds {{crate}}, installs it with cargo, and adds a launcher entry and icon
# for the current user.
set -eu

usage() {
    cat <<USAGE
Usage: $0 [-h] [--uninstall]

Installs {{crate}} for the current user:
  binary    via cargo install (into \$CARGO_HOME/bin, usually ~/.cargo/bin)
  launcher  ~/.local/share/applications/{{crate}}.desktop
  icon      ~/.local/share/icons/hicolor/scalable/apps/{{crate}}.svg

  --uninstall  remove all three
  -h, --help   show this help
USAGE
}

root=$(cd "$(dirname "$0")/.." && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
desktop=$data/applications/{{crate}}.desktop
icon=$data/icons/hicolor/scalable/apps/{{crate}}.svg

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --uninstall)
        cargo uninstall {{crate}} || true
        rm -f "$desktop" "$icon"
        echo "Removed."
        exit 0 ;;
    "") ;;
    *) usage >&2; exit 2 ;;
esac

# A build directory of its own, so an install never shares artifacts with
# builds running elsewhere against the same checkout.
# --locked once Cargo.lock exists; a project never built yet has none.
locked=
[ -f "$root/Cargo.lock" ] && locked=--locked
cargo install $locked --path "$root" --target-dir "$root/target/install"
bin=${CARGO_HOME:-$HOME/.cargo}/bin/{{crate}}
install -Dm644 "$root/assets/icon.svg" "$icon"
mkdir -p "$(dirname "$desktop")"
# Launchers often run without ~/.cargo/bin on PATH, so use the full path,
# quoted as the desktop entry spec asks: backslash before " ` $ \, then every
# backslash doubled for the string-value escaping, and % doubled. awk reads it
# from the environment, which leaves the value untouched.
quoted=$(printf '%s' "$bin" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g' -e 's/%/%%/g')
EXEC="Exec=\"$quoted\"" awk '/^Exec=/ { print ENVIRON["EXEC"]; next } { print }' \
    "$root/assets/{{crate}}.desktop" > "$desktop"
echo "Installed $bin and a launcher entry."

# {{title}}

A Linux desktop app, built with egui for GNOME on Wayland.

## Building

Needs Rust 1.85 or newer.

```sh
cargo run --release
```

It draws with OpenGL (glow) and runs natively on Wayland, or on X11. Each
launch prints a line like `startup: renderer ready after 70 ms, first frame
after 74 ms` to stderr.

To install it with a launcher entry and icon for your user:

```sh
scripts/install-desktop.sh            # -h for options, --uninstall to remove
```

## Development

```sh
git config core.hooksPath .githooks   # rustfmt check before each commit
cargo clippy --all-targets -- -D warnings
cargo test
```

| Path | What |
| --- | --- |
| `src/main.rs` | Entry point: window options, icon, renderer |
| `src/app.rs` | The egui window |
| `tests/window.rs` | Drives the window with egui_kittest |
| `assets/` | Icon (SVG for the launcher, PNG for the window) and `.desktop` entry |
| `scripts/install-desktop.sh` | Per-user install with launcher entry |

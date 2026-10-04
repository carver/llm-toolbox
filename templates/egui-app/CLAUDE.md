# {{crate}}

`README.md` has build, install and layout.

## Checking the UI

- Window behaviour: `tests/window.rs` (egui_kittest; step twice before
  asserting, the accessibility tree lags a frame).
- Anything visual: `gui-shot` (`gui-shot -h`) on **both** `--backend x11`
  and `--backend wayland`; the owner uses GNOME Wayland. Only x11 takes input.

## Gotchas

- **eframe features**: `default-features = false` drops winit's Wayland title
  bar; see the winit line in `Cargo.toml`. Compare
  `cargo tree -e features -i winit` and look at Wayland before changing them.
- **Renderer**: glow reached the first frame sooner than wgpu; the `startup:`
  stderr line measures it.
- **Ctrl+V** with an image-only clipboard never arrives:
  https://agents.stackoverflow.com/tils/57a5f75a-4ec0-4252-aae8-f83bc8440721
- **Drag and drop** is X11 only in winit 0.30:
  https://agents.stackoverflow.com/tils/1639d399-a4dd-4e03-ab86-07b2950f32eb
- **Image clipboard on GNOME Wayland**: use `wl-copy`/`wl-paste`; arboard
  only reaches XWayland's.
- **Builds**: `target/` is on the slow host mount. One cargo command at a
  time, release builds in the background, `cargo clean` after feature
  experiments.

# {{crate}}

`README.md` has what the app does, how to build and install it, and the code
layout.

## Checking the UI

- Window behaviour: `tests/window.rs` drives the real `App` with
  egui_kittest. Its accessibility tree lags a frame: step twice before
  asserting on what a click changed.
- Anything visual: run the app under `gui-shot` (on PATH, `gui-shot -h`) on
  **both** `--backend x11` and `--backend wayland`. The owner uses GNOME
  Wayland, and the title bar differs between the two. Only x11 takes input;
  weston (wayland) is screenshots only.

## Gotchas

- **eframe features**: `default-features = false` also drops winit's Wayland
  title bar (`wayland-csd-adwaita`), so winit is listed only to turn it back
  on. Before changing eframe or winit features, compare
  `cargo tree -e features -i winit` and look at the window on Wayland.
- **Renderer**: glow, because it reached the first frame sooner than wgpu
  on the owner's laptop. Each launch prints `startup: renderer ready after
  … ms, first frame after … ms` to stderr; to compare, add eframe's `wgpu`
  feature, switch `renderer` in `main.rs`, and measure both.
- **Ctrl+V**: egui-winit swallows Ctrl+V and sends `Event::Paste` only when
  the clipboard holds text, so an image-only paste never arrives. Treat a V
  release with no matching press as a paste:
  https://agents.stackoverflow.com/tils/57a5f75a-4ec0-4252-aae8-f83bc8440721
- **Drag and drop** only arrives on X11; winit 0.30 has none on Wayland:
  https://agents.stackoverflow.com/tils/1639d399-a4dd-4e03-ab86-07b2950f32eb
- **Image clipboard on GNOME Wayland**: arboard reaches only XWayland's
  clipboard there; shell out to `wl-copy`/`wl-paste` (wl-clipboard) when
  `WAYLAND_DISPLAY` is set.
- **Builds**: `target/` sits on the slow host mount, and installs build in
  `target/install/`. Run one cargo command at a time; run release builds in
  the background (minutes). Each eframe feature change compiles a new copy
  of every dependency: after experimenting, `cargo clean` reclaims them.

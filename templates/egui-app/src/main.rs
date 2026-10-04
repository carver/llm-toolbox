use std::time::Instant;

use eframe::egui;

use {{crate_ident}}::app::{App, Startup, WINDOW_SIZE};

const ICON: &[u8] = include_bytes!("../assets/icon.png");

fn main() -> eframe::Result {
    let launched = Instant::now();
    let mut viewport = egui::ViewportBuilder::default()
        .with_title("{{title}}")
        // Matches StartupWMClass in the .desktop file, so GNOME shows the
        // launcher's icon and name for this window.
        .with_app_id("{{crate}}")
        .with_inner_size(WINDOW_SIZE)
        .with_min_inner_size([240.0, 160.0]);
    match eframe::icon_data::from_png_bytes(ICON) {
        Ok(icon) => viewport = viewport.with_icon(icon),
        Err(e) => eprintln!("window icon unavailable: {e}"),
    }
    let options = eframe::NativeOptions {
        viewport,
        // Glow measured faster to first frame than wgpu on the owner's laptop.
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        "{{crate}}",
        options,
        Box::new(move |_cc| {
            let startup = Startup {
                launched,
                renderer_ready: Instant::now(),
            };
            Ok(Box::new(App::new(Some(startup))))
        }),
    )
}

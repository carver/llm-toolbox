//! Drives the real window with synthetic input.

use egui_kittest::Harness;
use egui_kittest::kittest::Queryable;

use {{crate_ident}}::app::{App, WINDOW_SIZE};

fn window() -> Harness<'static, App> {
    let mut harness = Harness::builder()
        .with_size(WINDOW_SIZE)
        .build_eframe(|_| App::new(None));
    harness.step();
    harness
}

#[test]
fn button_counts_clicks() {
    let mut h = window();
    h.get_by_label("Clicked 0 times");
    h.get_by_label("Click me").click();
    // The accessibility tree lags a frame behind.
    h.step();
    h.step();
    h.get_by_label("Clicked 1 time");
}

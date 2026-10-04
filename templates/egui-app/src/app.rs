//! The window.

use std::time::Instant;

use eframe::egui::{self, Ui, Vec2};

/// The window's size at launch, and the test harness's.
pub const WINDOW_SIZE: Vec2 = egui::vec2(480.0, 320.0);

/// When the process started and when the renderer came up, for the startup
/// timing line logged on the first frame.
pub struct Startup {
    pub launched: Instant,
    pub renderer_ready: Instant,
}

pub struct App {
    startup: Option<Startup>,
    clicks: u32,
}

impl App {
    /// `startup` is `None` in tests, which have no launch to time.
    pub fn new(startup: Option<Startup>) -> Self {
        App { startup, clicks: 0 }
    }

    fn report_startup(&mut self) {
        let Some(Startup {
            launched,
            renderer_ready,
        }) = self.startup.take()
        else {
            return;
        };
        let ms = |t: Instant| t.duration_since(launched).as_millis();
        eprintln!(
            "startup: renderer ready after {} ms, first frame after {} ms",
            ms(renderer_ready),
            ms(Instant::now())
        );
    }
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut Ui, _frame: &mut eframe::Frame) {
        egui::CentralPanel::default().show(ui, |ui| {
            ui.heading("{{title}}");
            if ui.button("Click me").clicked() {
                self.clicks += 1;
            }
            let times = if self.clicks == 1 { "time" } else { "times" };
            ui.label(format!("Clicked {} {times}", self.clicks));
        });
        self.report_startup();
    }
}

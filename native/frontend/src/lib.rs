//! What the platmon command and the tray share (native/README.md): the HTTP client (net.rs) and the
//! command's screen code (screen.rs), whose attention(), levels and metrics() the tray reads too. The
//! terminal (bin/platmon/term.rs) stays the command's.
pub mod net;
pub mod screen;

// ../../shared: the JSON parser and render(), the plain screen the server's /text answers with
pub use platmon_shared::{json, render};

/// The application version: the repository's VERSION file, read at build time, as platmon-server's.
pub fn version() -> &'static str {
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../VERSION")).trim()
}

#[cfg(test)]
mod tests {
    #[test]
    fn version_is_the_version_file() {
        assert!(super::version().split('.').count() == 3);
    }
}

//! What the service needs from the OS outside the collectors: the elapsed clocks, random bytes for the
//! instance id, the listening socket and the exit on a stop request. One module per OS, the same functions.
#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
pub use linux::*;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::*;

//! The collectors, one module per OS: what the Sampler and the slow groups call to read the host.
#[cfg(target_os = "linux")]
pub mod linux;

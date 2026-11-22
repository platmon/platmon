//! What platmon-server and the frontends (the `platmon` command) share, and nothing more: each side can
//! use this crate but not the other's modules, so the frontends could leave the repository with it
//! (native/README.md, "Workspace").
pub mod json;
pub mod render;

// Windows with the MSVC linker: embed src/platform/windows.manifest (the segment heap) in platmon-server.exe.
// Nothing for any other target.
fn main() {
    println!("cargo:rerun-if-changed=src/platform/windows.manifest");
    let var = |k| std::env::var(k).unwrap_or_default();
    if var("CARGO_CFG_TARGET_OS") == "windows" && var("CARGO_CFG_TARGET_ENV") == "msvc" {
        let manifest = std::path::Path::new(&var("CARGO_MANIFEST_DIR")).join("src/platform/windows.manifest");
        println!("cargo:rustc-link-arg-bins=/MANIFEST:EMBED");
        println!("cargo:rustc-link-arg-bins=/MANIFESTINPUT:{}", manifest.display());
    }
}

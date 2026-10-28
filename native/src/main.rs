//! The platmon server the Debian package runs, a port of the Python service (native/README.md). Its collector
//! covers the core groups: CPU, clocks, thermal zones, hwmon, memory, disk,
//! identity, the Jetson extension, DRM GPU and accel NPU, collectors and sensor source ids, and the
//! passive groups Network, Disk I/O and Pressure as the service enables them by default, and the nvidia-smi
//! fallback.
//!
//!   platmon-native collect            one recorded collection 1 s after a first one, as JSON
//!                                     {"stats", "sensors" (with spans), "required", "passive", "clock"}
//!   platmon-native script             fixture tests: for each stdin line "BASE_NS WALL_US", one collection
//!                                     on those clocks (see metadata::set_clocks), one JSON line each
//!                                     ({"error": ...} for a failed collection)
//!   platmon-native loop SECONDS [WARMUP]  collect every 1 s (after WARMUP s, default 3), print own CPU
//!                                     (user/sys %), RSS, collection times and group states as JSON
//!   platmon-native serve [CONFIG] [--frontends LIST] [--web DIR] [--app-version V]
//!                                     the service as platmon.py runs it: platmon.ini, the Sampler, slow
//!                                     groups, history, replay and the HTTP API (src/runtime.rs, src/http.rs)
//!   platmon-native service [CONFIG] [serve's options]
//!                                     Windows only: serve as a service the service control manager runs
//!                                     (platform/windows.rs, run_service)
//!   platmon-native runtime-script     tools/runtime_fixture.py: Sampler and slow-group operations with
//!                                     given times and collections, one JSON line per stdin line
// Elsewhere the shared helpers the Linux collectors call have no caller yet.
#![cfg_attr(not(target_os = "linux"), allow(dead_code))]
#[cfg(feature = "cache-alloc")]
mod alloc;
mod collect;
mod config;
mod harness;
mod history;
mod http;
mod json;
mod metadata;
mod platform;
mod pystr;
mod render;
mod replay;
mod runtime;
mod sha256;
mod slow;

use collect::{observe, Collected, Collector};
use json::Value;

#[cfg(feature = "cache-alloc")]
#[global_allocator]
static ALLOC: alloc::Cached = alloc::Cached::new();

#[cfg(all(feature = "cache-alloc", feature = "mimalloc"))]
compile_error!("cache-alloc and mimalloc are two allocators: choose one");

#[cfg(feature = "mimalloc")]
#[global_allocator]
static ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// The global allocator this binary was built with, for the results.
pub const ALLOCATOR: &str = if cfg!(feature = "cache-alloc") { "cache-alloc" } else if cfg!(feature = "mimalloc") { "mimalloc" } else { "system" };

/// `build-info`: the allocator, also in the binary's own bytes, so that build.sh can check a binary of
/// either target without running it.
pub const ALLOCATOR_MARK: &str = if cfg!(feature = "cache-alloc") { "platmon-native-allocator=cache-alloc;" }
    else if cfg!(feature = "mimalloc") { "platmon-native-allocator=mimalloc;" } else { "platmon-native-allocator=system;" };

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("serve") => return serve(&args[2..]),
        #[cfg(windows)]
        Some("service") => {
            let rest = args[2..].to_vec();
            return platform::run_service(move || serve(&rest));
        }
        Some("runtime-script") => return harness::runtime_script(),
        Some("config-check") => return config_check(&args[2..]),
        #[cfg(target_os = "linux")]
        Some("observe-script") => return harness::observe_script(&args[2..]),
        Some("replay-cost") => return harness::replay_cost(&args[2..]),
        Some("build-info") => return println!("{}", ALLOCATOR_MARK),
        _ => {}
    }
    harness::collect_command(&args);
}

/// config-check [CONFIG [PLATFORM [FRONTENDS]]]: the configuration as serve would use it, {"error": ...} if
/// refused (tools/config_fixture.py compares it with platmon.load_config); empty CONFIG or PLATFORM: none,
/// FRONTENDS: the --frontends value.
fn config_check(args: &[String]) {
    let platform = args.get(1).map(String::as_str).filter(|p| !p.is_empty());
    let frontends = config::frontends(args.get(2).map(String::as_str));
    let out = match config::load(args.first().map(String::as_str).filter(|p| !p.is_empty()), frontends.as_deref()) {
        Ok(cfg) => {
            let mut v = cfg.value(platform);
            if platform.is_none() {
                v.set("levels", cfg.value(None).get("levels").cloned().unwrap_or_default());
            }
            v
        }
        Err(e) => { let mut v = Value::obj(); v.set("error", e.into()); v }
    };
    println!("{}", out.to_string(false));
}

/// The slow groups that run, in their order: each that is enabled, the probe only with targets.
fn slow_groups(cfg: &config::Config) -> Vec<&'static str> {
    observe::GROUPS.iter().copied().filter(|g| cfg.flag(g, "enabled") && (*g != "probe" || !cfg.targets.is_empty())).collect()
}

/// platmon.py: serve [CONFIG] [--frontends LIST] [--web DIR] [--app-version V]. The configuration as
/// platmon.py reads it; the slow groups are on as configured, with their readers (src/collect/linux/observe.rs).
fn serve(args: &[String]) {
    let (mut path, mut web_dir, mut version, mut frontends) = (None, None, "0.0.0".to_string(), None);
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--web" | "--app-version" | "--frontends" => {
                let value = it.next().unwrap_or_else(|| { eprintln!("serve: {} needs a value", arg); std::process::exit(2) }).clone();
                match arg.as_str() {
                    "--web" => web_dir = Some(value),
                    "--app-version" => version = value,
                    _ => frontends = Some(value),
                }
            }
            a if a.starts_with("--frontends=") => frontends = Some(a["--frontends=".len()..].to_string()),
            _ if path.is_none() && !arg.starts_with("--") => path = Some(arg.clone()),
            _ => { eprintln!("serve: unknown argument {}", arg); std::process::exit(2) }
        }
    }
    let names = config::frontends(frontends.as_deref());
    let cfg = config::load(path.as_deref(), names.as_deref()).unwrap_or_else(|e| { eprintln!("platmon-native serve: error: {}", e); std::process::exit(2) });
    platform::exit_on_terminate();
    let interval = cfg.float("core", "interval");
    let mut c = Collector::new((3.0 * interval).max(5.0), [cfg.flag("network", "enabled"), cfg.flag("disk_io", "enabled"),
                                                            cfg.flag("pressure", "enabled")]);
    println!("platmon-native: platform {}, version {}", c.platform, version);
    let source = metadata::clock_source();
    let mut sampler = runtime::Sampler::new(interval, None, runtime::uuid4(), source);
    let slow_names = slow_groups(&cfg);
    let replay = cfg.flag("replay", "enabled").then(|| std::sync::Arc::new(replay::Replay::new(
        sampler.instance_id.clone(), source, &slow_names, cfg.float("replay", "retention"), cfg.int("replay", "max_bytes") as usize)));
    // recent numbers of the published snapshots, in memory only (docs/reference/history.md)
    let history = cfg.flag("history", "enabled").then(|| std::sync::Arc::new(history::History::new(cfg.float("history", "retention"), interval)));
    sampler.replay = replay.clone();
    sampler.history = history.clone();
    let sampler = std::sync::Arc::new(sampler);
    let groups: Vec<std::sync::Arc<slow::Slow>> = slow_names.iter().map(|name| {
        let mut g = slow::Slow::new(name, cfg.float(name, "interval"));
        g.replay = replay.clone();
        g.history = history.clone();
        std::sync::Arc::new(g)
    }).collect();
    let observations = std::sync::Arc::new(slow::Observations { groups: groups.clone(), instance_id: sampler.instance_id.clone(),
                                                                clock_source: source });
    let levels = config::levels_for(&cfg, c.platform);
    let (bind, port) = (cfg.text("http", "bind"), cfg.int("http", "port") as u16);
    let listener = http::bind(&bind, port).unwrap_or_else(|e| { eprintln!("platmon-native: cannot listen on {}:{}: {}", bind, port, e); std::process::exit(1) });
    let s = sampler.clone();
    std::thread::Builder::new().name("sampler".into()).spawn(move || {
        static STOP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
        s.run(|| c.collect().map(Collected::to_value), &STOP)
    }).expect("cannot start the sampler");
    for g in &groups {
        g.start(observe::reader(&g.name, &cfg));
    }
    println!("platmon http on {}:{}", bind, port);
    let web = cfg.flag("http", "web").then_some(web_dir).flatten();
    std::sync::Arc::new(http::Server::new(sampler, observations, web, version, levels)).serve(listener);
}

// tests/collector/test_storage.py test_slow_groups_and_history_follow_configuration, ported
// (docs/development/python-test-port.md).
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slow_groups_and_history_follow_configuration() {
        const BAT: (&str, f64) = ("battery", 60.0); // on by default, like storage and wifi
        let cases: [(Option<&str>, Vec<(&str, f64)>, bool); 10] = [
            (None, vec![("storage", 30.0), ("wifi", 5.0), BAT], true),
            (Some("[storage]\ninterval = 60\n"), vec![("storage", 60.0), ("wifi", 5.0), BAT], true),
            (Some("[storage]\nenabled = no\n"), vec![("wifi", 5.0), BAT], true),
            (Some("[wifi]\nenabled = no\n"), vec![("storage", 30.0), BAT], true),
            (Some("[battery]\nenabled = no\n"), vec![("storage", 30.0), ("wifi", 5.0)], true),
            (Some("[battery]\ninterval = 120\n"), vec![("storage", 30.0), ("wifi", 5.0), ("battery", 120.0)], true),
            (Some("[storage]\nenabled = no\n[wifi]\nenabled = no\n[battery]\nenabled = no\n[history]\nenabled = no\n"), vec![], false),
            (Some("[probe]\ntargets = 127.0.0.1:9797\n"), vec![("storage", 30.0), ("wifi", 5.0), BAT, ("probe", 10.0)], true),
            (Some("[probe]\nenabled = no\ntargets = 127.0.0.1:9797\n"), vec![("storage", 30.0), ("wifi", 5.0), BAT], true),
            (Some("[probe]\ntargets = , ,\n"), vec![("storage", 30.0), ("wifi", 5.0), BAT], true)];
        let path = std::env::temp_dir().join(format!("platmon-native-slow-{}.ini", std::process::id()));
        for (text, groups, history) in cases {
            if let Some(t) = text {
                std::fs::write(&path, t).unwrap();
            }
            let cfg = config::load(text.map(|_| path.to_str().unwrap()), None).ok().unwrap();
            let got: Vec<(&str, f64)> = slow_groups(&cfg).into_iter().map(|g| (g, cfg.float(g, "interval"))).collect();
            assert_eq!(got, groups, "{:?}", text);
            assert_eq!(cfg.flag("history", "enabled"), history, "{:?}", text);
        }
        let _ = std::fs::remove_file(&path);
    }
}

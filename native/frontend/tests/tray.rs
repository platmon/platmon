//! platmon-tray on a private session bus (dbus-daemon) against a stand-in server: without a
//! StatusNotifierWatcher it exits 3 (on an abstract socket address); with a stand-in watcher it registers,
//! answers GetAll, Get and Introspect, runs xdg-open on Activate, and signals NewIcon and NewToolTip when the
//! service stops having current data. Skipped where dbus-daemon is not installed, unless
//! PLATMON_REQUIRE_DBUS=1 (CI's native job installs it for this test).
#![cfg(target_os = "linux")]

#[path = "../src/bin/platmon-tray/dbus.rs"]
#[allow(dead_code)]
mod dbus;

use dbus::{Arg, Bus, Message};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const ITEM: &str = "org.kde.StatusNotifierItem";
const STATS: &str = r#"{"system": {"hostname": "testhost"}, "cpu": [{"usage": 50.0}], "memory": {"total": 100, "used": 41},
    "disk": {"total": 100, "used": 10}, "temperature": {"cpu": 52.0}}"#;

/// The stand-in server: /api/status ready (or stale once `stale` is set), /api/stats, /api/observations 404.
fn serve(stale: Arc<AtomicBool>) -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    std::thread::spawn(move || for c in l.incoming() {
        let Ok(mut c) = c else { continue };
        let mut buf = [0u8; 2048];
        let n = c.read(&mut buf).unwrap_or(0);
        let path = String::from_utf8_lossy(&buf[..n]).split(' ').nth(1).unwrap_or("").to_string();
        let state = if stale.load(Ordering::SeqCst) { "stale" } else { "ready" };
        let (status, body) = match path.as_str() {
            "/api/status" => ("200 OK", format!(r#"{{"state": "{}", "levels": {{"cpu": [80, 90]}}}}"#, state)),
            "/api/stats" => ("200 OK", STATS.to_string()),
            _ => ("404 Not Found", "{}".to_string()),
        };
        let _ = c.write_all(format!("HTTP/1.0 {}\r\nContent-Length: {}\r\n\r\n{}", status, body.len(), body).as_bytes());
    });
    port
}

struct Kill(Child);

impl Drop for Kill {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn daemon(address: &str, socket: Option<&Path>) -> Kill {
    let child = Command::new("dbus-daemon").args(["--session", "--nofork", "--nopidfile", &format!("--address={}", address)])
        .stdout(Stdio::null()).spawn().unwrap();
    let k = Kill(child);
    let until = Instant::now() + Duration::from_secs(5);
    while Instant::now() < until {
        // ready once a client gets in (the abstract socket has no file to wait for)
        if socket.map_or(true, |s| s.exists()) && dbus_connects(address) { return k }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("dbus-daemon did not start on {}", address);
}

fn dbus_connects(address: &str) -> bool {
    std::env::set_var("DBUS_SESSION_BUS_ADDRESS", address);
    dbus::connect().is_ok()
}

fn tray(address: &str, port: u16, bin: &Path) -> Child {
    let path = format!("{}:{}", bin.display(), std::env::var("PATH").unwrap_or_default());
    Command::new(env!("CARGO_BIN_EXE_platmon-tray")).arg(format!("127.0.0.1:{}", port))
        .env("DBUS_SESSION_BUS_ADDRESS", address).env("PATH", path).stderr(Stdio::piped()).spawn().unwrap()
}

/// The next message that pred accepts, within the time left.
fn next(rd: &mut UnixStream, until: Instant, what: &str, pred: impl Fn(&Message) -> bool) -> Message {
    loop {
        let left = until.saturating_duration_since(Instant::now());
        assert!(!left.is_zero(), "timed out waiting for {}", what);
        rd.set_read_timeout(Some(left)).unwrap();
        let m = dbus::read(rd).unwrap_or_else(|e| panic!("waiting for {}: {}", what, e));
        if pred(&m) { return m }
    }
}

fn call(bus: &Bus, rd: &mut UnixStream, dest: &str, iface: &str, member: &str, body: &[Arg]) -> Message {
    let serial = bus.call(dest, "/StatusNotifierItem", iface, member, body).unwrap();
    let m = next(rd, Instant::now() + Duration::from_secs(5), member, |m| m.reply_serial == Some(serial));
    assert_eq!(m.kind, dbus::METHOD_RETURN, "{} answered {:?}", member, m.error_name);
    m
}

fn get(bus: &Bus, rd: &mut UnixStream, dest: &str, property: &str) -> String {
    let m = call(bus, rd, dest, "org.freedesktop.DBus.Properties", "Get", &[Arg::Str(ITEM.into()), Arg::Str(property.into())]);
    assert_eq!(m.signature, "v");
    String::from_utf8_lossy(&m.body).into_owned()
}

#[test]
fn tray_on_a_session_bus() {
    if Command::new("dbus-daemon").arg("--version").stdout(Stdio::null()).status().is_err() {
        assert!(std::env::var("PLATMON_REQUIRE_DBUS").as_deref() != Ok("1"), "PLATMON_REQUIRE_DBUS=1 but dbus-daemon is not installed");
        eprintln!("skipped: dbus-daemon is not installed");
        return;
    }
    let dir = std::env::temp_dir().join(format!("platmon-tray-test-{}", std::process::id()));
    let bin = dir.join("bin");
    std::fs::create_dir_all(&bin).unwrap();
    let opened: PathBuf = dir.join("opened");
    std::fs::write(bin.join("xdg-open"), format!("#!/bin/sh\necho \"$1\" > {}\n", opened.display())).unwrap();
    std::fs::set_permissions(bin.join("xdg-open"), std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let stale = Arc::new(AtomicBool::new(false));
    let port = serve(stale.clone());

    // no watcher: exit 3, on an abstract address
    let abstract_address = format!("unix:abstract={}/abstract", dir.display());
    let _plain = daemon(&abstract_address, None);
    let out = tray(&abstract_address, port, &bin).wait_with_output().unwrap();
    assert_eq!(out.status.code(), Some(3), "{}", String::from_utf8_lossy(&out.stderr));
    assert!(String::from_utf8_lossy(&out.stderr).contains("no StatusNotifierWatcher"));

    // a stand-in watcher on a path address
    let socket = dir.join("bus");
    let address = format!("unix:path={}", socket.display());
    let _bus = daemon(&address, Some(&socket));
    std::env::set_var("DBUS_SESSION_BUS_ADDRESS", &address);
    let (bus, mut rd, _) = dbus::connect().unwrap();
    assert_eq!(bus.bus_call(&mut rd, "RequestName", &[Arg::Str("org.kde.StatusNotifierWatcher".into()), Arg::U32(4)]).unwrap().unwrap().first_u32(), Some(1));
    bus.bus_call(&mut rd, "AddMatch", &[Arg::Str(format!("type='signal',interface='{}'", ITEM))]).unwrap().unwrap();
    let child = Kill(tray(&address, port, &bin));

    let soon = || Instant::now() + Duration::from_secs(10);
    let reg = next(&mut rd, soon(), "RegisterStatusNotifierItem", |m| m.member.as_deref() == Some("RegisterStatusNotifierItem"));
    let service = reg.strs()[0].clone();
    assert_eq!(service, format!("{}-{}-1", ITEM, child.0.id()));
    bus.reply(&reg, &[]).unwrap();

    // the first answer arrives within a moment of the start
    let until = soon();
    while !get(&bus, &mut rd, &service, "ToolTip").contains("CPU 50% · RAM 41% · 52°C") {
        assert!(Instant::now() < until, "the tooltip never showed the readings");
        std::thread::sleep(Duration::from_millis(100));
    }
    let all = call(&bus, &mut rd, &service, "org.freedesktop.DBus.Properties", "GetAll", &[Arg::Str(ITEM.into())]);
    assert_eq!(all.signature, "a{sv}");
    let text = String::from_utf8_lossy(&all.body);
    for want in ["Category", "SystemServices", "Id", "platmon", "Status", "Active", "IconName", "dialog-information", "ToolTip",
                 "(sa(iiay)ss)", "testhost — ok", "ItemIsMenu"] {
        assert!(text.contains(want), "GetAll lacks {}: {:?}", want, text);
    }
    assert!(!text.contains("Menu\0\x01o"), "no Menu property");
    let xml = call(&bus, &mut rd, &service, "org.freedesktop.DBus.Introspectable", "Introspect", &[]).strs()[0].clone();
    assert!(xml.contains(r#"<interface name="org.kde.StatusNotifierItem">"#));
    call(&bus, &mut rd, &service, "org.freedesktop.DBus.Peer", "Ping", &[]);

    call(&bus, &mut rd, &service, ITEM, "Activate", &[Arg::I32(0), Arg::I32(0)]);
    let until = soon();
    while std::fs::read_to_string(&opened).unwrap_or_default().trim() != format!("http://127.0.0.1:{}/", port) {
        assert!(Instant::now() < until, "Activate did not run xdg-open with the page");
        std::thread::sleep(Duration::from_millis(50));
    }

    // no current data: not reachable, announced with NewIcon and NewToolTip (within one 10 s poll)
    stale.store(true, Ordering::SeqCst);
    let until = Instant::now() + Duration::from_secs(15);
    next(&mut rd, until, "NewIcon", |m| m.kind == dbus::SIGNAL && m.member.as_deref() == Some("NewIcon"));
    next(&mut rd, until, "NewToolTip", |m| m.kind == dbus::SIGNAL && m.member.as_deref() == Some("NewToolTip"));
    assert!(get(&bus, &mut rd, &service, "IconName").contains("network-offline"));
    let tip = get(&bus, &mut rd, &service, "ToolTip");
    assert!(tip.contains("not reachable") && tip.contains("is stale: no current data"), "{:?}", tip);
    drop(child);
    let _ = std::fs::remove_dir_all(&dir);
}

//! The tray icon on Linux: a StatusNotifierItem (freedesktop's draft, as KDE, XFCE, Cinnamon and GNOME's
//! AppIndicator extension read it) on the session bus, through dbus.rs. The object /StatusNotifierItem with
//! its properties (theme icon names, no pixmaps; no Menu), Activate opening the web page, the Properties,
//! Introspectable and Peer interfaces, and the New* signals when what it shows changes. Registered with the
//! StatusNotifierWatcher, again whenever the watcher's name gets a new owner (a restarted panel).
use crate::dbus::{self, Arg, Bus, Message};
use crate::{Level, Shown};
use std::sync::{Arc, Mutex};

const WATCHER: &str = "org.kde.StatusNotifierWatcher";
const ITEM: &str = "org.kde.StatusNotifierItem";
const PATH: &str = "/StatusNotifierItem";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";
const INTROSPECTABLE: &str = "org.freedesktop.DBus.Introspectable";
const PEER: &str = "org.freedesktop.DBus.Peer";

const XML: &str = r#"<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get"><arg name="interface" type="s" direction="in"/><arg name="property" type="s" direction="in"/><arg name="value" type="v" direction="out"/></method>
    <method name="GetAll"><arg name="interface" type="s" direction="in"/><arg name="properties" type="a{sv}" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg name="xml" type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
  </interface>
</node>
"#;

const ROOT_XML: &str = r#"<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node><node name="StatusNotifierItem"/></node>
"#;

/// The freedesktop icon theme's names (Icon Naming Specification, status icons), present in every theme.
pub fn icon(level: Level) -> &'static str {
    match level {
        Level::Ok => "dialog-information",
        Level::Warning => "dialog-warning",
        Level::Critical => "dialog-error",
        Level::Down => "network-offline",
    }
}

fn status(level: Level) -> &'static str {
    if matches!(level, Level::Warning | Level::Critical) { "NeedsAttention" } else { "Active" }
}

/// The item's properties for what it shows now.
pub fn properties(s: &Shown) -> Vec<(String, Arg)> {
    let str = |v: &str| Arg::Str(v.into());
    vec![("Category".into(), str("SystemServices")),
         ("Id".into(), str("platmon")),
         ("Title".into(), str("platmon")),
         ("Status".into(), str(status(s.level))),
         ("IconName".into(), str(icon(s.level))),
         ("AttentionIconName".into(), str(icon(s.level))),
         ("ToolTip".into(), Arg::ToolTip(icon(s.level).into(), s.title.clone(), s.text.clone())),
         ("ItemIsMenu".into(), Arg::Bool(false))]
}

/// The signals for a change from old to new: NewIcon and NewAttentionIcon, NewStatus, NewToolTip.
pub fn signals(old: &Shown, new: &Shown) -> Vec<(&'static str, Vec<Arg>)> {
    let mut out = Vec::new();
    if icon(old.level) != icon(new.level) {
        out.push(("NewIcon", vec![]));
        out.push(("NewAttentionIcon", vec![]));
    }
    if status(old.level) != status(new.level) {
        out.push(("NewStatus", vec![Arg::Str(status(new.level).into())]));
    }
    if old != new {
        out.push(("NewToolTip", vec![]));
    }
    out
}

/// One method call to this connection; replied to unless the caller asked for no reply.
fn handle(bus: &Bus, m: &Message, shown: &Mutex<Shown>, url: &str) {
    let member = m.member.as_deref().unwrap_or("");
    let path = m.path.as_deref().unwrap_or("");
    let iface = m.interface.as_deref().unwrap_or(match member { // the interface is optional in a call
        "Ping" => PEER,
        "Introspect" => INTROSPECTABLE,
        "Get" | "GetAll" | "Set" => PROPERTIES,
        _ => ITEM,
    });
    let args = m.strs();
    let answer: Result<Vec<Arg>, (&str, String)> = match (path, iface, member) {
        (_, PEER, "Ping") => Ok(vec![]),
        (PATH, INTROSPECTABLE, "Introspect") => Ok(vec![Arg::Str(XML.into())]),
        ("/", INTROSPECTABLE, "Introspect") => Ok(vec![Arg::Str(ROOT_XML.into())]),
        (PATH, PROPERTIES, "Get") if args.len() == 2 => {
            let props = if args[0] == ITEM { properties(&shown.lock().unwrap_or_else(|e| e.into_inner())) } else { vec![] };
            match props.into_iter().find(|(k, _)| *k == args[1]) {
                Some((_, v)) => Ok(vec![Arg::Var(Box::new(v))]),
                None => Err(("org.freedesktop.DBus.Error.UnknownProperty", format!("no property {} on {}", args[1], args[0]))),
            }
        }
        (PATH, PROPERTIES, "GetAll") if args.len() == 1 => {
            let props = if args[0] == ITEM { properties(&shown.lock().unwrap_or_else(|e| e.into_inner())) } else { vec![] };
            Ok(vec![Arg::Dict(props)])
        }
        (PATH, PROPERTIES, "Set") => Err(("org.freedesktop.DBus.Error.PropertyReadOnly", "the properties are read-only".into())),
        (PATH, ITEM, "Activate") => {
            crate::open(url);
            Ok(vec![])
        }
        (PATH, ITEM, "SecondaryActivate" | "ContextMenu" | "Scroll") => Ok(vec![]),
        (PATH, _, _) => Err(("org.freedesktop.DBus.Error.UnknownMethod", format!("no method {}.{}", iface, member))),
        _ => Err(("org.freedesktop.DBus.Error.UnknownObject", format!("no object {}", path))),
    };
    if m.flags & dbus::NO_REPLY_EXPECTED != 0 {
        return;
    }
    let _ = match answer {
        Ok(body) => bus.reply(m, &body),
        Err((name, text)) => bus.error(m, name, &text),
    };
}

fn fail(text: &str) -> ! {
    eprintln!("platmon-tray: {}", text);
    std::process::exit(1)
}

/// Shows the icon until the session bus goes away; exits 3 when there is no StatusNotifierWatcher to show it.
pub fn run(addr: &str) -> ! {
    let (bus, mut rd, _unique) = dbus::connect().unwrap_or_else(|e| fail(&format!("cannot connect to the D-Bus session bus: {}", e)));
    let name = format!("{}-{}-1", ITEM, std::process::id());
    let lost = |e: std::io::Error| -> ! { fail(&format!("lost the D-Bus session bus ({})", e)) };
    match bus.bus_call(&mut rd, "RequestName", &[Arg::Str(name.clone()), Arg::U32(4)]).unwrap_or_else(|e| lost(e)) { // 4: do not queue
        Ok(m) if m.first_u32() == Some(1) => {}
        Ok(m) => fail(&format!("cannot own {} (RequestName answered {:?})", name, m.first_u32())),
        Err(e) => fail(&format!("cannot own {} ({})", name, e)),
    }
    let rule = format!("type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{}'", WATCHER);
    if let Err(e) = bus.bus_call(&mut rd, "AddMatch", &[Arg::Str(rule)]).unwrap_or_else(|e| lost(e)) {
        eprintln!("platmon-tray: will not notice a restarted StatusNotifierWatcher ({})", e);
    }
    let bus = Arc::new(bus);
    let shown = Arc::new(Mutex::new(crate::waiting(addr)));
    let url = crate::url(addr);
    {
        let (bus, shown, addr) = (bus.clone(), shown.clone(), addr.to_string());
        std::thread::spawn(move || crate::watch(&addr, |new| {
            let old = std::mem::replace(&mut *shown.lock().unwrap_or_else(|e| e.into_inner()), new.clone());
            for (member, body) in signals(&old, &new) {
                let _ = bus.signal(PATH, ITEM, member, &body); // a lost bus ends the reading thread
            }
        }));
    }
    let register = || bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem", &[Arg::Str(name.clone())])
        .unwrap_or_else(|e| lost(e));
    let (mut pending, mut registered) = (Some(register()), false);
    loop {
        let m = dbus::read(&mut rd).unwrap_or_else(|e| lost(e));
        match m.kind {
            dbus::METHOD_CALL => handle(&bus, &m, &shown, &url),
            dbus::METHOD_RETURN | dbus::ERROR if pending.is_some() && m.reply_serial == pending => {
                pending = None;
                if m.kind == dbus::METHOD_RETURN {
                    registered = true;
                } else if !registered {
                    eprintln!("platmon-tray: no StatusNotifierWatcher on the session bus ({}): this desktop shows no tray icons; \
                               see man platmon-tray", m.error_name.as_deref().unwrap_or(""));
                    std::process::exit(3);
                } else {
                    eprintln!("platmon-tray: the StatusNotifierWatcher refused the icon again ({})", m.error_name.as_deref().unwrap_or(""));
                }
            }
            dbus::SIGNAL if m.member.as_deref() == Some("NameOwnerChanged") && m.sender.as_deref() == Some("org.freedesktop.DBus") => {
                let a = m.strs();
                if a.len() == 3 && a[0] == WATCHER && !a[2].is_empty() { // a new watcher: a restarted panel
                    pending = Some(register());
                }
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shown(level: Level, text: &str) -> Shown {
        Shown { level, title: "orin — ok".into(), text: text.into() }
    }

    #[test]
    fn properties_follow_the_level() {
        let p = properties(&shown(Level::Critical, "CPU 95.0 %"));
        let get = |k: &str| p.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone()).unwrap();
        assert_eq!(get("Status"), Arg::Str("NeedsAttention".into()));
        assert_eq!(get("IconName"), Arg::Str("dialog-error".into()));
        assert_eq!(get("ToolTip"), Arg::ToolTip("dialog-error".into(), "orin — ok".into(), "CPU 95.0 %".into()));
        assert_eq!(get("Category"), Arg::Str("SystemServices".into()));
        assert_eq!(get("ItemIsMenu"), Arg::Bool(false));
        assert!(p.iter().all(|(n, _)| n != "Menu"));
        assert_eq!(properties(&shown(Level::Down, "")).iter().find(|(n, _)| n == "Status").unwrap().1, Arg::Str("Active".into()));
    }

    #[test]
    fn signals_only_for_what_changed() {
        let ok = shown(Level::Ok, "CPU 1%");
        assert!(signals(&ok, &ok).is_empty());
        let names = |a: &Shown, b: &Shown| signals(a, b).into_iter().map(|(n, _)| n).collect::<Vec<_>>();
        assert_eq!(names(&ok, &shown(Level::Ok, "CPU 2%")), ["NewToolTip"]);
        assert_eq!(names(&ok, &shown(Level::Down, "x")), ["NewIcon", "NewAttentionIcon", "NewToolTip"]);
        assert_eq!(signals(&ok, &shown(Level::Warning, "x"))[2], ("NewStatus", vec![Arg::Str("NeedsAttention".into())]));
    }
}

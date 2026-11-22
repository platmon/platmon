//! The little of D-Bus the tray needs, written on a UnixStream (no D-Bus crate; the specification's "Message
//! Protocol" and "Authentication Protocol"): the session bus's address, SASL EXTERNAL with the uid, Hello, and
//! messages with the types y b i u x s o g v as a{sv} (sa(iiay)ss), written little-endian. Messages read
//! may be either byte order; their bodies are read for strings, unsigned and signed integers and booleans only.
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;
use std::time::Duration;

pub const METHOD_CALL: u8 = 1;
pub const METHOD_RETURN: u8 = 2;
pub const ERROR: u8 = 3;
pub const SIGNAL: u8 = 4;
pub const NO_REPLY_EXPECTED: u8 = 1;
// header field codes
const PATH: u8 = 1;
const INTERFACE: u8 = 2;
const MEMBER: u8 = 3;
const ERROR_NAME: u8 = 4;
const REPLY_SERIAL: u8 = 5;
const DESTINATION: u8 = 6;
const SENDER: u8 = 7;
const SIGNATURE: u8 = 8;
/// The specification's longest message.
const MAX_MESSAGE: usize = 128 << 20;

const BUS: &str = "org.freedesktop.DBus";
const BUS_PATH: &str = "/org/freedesktop/DBus";

/// A value to write (the types the tray was specified with; not all are sent). ToolTip is StatusNotifierItem's
/// (icon name, pixmaps, title, text), pixmaps always empty.
#[derive(Clone, Debug, PartialEq)]
#[allow(dead_code)] // Byte, I64, Path and Strs: written and tested, not sent by the item
pub enum Arg {
    Byte(u8),
    Bool(bool),
    I32(i32),
    U32(u32),
    I64(i64),
    Str(String),
    Path(String),
    Sig(String),
    Var(Box<Arg>),
    Strs(Vec<String>),
    Dict(Vec<(String, Arg)>),
    ToolTip(String, String, String),
}

impl Arg {
    pub fn sig(&self) -> &'static str {
        match self {
            Arg::Byte(_) => "y",
            Arg::Bool(_) => "b",
            Arg::I32(_) => "i",
            Arg::U32(_) => "u",
            Arg::I64(_) => "x",
            Arg::Str(_) => "s",
            Arg::Path(_) => "o",
            Arg::Sig(_) => "g",
            Arg::Var(_) => "v",
            Arg::Strs(_) => "as",
            Arg::Dict(_) => "a{sv}",
            Arg::ToolTip(..) => "(sa(iiay)ss)",
        }
    }

    /// Appends the value to w, aligned as the specification wants relative to w's start: a message's header
    /// and its body (which starts 8-aligned) are each written into their own buffer.
    pub fn write(&self, w: &mut Vec<u8>) {
        match self {
            Arg::Byte(v) => w.push(*v),
            Arg::Bool(v) => put_u32(w, *v as u32),
            Arg::I32(v) => { pad(w, 4); w.extend_from_slice(&v.to_le_bytes()) }
            Arg::U32(v) => put_u32(w, *v),
            Arg::I64(v) => { pad(w, 8); w.extend_from_slice(&v.to_le_bytes()) }
            Arg::Str(t) | Arg::Path(t) => put_str(w, t),
            Arg::Sig(t) => put_sig(w, t),
            Arg::Var(a) => { put_sig(w, a.sig()); a.write(w) }
            Arg::Strs(v) => array(w, 4, |w| v.iter().for_each(|t| put_str(w, t))),
            Arg::Dict(v) => array(w, 8, |w| for (k, a) in v {
                pad(w, 8);
                put_str(w, k);
                put_sig(w, a.sig());
                a.write(w);
            }),
            Arg::ToolTip(icon, title, text) => {
                pad(w, 8);
                put_str(w, icon);
                array(w, 8, |_| {}); // a(iiay): no pixmaps, the icon name says it
                put_str(w, title);
                put_str(w, text);
            }
        }
    }
}

fn pad(w: &mut Vec<u8>, align: usize) {
    while w.len() % align != 0 {
        w.push(0);
    }
}

fn put_u32(w: &mut Vec<u8>, v: u32) {
    pad(w, 4);
    w.extend_from_slice(&v.to_le_bytes());
}

/// A string without NULs (D-Bus forbids them; one from the server's answer is dropped).
fn put_str(w: &mut Vec<u8>, t: &str) {
    let t = t.replace('\0', "");
    put_u32(w, t.len() as u32);
    w.extend_from_slice(t.as_bytes());
    w.push(0);
}

fn put_sig(w: &mut Vec<u8>, t: &str) {
    w.push(t.len() as u8);
    w.extend_from_slice(t.as_bytes());
    w.push(0);
}

/// An array: its length, the padding to its first element (there even when it is empty), the elements.
fn array(w: &mut Vec<u8>, align: usize, elements: impl FnOnce(&mut Vec<u8>)) {
    put_u32(w, 0);
    let at = w.len() - 4;
    pad(w, align);
    let start = w.len();
    elements(w);
    let n = (w.len() - start) as u32;
    w[at..at + 4].copy_from_slice(&n.to_le_bytes());
}

/// A whole message: the header (byte order, type, flags, version 1, body length, serial, fields) padded to 8,
/// then the body; the signature field from the body's arguments.
pub fn encode(kind: u8, flags: u8, serial: u32, fields: &[(u8, Arg)], body: &[Arg]) -> Vec<u8> {
    let mut b = Vec::new();
    body.iter().for_each(|a| a.write(&mut b));
    let sig: String = body.iter().map(Arg::sig).collect();
    let mut h = vec![b'l', kind, flags, 1];
    put_u32(&mut h, b.len() as u32);
    put_u32(&mut h, serial);
    let signature = (!sig.is_empty()).then(|| (SIGNATURE, Arg::Sig(sig)));
    array(&mut h, 8, |h| for (code, a) in fields.iter().chain(signature.iter()) {
        pad(h, 8);
        h.push(*code);
        put_sig(h, a.sig());
        a.write(h);
    });
    pad(&mut h, 8);
    h.extend_from_slice(&b);
    h
}

/// A message read from the bus: its header fields and its body as it came.
#[derive(Debug, Default)]
pub struct Message {
    pub kind: u8,
    pub flags: u8,
    pub serial: u32,
    pub path: Option<String>,
    pub interface: Option<String>,
    pub member: Option<String>,
    pub error_name: Option<String>,
    pub reply_serial: Option<u32>,
    pub destination: Option<String>,
    pub sender: Option<String>,
    pub signature: String,
    pub body: Vec<u8>,
    pub le: bool,
}

impl Message {
    /// The body's leading string arguments (s, o, g, a v holding one), up to the first of another type.
    pub fn strs(&self) -> Vec<String> {
        let mut r = Reader { data: &self.body, pos: 0, le: self.le };
        let mut out = Vec::new();
        for c in self.signature.chars() {
            let next = match c {
                's' | 'o' => r.string(),
                'g' => r.sig(),
                'v' => match r.variant() { Some(Variant::Str(t)) => Some(t), _ => None },
                _ => None,
            };
            match next {
                Some(t) => out.push(t),
                None => break,
            }
        }
        out
    }

    /// The body's first argument when it is a u.
    pub fn first_u32(&self) -> Option<u32> {
        if !self.signature.starts_with('u') { return None }
        Reader { data: &self.body, pos: 0, le: self.le }.u32()
    }
}

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
    le: bool,
}

impl Reader<'_> {
    fn align(&mut self, a: usize) {
        self.pos = self.pos.div_ceil(a) * a;
    }

    fn take(&mut self, n: usize) -> Option<&[u8]> {
        let out = self.data.get(self.pos..self.pos.checked_add(n)?)?;
        self.pos += n;
        Some(out)
    }

    fn byte(&mut self) -> Option<u8> {
        self.take(1).map(|b| b[0])
    }

    fn u32(&mut self) -> Option<u32> {
        self.align(4);
        let b: [u8; 4] = self.take(4)?.try_into().ok()?;
        Some(if self.le { u32::from_le_bytes(b) } else { u32::from_be_bytes(b) })
    }

    fn string(&mut self) -> Option<String> {
        let n = self.u32()? as usize;
        let t = String::from_utf8(self.take(n)?.to_vec()).ok()?;
        self.byte()?;
        Some(t)
    }

    fn sig(&mut self) -> Option<String> {
        let n = self.byte()? as usize;
        let t = String::from_utf8(self.take(n)?.to_vec()).ok()?;
        self.byte()?;
        Some(t)
    }

    /// A variant of a basic type (what header fields hold); None for others.
    fn variant(&mut self) -> Option<Variant> {
        let sig = self.sig()?;
        Some(match sig.as_str() {
            "s" | "o" => Variant::Str(self.string()?),
            "g" => Variant::Str(self.sig()?),
            "u" => Variant::U32(self.u32()?),
            "y" => { self.byte()?; Variant::Other }
            "b" | "i" => { self.u32()?; Variant::Other }
            "x" | "t" | "d" => { self.align(8); self.take(8)?; Variant::Other }
            _ => return None,
        })
    }
}

enum Variant {
    Str(String),
    U32(u32),
    Other,
}

/// Parses one whole message (its length already checked by read()).
pub fn decode(data: &[u8]) -> Option<Message> {
    let le = match data.first()? { b'l' => true, b'B' => false, _ => return None };
    let mut r = Reader { data, pos: 1, le };
    let mut m = Message { kind: r.byte()?, flags: r.byte()?, le, ..Default::default() };
    r.byte()?; // version
    let body_len = r.u32()? as usize;
    m.serial = r.u32()?;
    let fields_len = r.u32()? as usize;
    let end = r.pos + fields_len;
    while r.pos < end {
        r.align(8);
        let code = r.byte()?;
        let v = r.variant()?;
        match (code, v) {
            (PATH, Variant::Str(t)) => m.path = Some(t),
            (INTERFACE, Variant::Str(t)) => m.interface = Some(t),
            (MEMBER, Variant::Str(t)) => m.member = Some(t),
            (ERROR_NAME, Variant::Str(t)) => m.error_name = Some(t),
            (REPLY_SERIAL, Variant::U32(u)) => m.reply_serial = Some(u),
            (DESTINATION, Variant::Str(t)) => m.destination = Some(t),
            (SENDER, Variant::Str(t)) => m.sender = Some(t),
            (SIGNATURE, Variant::Str(t)) => m.signature = t,
            _ => {} // unknown fields are ignored, as the specification asks
        }
    }
    r.pos = end;
    r.align(8);
    m.body = r.take(body_len)?.to_vec();
    Some(m)
}

/// Reads the next message; one this cannot parse comes back as a Message of kind 0, skipped whole.
pub fn read(s: &mut UnixStream) -> io::Result<Message> {
    let mut head = [0u8; 16];
    s.read_exact(&mut head)?;
    let le = head[0] == b'l';
    let word = |b: &[u8]| { let b: [u8; 4] = b.try_into().unwrap(); (if le { u32::from_le_bytes(b) } else { u32::from_be_bytes(b) }) as usize };
    let (body_len, fields_len) = (word(&head[4..8]), word(&head[12..16]));
    let total = (16 + fields_len).div_ceil(8) * 8 + body_len;
    if total > MAX_MESSAGE {
        return Err(io::Error::other("a message longer than D-Bus allows"));
    }
    let mut data = head.to_vec();
    data.resize(total, 0);
    s.read_exact(&mut data[16..])?;
    Ok(decode(&data).unwrap_or_default())
}

/// One address of DBUS_SESSION_BUS_ADDRESS that this can connect to.
#[derive(Debug, PartialEq)]
pub enum Address {
    Path(String),
    Abstract(String),
}

/// The unix: addresses with a path= or abstract=, in order; others (tcp:, launchd:, unix:dir=...) skipped.
pub fn addresses(var: &str) -> Vec<Address> {
    var.split(';').filter_map(|entry| {
        let rest = entry.strip_prefix("unix:")?;
        rest.split(',').find_map(|kv| {
            if let Some(v) = kv.strip_prefix("path=") { Some(Address::Path(unescape(v))) }
            else { kv.strip_prefix("abstract=").map(|v| Address::Abstract(unescape(v))) }
        })
    }).collect()
}

/// An address value's %XX escapes.
fn unescape(v: &str) -> String {
    let b = v.as_bytes();
    let (mut out, mut i) = (Vec::new(), 0);
    while i < b.len() {
        let hex = (b[i] == b'%').then(|| v.get(i + 1..i + 3).and_then(|h| u8::from_str_radix(h, 16).ok())).flatten();
        match hex {
            Some(c) => { out.push(c); i += 3 }
            None => { out.push(b[i]); i += 1 }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn open(a: &Address) -> io::Result<UnixStream> {
    match a {
        Address::Path(p) => UnixStream::connect(p),
        #[cfg(any(target_os = "linux", target_os = "android"))]
        Address::Abstract(name) => {
            use std::os::linux::net::SocketAddrExt;
            UnixStream::connect_addr(&std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes())?)
        }
        #[cfg(not(any(target_os = "linux", target_os = "android")))]
        Address::Abstract(_) => Err(io::Error::new(io::ErrorKind::Unsupported, "abstract sockets are Linux's")),
    }
}

/// The writing half, shared by the thread that reads (replies) and the poller (signals).
pub struct Bus {
    w: Mutex<UnixStream>,
    serial: AtomicU32,
}

impl Bus {
    pub fn send(&self, kind: u8, flags: u8, fields: &[(u8, Arg)], body: &[Arg]) -> io::Result<u32> {
        let serial = self.serial.fetch_add(1, Ordering::Relaxed);
        let m = encode(kind, flags, serial, fields, body);
        self.w.lock().unwrap_or_else(|e| e.into_inner()).write_all(&m)?;
        Ok(serial)
    }

    pub fn call(&self, dest: &str, path: &str, iface: &str, member: &str, body: &[Arg]) -> io::Result<u32> {
        self.send(METHOD_CALL, 0, &[(PATH, Arg::Path(path.into())), (INTERFACE, Arg::Str(iface.into())), (MEMBER, Arg::Str(member.into())),
                                    (DESTINATION, Arg::Str(dest.into()))], body)
    }

    pub fn reply(&self, to: &Message, body: &[Arg]) -> io::Result<u32> {
        let mut fields = vec![(REPLY_SERIAL, Arg::U32(to.serial))];
        fields.extend(to.sender.clone().map(|s| (DESTINATION, Arg::Str(s))));
        self.send(METHOD_RETURN, NO_REPLY_EXPECTED, &fields, body)
    }

    pub fn error(&self, to: &Message, name: &str, text: &str) -> io::Result<u32> {
        let mut fields = vec![(REPLY_SERIAL, Arg::U32(to.serial)), (ERROR_NAME, Arg::Str(name.into()))];
        fields.extend(to.sender.clone().map(|s| (DESTINATION, Arg::Str(s))));
        self.send(ERROR, NO_REPLY_EXPECTED, &fields, &[Arg::Str(text.into())])
    }

    pub fn signal(&self, path: &str, iface: &str, member: &str, body: &[Arg]) -> io::Result<u32> {
        self.send(SIGNAL, NO_REPLY_EXPECTED, &[(PATH, Arg::Path(path.into())), (INTERFACE, Arg::Str(iface.into())),
                                               (MEMBER, Arg::Str(member.into()))], body)
    }

    /// A call to the bus itself, waiting for its answer; anything else read meanwhile is dropped (only for the
    /// calls made before the item is exported). Ok(the reply) or Err(the error's name and text).
    pub fn bus_call(&self, rd: &mut UnixStream, member: &str, body: &[Arg]) -> io::Result<Result<Message, String>> {
        let serial = self.call(BUS, BUS_PATH, BUS, member, body)?;
        loop {
            let m = read(rd)?;
            if m.reply_serial == Some(serial) && (m.kind == METHOD_RETURN || m.kind == ERROR) {
                if m.kind == METHOD_RETURN { return Ok(Ok(m)) }
                return Ok(Err(format!("{}: {}", m.error_name.clone().unwrap_or_default(), m.strs().join(" "))));
            }
        }
    }
}

/// A line of the authentication exchange, up to "\r\n".
fn read_line(s: &mut UnixStream) -> io::Result<String> {
    let (mut line, mut b) = (Vec::new(), [0u8; 1]);
    while !line.ends_with(b"\r\n") {
        if line.len() > 4096 { return Err(io::Error::other("an authentication line without an end")) }
        s.read_exact(&mut b)?;
        line.push(b[0]);
    }
    Ok(String::from_utf8_lossy(&line).trim_end().to_string())
}

/// SASL EXTERNAL: the uid as hex-encoded ASCII digits; the server checks it against the socket's credentials.
fn authenticate(s: &mut UnixStream) -> io::Result<()> {
    // SAFETY: getuid cannot fail.
    let uid = unsafe { libc::getuid() };
    let hex: String = uid.to_string().bytes().map(|b| format!("{:02x}", b)).collect();
    s.write_all(format!("\0AUTH EXTERNAL {}\r\n", hex).as_bytes())?;
    let answer = read_line(s)?;
    if !answer.starts_with("OK ") {
        return Err(io::Error::other(format!("the bus refused EXTERNAL authentication ({})", answer)));
    }
    s.write_all(b"BEGIN\r\n")
}

/// The session bus: (the writing half, the reading half, this connection's unique name). Its address from
/// DBUS_SESSION_BUS_ADDRESS, else $XDG_RUNTIME_DIR/bus (where systemd's user bus is).
pub fn connect() -> Result<(Bus, UnixStream, String), String> {
    let list = match std::env::var("DBUS_SESSION_BUS_ADDRESS") {
        Ok(v) if !v.is_empty() => {
            let list = addresses(&v);
            if list.is_empty() { return Err(format!("DBUS_SESSION_BUS_ADDRESS has no unix:path= or unix:abstract= address ({})", v)) }
            list
        }
        _ => match std::env::var("XDG_RUNTIME_DIR") {
            Ok(d) if !d.is_empty() => vec![Address::Path(format!("{}/bus", d))],
            _ => return Err("neither DBUS_SESSION_BUS_ADDRESS nor XDG_RUNTIME_DIR is set".into()),
        },
    };
    let mut last = String::new();
    for a in &list {
        let attempt = || -> io::Result<(Bus, UnixStream, String)> {
            let mut s = open(a)?;
            s.set_read_timeout(Some(Duration::from_secs(5)))?;
            authenticate(&mut s)?;
            let bus = Bus { w: Mutex::new(s.try_clone()?), serial: AtomicU32::new(1) };
            let name = match bus.bus_call(&mut s, "Hello", &[])? {
                Ok(m) => m.strs().into_iter().next().unwrap_or_default(),
                Err(e) => return Err(io::Error::other(e)),
            };
            s.set_read_timeout(None)?;
            Ok((bus, s, name))
        };
        match attempt() {
            Ok(c) => return Ok(c),
            Err(e) => last = format!("{:?}: {}", a, e),
        }
    }
    Err(last)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basic_types_as_the_specification_lays_them_out() {
        let bytes = |args: &[Arg]| { let mut w = Vec::new(); args.iter().for_each(|a| a.write(&mut w)); w };
        assert_eq!(bytes(&[Arg::Byte(7), Arg::Bool(true)]), [7, 0, 0, 0, 1, 0, 0, 0]);
        assert_eq!(bytes(&[Arg::Byte(1), Arg::I32(-2)]), [1, 0, 0, 0, 0xfe, 0xff, 0xff, 0xff]);
        assert_eq!(bytes(&[Arg::Byte(1), Arg::I64(1)]), [1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]);
        assert_eq!(bytes(&[Arg::Str("ab".into())]), [2, 0, 0, 0, b'a', b'b', 0]);
        assert_eq!(bytes(&[Arg::Path("/".into())]), [1, 0, 0, 0, b'/', 0]);
        assert_eq!(bytes(&[Arg::Sig("as".into())]), [2, b'a', b's', 0]);
        // a variant: its signature, then the value aligned after it
        assert_eq!(bytes(&[Arg::Var(Box::new(Arg::U32(5)))]), [1, b'u', 0, 0, 5, 0, 0, 0]);
        assert_eq!(bytes(&[Arg::Strs(vec!["a".into(), "b".into()])]), [14, 0, 0, 0, 1, 0, 0, 0, b'a', 0, 0, 0, 1, 0, 0, 0, b'b', 0]);
        assert_eq!(bytes(&[Arg::Str("a\0b".into())]), [2, 0, 0, 0, b'a', b'b', 0]);
    }

    #[test]
    fn dict_entries_and_the_tooltip_struct_are_8_aligned() {
        let mut w = Vec::new();
        Arg::Dict(vec![("Id".into(), Arg::Str("p".into())), ("ItemIsMenu".into(), Arg::Bool(false))]).write(&mut w);
        let mut want = vec![48, 0, 0, 0, 0, 0, 0, 0]; // the length (from the first entry), padding to 8
        want.extend_from_slice(&[2, 0, 0, 0, b'I', b'd', 0, 1, b's', 0, 0, 0, 1, 0, 0, 0, b'p', 0]); // "Id" -> v s "p"
        want.extend_from_slice(&[0; 6]); // the next entry 8-aligned
        want.extend_from_slice(&[10, 0, 0, 0]);
        want.extend_from_slice(b"ItemIsMenu\0");
        want.extend_from_slice(&[1, b'b', 0, 0, 0, 0, 0, 0, 0]); // v b false
        assert_eq!(w, want);

        let mut w = vec![1]; // a byte first, so the struct's alignment shows
        Arg::ToolTip("i".into(), "T".into(), "x".into()).write(&mut w);
        assert_eq!(w, [1, 0, 0, 0, 0, 0, 0, 0, // the struct starts 8-aligned
                       1, 0, 0, 0, b'i', 0, 0, 0, // s "i", padding to the array's length
                       0, 0, 0, 0, 0, 0, 0, 0, // a(iiay): length 0, padding to its (iiay) elements even when empty
                       1, 0, 0, 0, b'T', 0, 0, 0, 1, 0, 0, 0, b'x', 0]);
        assert_eq!(Arg::ToolTip(String::new(), String::new(), String::new()).sig(), "(sa(iiay)ss)");
    }

    #[test]
    fn a_hello_call_byte_for_byte() {
        let m = encode(METHOD_CALL, 0, 1, &[(PATH, Arg::Path(BUS_PATH.into())), (DESTINATION, Arg::Str(BUS.into())),
                                            (INTERFACE, Arg::Str(BUS.into())), (MEMBER, Arg::Str("Hello".into()))], &[]);
        let mut want = vec![b'l', 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0x6e, 0, 0, 0];
        want.extend_from_slice(&[1, 1, b'o', 0, 21, 0, 0, 0]);
        want.extend_from_slice(b"/org/freedesktop/DBus\0");
        want.extend_from_slice(&[0, 0]);
        want.extend_from_slice(&[6, 1, b's', 0, 20, 0, 0, 0]);
        want.extend_from_slice(b"org.freedesktop.DBus\0");
        want.extend_from_slice(&[0, 0, 0]);
        want.extend_from_slice(&[2, 1, b's', 0, 20, 0, 0, 0]);
        want.extend_from_slice(b"org.freedesktop.DBus\0");
        want.extend_from_slice(&[0, 0, 0]);
        want.extend_from_slice(&[3, 1, b's', 0, 5, 0, 0, 0]);
        want.extend_from_slice(b"Hello\0");
        want.extend_from_slice(&[0, 0]); // the header padded to 8, no body
        assert_eq!(m, want);
    }

    #[test]
    fn messages_read_back_in_either_byte_order() {
        let data = encode(SIGNAL, 1, 9, &[(PATH, Arg::Path("/a".into())), (MEMBER, Arg::Str("NameOwnerChanged".into()))],
                          &[Arg::Str("x".into()), Arg::Str("".into()), Arg::Str(":1.2".into())]);
        let m = decode(&data).unwrap();
        assert_eq!((m.kind, m.flags, m.serial, m.path.as_deref(), m.member.as_deref(), m.signature.as_str()),
                   (SIGNAL, 1, 9, Some("/a"), Some("NameOwnerChanged"), "sss"));
        assert_eq!(m.strs(), ["x", "", ":1.2"]);
        // a big-endian method return with reply serial 3 and a u body (dbus-daemon's RequestName answer)
        let mut be = vec![b'B', 2, 1, 1, 0, 0, 0, 4, 0, 0, 0, 7, 0, 0, 0, 15];
        be.extend_from_slice(&[5, 1, b'u', 0, 0, 0, 0, 3, 8, 1, b'g', 0, 1, b'u', 0, 0, 0, 0, 0, 1]);
        let m = decode(&be).unwrap();
        assert_eq!((m.kind, m.serial, m.reply_serial, m.first_u32()), (METHOD_RETURN, 7, Some(3), Some(1)));
        assert!(decode(b"x").is_none() && decode(&data[..20]).is_none());
    }

    #[test]
    fn session_bus_addresses() {
        assert_eq!(addresses("unix:path=/run/user/1000/bus"), [Address::Path("/run/user/1000/bus".into())]);
        assert_eq!(addresses("unix:abstract=/tmp/dbus-AbC,guid=12ab"), [Address::Abstract("/tmp/dbus-AbC".into())]);
        assert_eq!(addresses("tcp:host=x,port=1;unix:guid=1,path=/tmp/a%20b"), [Address::Path("/tmp/a b".into())]);
        assert_eq!(addresses("unix:tmpdir=/tmp"), []);
        assert_eq!(unescape("%2fx%zz"), "/x%zz");
    }
}

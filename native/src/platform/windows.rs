//! Windows: BCryptGenRandom and a WinSock socket. Nothing here collects; the commands that do exit on
//! Windows (main.rs, collect_command), so the clocks and the stop request are not chosen yet.
use std::net::{Ipv4Addr, TcpListener};
use std::os::windows::io::FromRawSocket;
use windows_sys::Win32::Networking::WinSock as ws;
use windows_sys::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};

pub fn boottime_ns() -> Option<i64> {
    unimplemented!("no elapsed clock is chosen for Windows yet")
}

pub fn monotonic_ns() -> Option<i64> {
    unimplemented!("no elapsed clock is chosen for Windows yet")
}

/// Fills b from the system's preferred random generator; false if it could not.
pub fn random_bytes(b: &mut [u8]) -> bool {
    let Ok(len) = u32::try_from(b.len()) else { return false };
    unsafe { BCryptGenRandom(std::ptr::null_mut(), b.as_mut_ptr(), len, BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0 }
}

/// A listening IPv4 socket with the given backlog. SO_EXCLUSIVEADDRUSE, not SO_REUSEADDR: on Windows
/// SO_REUSEADDR would let another socket bind the same port and take its connections.
pub fn listen(ip: Ipv4Addr, port: u16, backlog: i32) -> std::io::Result<TcpListener> {
    static STARTED: std::sync::Once = std::sync::Once::new();
    let error = || std::io::Error::from_raw_os_error(unsafe { ws::WSAGetLastError() });
    unsafe {
        // std starts WinSock on its first socket; this one comes before any
        STARTED.call_once(|| {
            let mut data: ws::WSADATA = std::mem::zeroed();
            ws::WSAStartup(0x202, &mut data);
        });
        // not inherited by child processes, as SOCK_CLOEXEC on Linux; overlapped, as std makes its sockets
        let s = ws::WSASocketW(ws::AF_INET as i32, ws::SOCK_STREAM, ws::IPPROTO_TCP, std::ptr::null(), 0,
                               ws::WSA_FLAG_OVERLAPPED | ws::WSA_FLAG_NO_HANDLE_INHERIT);
        if s == ws::INVALID_SOCKET {
            return Err(error());
        }
        let listener = TcpListener::from_raw_socket(s as u64); // closes s on the error paths below
        let one: i32 = 1;
        let mut sa: ws::SOCKADDR_IN = std::mem::zeroed();
        sa.sin_family = ws::AF_INET;
        sa.sin_port = port.to_be();
        sa.sin_addr.S_un.S_addr = u32::from(ip).to_be();
        if ws::setsockopt(s, ws::SOL_SOCKET, ws::SO_EXCLUSIVEADDRUSE, (&one as *const i32).cast(), std::mem::size_of::<i32>() as i32) != 0
            || ws::bind(s, (&sa as *const ws::SOCKADDR_IN).cast(), std::mem::size_of::<ws::SOCKADDR_IN>() as i32) != 0
            || ws::listen(s, backlog) != 0 {
            return Err(error());
        }
        Ok(listener)
    }
}

pub fn exit_on_terminate() {
    unimplemented!("no stop request is handled on Windows yet")
}

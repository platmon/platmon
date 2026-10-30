//! Linux: clock_gettime, getrandom, a raw socket and SIGTERM.
use std::net::{Ipv4Addr, TcpListener};
use std::os::fd::FromRawFd;

fn os_clock(id: libc::clockid_t) -> Option<i64> {
    let mut t = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    (unsafe { libc::clock_gettime(id, &mut t) } == 0).then(|| t.tv_sec as i64 * 1_000_000_000 + t.tv_nsec as i64)
}

/// CLOCK_BOOTTIME in nanoseconds: keeps counting through suspend. None where the kernel has none.
pub fn boottime_ns() -> Option<i64> {
    os_clock(libc::CLOCK_BOOTTIME)
}

/// CLOCK_MONOTONIC in nanoseconds: stops while suspended.
pub fn monotonic_ns() -> Option<i64> {
    os_clock(libc::CLOCK_MONOTONIC)
}

/// Fills b from the kernel's random source; false if it could not.
pub fn random_bytes(b: &mut [u8]) -> bool {
    let n = unsafe { libc::getrandom(b.as_mut_ptr().cast(), b.len(), 0) };
    n == b.len() as isize
}

/// A listening IPv4 socket with SO_REUSEADDR and the given backlog.
pub fn listen(ip: Ipv4Addr, port: u16, backlog: i32) -> std::io::Result<TcpListener> {
    unsafe {
        let fd = libc::socket(libc::AF_INET, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0);
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let listener = TcpListener::from_raw_fd(fd); // closes fd on the error paths below
        let one: libc::c_int = 1;
        let mut sa: libc::sockaddr_in = std::mem::zeroed();
        sa.sin_family = libc::AF_INET as libc::sa_family_t;
        sa.sin_port = port.to_be();
        sa.sin_addr.s_addr = u32::from(ip).to_be();
        if libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_REUSEADDR, (&one as *const libc::c_int).cast(),
                            std::mem::size_of::<libc::c_int>() as libc::socklen_t) != 0
            || libc::bind(fd, (&sa as *const libc::sockaddr_in).cast(), std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t) != 0
            || libc::listen(fd, backlog) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(listener)
    }
}

extern "C" fn on_term(_: libc::c_int) {
    unsafe { libc::_exit(0) } // a clean exit on SIGTERM (systemd stop, docker stop)
}

/// Exits at once, with status 0, when the service is asked to stop.
pub fn exit_on_terminate() {
    unsafe { libc::signal(libc::SIGTERM, on_term as extern "C" fn(libc::c_int) as libc::sighandler_t) };
}

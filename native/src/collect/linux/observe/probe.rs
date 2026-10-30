//! The probe slow group's connection on Linux: connect_ms (the group itself is collect/probe.rs). Contract:
//! docs/reference/probe.md.

fn reason(code: i32) -> &'static str {
    match code {
        libc::ECONNREFUSED => "refused",
        libc::ENETUNREACH | libc::EHOSTUNREACH => "unreachable",
        libc::ETIMEDOUT => "timeout",
        _ => "error",
    }
}

fn monotonic_ns() -> i64 {
    let mut t = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t) };
    t.tv_sec as i64 * 1_000_000_000 + t.tv_nsec as i64
}

/// probe.connect_ms: (ms to complete the connection, None) or (None, reason). One socket, closed before
/// returning.
pub fn connect_ms(address: &str, port: u16, timeout: f64) -> (Option<f64>, Option<&'static str>) {
    let Ok(ip) = address.parse::<std::net::IpAddr>() else { return (None, Some("error")) };
    unsafe {
        let family = if ip.is_ipv6() { libc::AF_INET6 } else { libc::AF_INET };
        let fd = libc::socket(family, libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC, 0);
        if fd < 0 {
            return (None, Some(reason(*libc::__errno_location())));
        }
        let start = monotonic_ns();
        let rc = match ip {
            std::net::IpAddr::V4(a) => {
                let mut sa: libc::sockaddr_in = std::mem::zeroed();
                sa.sin_family = libc::AF_INET as libc::sa_family_t;
                sa.sin_port = port.to_be();
                sa.sin_addr.s_addr = u32::from(a).to_be();
                libc::connect(fd, (&sa as *const libc::sockaddr_in).cast(), std::mem::size_of::<libc::sockaddr_in>() as u32)
            }
            std::net::IpAddr::V6(a) => {
                let mut sa: libc::sockaddr_in6 = std::mem::zeroed();
                sa.sin6_family = libc::AF_INET6 as libc::sa_family_t;
                sa.sin6_port = port.to_be();
                sa.sin6_addr.s6_addr = a.octets();
                libc::connect(fd, (&sa as *const libc::sockaddr_in6).cast(), std::mem::size_of::<libc::sockaddr_in6>() as u32)
            }
        };
        let out = (|| {
            if rc != 0 {
                let e = *libc::__errno_location();
                if e != libc::EINPROGRESS {
                    return (None, Some(reason(e)));
                }
                let mut p = libc::pollfd { fd, events: libc::POLLOUT, revents: 0 };
                let deadline = start + (timeout * 1e9) as i64;
                loop {
                    let left = ((deadline - monotonic_ns()) as f64 / 1e6).ceil().max(0.0) as i32;
                    let n = libc::poll(&mut p, 1, left);
                    if n < 0 && *libc::__errno_location() == libc::EINTR {
                        continue;
                    }
                    if n <= 0 {
                        return (None, Some("timeout"));
                    }
                    break;
                }
                let mut err: libc::c_int = 0;
                let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
                libc::getsockopt(fd, libc::SOL_SOCKET, libc::SO_ERROR, (&mut err as *mut libc::c_int).cast(), &mut len);
                if err != 0 {
                    return (None, Some(reason(err)));
                }
            }
            (Some(crate::json::round_to((monotonic_ns() - start) as f64 / 1e6, 3)), None)
        })();
        libc::close(fd);
        out
    }
}

// tests/collector/test_probe.py's connect_ms tests, ported (docs/development/python-test-port.md).
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connect_to_a_loopback_listener() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let (ms, why) = connect_ms("127.0.0.1", listener.local_addr().unwrap().port(), 2.0);
        assert!(matches!(ms, Some(m) if (0.0..2000.0).contains(&m)) && why.is_none(), "{:?} {:?}", ms, why);
    }

    #[test]
    fn refused_is_a_failure_not_a_time() {
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port(); // closed again
        assert_eq!(connect_ms("127.0.0.1", port, 2.0), (None, Some("refused")));
    }

    #[test]
    fn timeout_is_reported_without_a_time() {
        // a listener whose accept queue is full drops the next SYN: the connect waits past the timeout
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let fd = std::os::fd::AsRawFd::as_raw_fd(&listener);
        assert_eq!(unsafe { libc::listen(fd, 0) }, 0);
        let mut held = Vec::new();
        for _ in 0..4 {
            if let Ok(c) = std::net::TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), std::time::Duration::from_millis(100)) {
                held.push(c);
            }
        }
        assert_eq!(connect_ms("127.0.0.1", port, 0.05), (None, Some("timeout")));
    }

    #[test]
    fn a_socket_that_cannot_be_made_is_an_error() {
        assert_eq!(connect_ms("not an address", 80, 0.1), (None, Some("error")));
        assert_eq!(reason(libc::EAFNOSUPPORT), "error"); // socket() refused, e.g. IPv6 unavailable
        assert_eq!((reason(libc::ECONNREFUSED), reason(libc::ENETUNREACH), reason(libc::EHOSTUNREACH), reason(libc::ETIMEDOUT)),
                   ("refused", "unreachable", "unreachable", "timeout"));
    }
}

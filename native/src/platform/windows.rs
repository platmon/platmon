//! Windows: the interrupt time, BCryptGenRandom, a WinSock socket, the console's stop events and the service
//! control manager's.
use std::net::{Ipv4Addr, TcpListener};
use std::os::windows::io::FromRawSocket;
use windows_sys::Win32::Networking::WinSock as ws;
use windows_sys::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};
use std::sync::atomic::{AtomicPtr, Ordering};
use std::sync::OnceLock;
use windows_sys::Win32::System::Console::{SetConsoleCtrlHandler, CTRL_LOGOFF_EVENT};
use windows_sys::Win32::System::Services as svc;
use windows_sys::Win32::System::WindowsProgramming::{QueryInterruptTimePrecise, QueryUnbiasedInterruptTimePrecise};

/// The interrupt time in nanoseconds (100 ns units): keeps counting through sleep and hibernation, as
/// CLOCK_BOOTTIME, so the service reports its clock as "boottime" here too.
pub fn boottime_ns() -> Option<i64> {
    let mut t = 0u64;
    unsafe { QueryInterruptTimePrecise(&mut t) };
    i64::try_from(t).ok()?.checked_mul(100)
}

/// The unbiased interrupt time in nanoseconds: stops while asleep, as CLOCK_MONOTONIC.
pub fn monotonic_ns() -> Option<i64> {
    let mut t = 0u64;
    unsafe { QueryUnbiasedInterruptTimePrecise(&mut t) };
    i64::try_from(t).ok()?.checked_mul(100)
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

unsafe extern "system" fn on_stop(event: u32) -> windows_sys::core::BOOL {
    if event == CTRL_LOGOFF_EVENT && !STATUS.load(Ordering::Acquire).is_null() {
        return 1; // a user logging off does not stop a service
    }
    std::process::exit(0) // Ctrl+C, Ctrl+Break, the console closed, logoff or shutdown: as SIGTERM on Linux
}

/// Exits at once, with status 0, when the console asks the process to stop (a service: at shutdown, not at
/// a user's logoff). The service control manager's stop: run_service.
pub fn exit_on_terminate() {
    unsafe { SetConsoleCtrlHandler(Some(on_stop), 1) };
}

/// The service's status handle, null outside run_service.
static STATUS: AtomicPtr<core::ffi::c_void> = AtomicPtr::new(std::ptr::null_mut());
static SERVE: OnceLock<Box<dyn Fn() + Send + Sync>> = OnceLock::new();
/// The stop request, from the control handler to service_main.
static STOP: OnceLock<std::sync::mpsc::Sender<()>> = OnceLock::new();

fn report(state: u32) {
    let accepted = if state == svc::SERVICE_RUNNING { svc::SERVICE_ACCEPT_STOP | svc::SERVICE_ACCEPT_SHUTDOWN } else { 0 };
    let status = svc::SERVICE_STATUS { dwServiceType: svc::SERVICE_WIN32_OWN_PROCESS, dwCurrentState: state,
                                       dwControlsAccepted: accepted, dwWin32ExitCode: 0, dwServiceSpecificExitCode: 0,
                                       dwCheckPoint: 0, dwWaitHint: 0 };
    unsafe { svc::SetServiceStatus(STATUS.load(Ordering::Acquire), &status) };
}

unsafe extern "system" fn on_control(control: u32, _: u32, _: *mut core::ffi::c_void, _: *mut core::ffi::c_void) -> u32 {
    match control {
        // service_main reports stopped: exiting here would leave the request unanswered (ERROR_BROKEN_PIPE)
        svc::SERVICE_CONTROL_STOP | svc::SERVICE_CONTROL_SHUTDOWN => {
            report(svc::SERVICE_STOP_PENDING);
            if let Some(stop) = STOP.get() {
                let _ = stop.send(());
            }
            0
        }
        svc::SERVICE_CONTROL_INTERROGATE => 0,
        _ => 120, // ERROR_CALL_NOT_IMPLEMENTED
    }
}

unsafe extern "system" fn service_main(_: u32, _: *mut windows_sys::core::PWSTR) {
    let name = [0u16]; // not checked for a service in its own process
    let handle = svc::RegisterServiceCtrlHandlerExW(name.as_ptr(), Some(on_control), std::ptr::null());
    if handle.is_null() {
        std::process::exit(1);
    }
    STATUS.store(handle, Ordering::Release);
    let (stop, stopped) = std::sync::mpsc::channel();
    let _ = STOP.set(stop);
    std::thread::spawn(|| SERVE.get().expect("run_service sets it")());
    report(svc::SERVICE_RUNNING);
    let _ = stopped.recv();
    report(svc::SERVICE_STOPPED); // then the dispatcher returns and run_service exits
}

/// Runs serve as a Windows service in its own process (SERVICE_WIN32_OWN_PROCESS): running once it has
/// connected to the service control manager, stopped on its stop or shutdown request, then the exit with
/// status 0 as on SIGTERM (nothing to flush: the history is in memory). Exits with status 2 when the
/// process was not started by the service control manager.
pub fn run_service(serve: impl Fn() + Send + Sync + 'static) {
    let _ = SERVE.set(Box::new(serve));
    let mut name = [0u16];
    let table = [svc::SERVICE_TABLE_ENTRYW { lpServiceName: name.as_mut_ptr(), lpServiceProc: Some(service_main) },
                 svc::SERVICE_TABLE_ENTRYW::default()];
    if unsafe { svc::StartServiceCtrlDispatcherW(table.as_ptr()) } == 0 {
        eprintln!("platmon-server service: {} (the service control manager starts it; serve runs in a console)",
                  std::io::Error::last_os_error());
        std::process::exit(2);
    }
    std::process::exit(0)
}

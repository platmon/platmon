//! The tray icon on Windows: Shell_NotifyIconW with the system's stock icons, owned by a hidden window. Not a
//! message-only window (HWND_MESSAGE): those never get broadcasts, and TaskbarCreated (Explorer restarted, or
//! started after the tray at login) is one; the icon is added again then. A left click or Enter opens the web
//! page. --autostart writes the user's Run value (the MSI's machine-wide one is separate).
use crate::{Level, Shown, TIP_UNITS};
use std::sync::atomic::{AtomicU32, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};
use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::Console::{AttachConsole, GetStdHandle, ATTACH_PARENT_PROCESS, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Registry::{RegDeleteKeyValueW, RegSetKeyValueW, HKEY_CURRENT_USER, REG_SZ};
use windows_sys::Win32::UI::Shell::{ShellExecuteW, Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_SHOWTIP, NIF_TIP, NIM_ADD, NIM_DELETE,
                                    NIM_MODIFY, NIM_SETVERSION, NIN_SELECT, NOTIFYICONDATAW, NOTIFYICON_VERSION_4};
use windows_sys::Win32::UI::WindowsAndMessaging::{CreateWindowExW, DefWindowProcW, DispatchMessageW, GetMessageW, LoadIconW, PostMessageW,
                                                  PostQuitMessage, RegisterClassExW, RegisterWindowMessageW, IDI_ERROR, IDI_INFORMATION,
                                                  IDI_QUESTION, IDI_WARNING, MSG, SW_SHOWNORMAL, WM_APP, WM_DESTROY, WM_ENDSESSION,
                                                  WNDCLASSEXW,
                                                  WS_EX_TOOLWINDOW, WS_OVERLAPPED};

/// The icon's messages to the window (uCallbackMessage).
const CALLBACK: u32 = WM_APP + 1;
/// The poller's: what to show changed.
const CHANGED: u32 = WM_APP + 2;
/// NIN_KEYSELECT: NIN_SELECT | NINF_KEY, Enter or Space on the focused icon.
const NIN_KEYSELECT: u32 = NIN_SELECT | 1;
const RUN: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";

static SHOWN: Mutex<Option<Shown>> = Mutex::new(None);
static URL: OnceLock<String> = OnceLock::new();
static WINDOW: AtomicUsize = AtomicUsize::new(0);
static TASKBAR_CREATED: AtomicU32 = AtomicU32::new(0);

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain([0]).collect()
}

/// Output for --help, --version and errors: the parent's console when there is no standard handle (a GUI
/// program started from cmd or PowerShell); a pipe or file given by the caller stays.
pub fn attach_console() {
    // SAFETY: plain calls; AttachConsole fails harmlessly without a parent console.
    unsafe {
        if GetStdHandle(STD_OUTPUT_HANDLE).is_null() && GetStdHandle(STD_ERROR_HANDLE).is_null() {
            AttachConsole(ATTACH_PARENT_PROCESS);
        }
    }
}

pub fn open(url: &str) {
    let (verb, file) = (wide("open"), wide(url));
    // SAFETY: NUL-terminated strings that outlive the call.
    unsafe { ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), file.as_ptr(), std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL) };
}

/// Some(command): HKCU\...\Run\platmon-tray = command; None: that value removed.
pub fn autostart(command: Option<&str>) -> Result<String, String> {
    let (key, name) = (wide(RUN), wide("platmon-tray"));
    // SAFETY: NUL-terminated strings and the data's length in bytes, terminator included.
    let code = unsafe {
        match command {
            Some(c) => {
                let data = wide(c);
                RegSetKeyValueW(HKEY_CURRENT_USER, key.as_ptr(), name.as_ptr(), REG_SZ, data.as_ptr().cast(), (data.len() * 2) as u32)
            }
            None => RegDeleteKeyValueW(HKEY_CURRENT_USER, key.as_ptr(), name.as_ptr()),
        }
    };
    let place = format!(r"HKEY_CURRENT_USER\{}\platmon-tray", RUN);
    match (command, code) {
        (Some(c), 0) => Ok(format!("wrote {} = {}: platmon-tray starts at your next login", place, c)),
        (None, 0) => Ok(format!("removed {}", place)),
        (None, 2) => Ok(format!("{} does not exist; nothing to remove", place)), // ERROR_FILE_NOT_FOUND
        (_, code) => Err(format!("cannot change {} ({})", place, crate::net::reason(&std::io::Error::from_raw_os_error(code as i32)))),
    }
}

/// Adds, changes or removes the icon as SHOWN has it.
unsafe fn notify(hwnd: HWND, action: u32) -> bool {
    let Some(shown) = SHOWN.lock().unwrap_or_else(|e| e.into_inner()).clone() else { return false };
    let mut d = NOTIFYICONDATAW { cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32, hWnd: hwnd, uID: 1,
                                  uFlags: NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP, uCallbackMessage: CALLBACK, ..Default::default() };
    let stock = match shown.level {
        Level::Ok => IDI_INFORMATION,
        Level::Warning => IDI_WARNING,
        Level::Critical => IDI_ERROR,
        Level::Down => IDI_QUESTION,
    };
    d.hIcon = LoadIconW(std::ptr::null_mut(), stock);
    let tip: Vec<u16> = shown.tip(TIP_UNITS).encode_utf16().collect();
    d.szTip[..tip.len()].copy_from_slice(&tip); // at most 127 units: the terminator stays 0
    let ok = Shell_NotifyIconW(action, &d) != 0;
    if ok && action == NIM_ADD {
        d.Anonymous.uVersion = NOTIFYICON_VERSION_4; // NIN_SELECT and NIN_KEYSELECT, NIF_SHOWTIP's tooltip
        Shell_NotifyIconW(NIM_SETVERSION, &d);
    }
    ok
}

unsafe extern "system" fn window(hwnd: HWND, msg: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    match msg {
        CALLBACK => {
            let event = (l as u32) & 0xffff;
            if event == NIN_SELECT || event == NIN_KEYSELECT {
                if let Some(url) = URL.get() { open(url) }
            }
            0
        }
        CHANGED => {
            if !notify(hwnd, NIM_MODIFY) { // no icon to change (Explorer was not there yet): add it
                notify(hwnd, NIM_ADD);
            }
            0
        }
        WM_ENDSESSION if w != 0 => { // logging off, or Restart Manager closing it for an upgrade (it has a window)
            notify(hwnd, NIM_DELETE);
            std::process::exit(0)
        }
        WM_DESTROY => {
            notify(hwnd, NIM_DELETE);
            PostQuitMessage(0);
            0
        }
        m if m != 0 && m == TASKBAR_CREATED.load(Ordering::Relaxed) => {
            notify(hwnd, NIM_ADD);
            0
        }
        _ => DefWindowProcW(hwnd, msg, w, l),
    }
}

fn fail(text: &str) -> ! {
    attach_console();
    eprintln!("platmon-tray: {}", text);
    std::process::exit(1)
}

pub fn run(addr: &str) -> ! {
    let _ = URL.set(crate::url(addr));
    *SHOWN.lock().unwrap_or_else(|e| e.into_inner()) = Some(crate::waiting(addr));
    let class = wide("platmon-tray");
    // SAFETY: the window class and window are created once with NUL-terminated names that live to the end of
    // this function, which never returns; the message loop is this thread's.
    unsafe {
        let instance = GetModuleHandleW(std::ptr::null());
        let wc = WNDCLASSEXW { cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32, lpfnWndProc: Some(window), hInstance: instance,
                               lpszClassName: class.as_ptr(), ..Default::default() };
        if RegisterClassExW(&wc) == 0 {
            fail("cannot register the window class");
        }
        TASKBAR_CREATED.store(RegisterWindowMessageW(wide("TaskbarCreated").as_ptr()), Ordering::Relaxed);
        let hwnd = CreateWindowExW(WS_EX_TOOLWINDOW, class.as_ptr(), class.as_ptr(), WS_OVERLAPPED, 0, 0, 0, 0, std::ptr::null_mut(),
                                   std::ptr::null_mut(), instance, std::ptr::null());
        if hwnd.is_null() {
            fail("cannot create the window");
        }
        WINDOW.store(hwnd as usize, Ordering::Relaxed);
        notify(hwnd, NIM_ADD); // may fail before Explorer runs; TaskbarCreated adds it then
        let addr = addr.to_string();
        std::thread::spawn(move || crate::watch(&addr, |shown| {
            *SHOWN.lock().unwrap_or_else(|e| e.into_inner()) = Some(shown);
            PostMessageW(WINDOW.load(Ordering::Relaxed) as HWND, CHANGED, 0, 0);
        }));
        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
            DispatchMessageW(&msg);
        }
    }
    std::process::exit(0)
}

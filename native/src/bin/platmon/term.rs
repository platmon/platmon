//! The live view's terminal on Unix (cli.py's Terminal, interactive() and shutil.get_terminal_size()):
//! the alternate screen, a hidden cursor and keys without Enter, all restored on exit, Ctrl+C included.
//! Written for any Unix (termios, select, SIGWINCH), not Linux only.
use std::io::Write;
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

const ENTER: &str = "\x1b[?1049h\x1b[?25l";
const LEAVE: &str = "\x1b[0m\x1b[?25h\x1b[?1049l";
/// The resize pipe's write end, for the SIGWINCH handler; -1 outside the live view.
static WAKE: AtomicI32 = AtomicI32::new(-1);
/// The terminal's settings before the live view, for the SIGINT handler.
static SAVED: OnceLock<libc::termios> = OnceLock::new();

fn isatty(fd: i32) -> bool {
    // SAFETY: isatty only inspects the descriptor.
    unsafe { libc::isatty(fd) == 1 }
}

pub fn stdout_isatty() -> bool {
    isatty(1)
}

/// interactive(): keys and a screen to draw on, and a terminal that understands escapes.
pub fn interactive() -> bool {
    isatty(0) && isatty(1) && !matches!(std::env::var("TERM").unwrap_or_default().as_str(), "" | "dumb")
}

/// shutil.get_terminal_size(): COLUMNS and LINES when set, else stdout's size, else 80x24.
pub fn size() -> (usize, usize) {
    let env = |k: &str| std::env::var(k).ok().and_then(|v| v.trim().parse::<i64>().ok()).filter(|v| *v > 0).map(|v| v as usize);
    let (cols, lines) = (env("COLUMNS"), env("LINES"));
    if let (Some(c), Some(l)) = (cols, lines) {
        return (c, l);
    }
    // SAFETY: TIOCGWINSZ fills a winsize; on failure it stays zeroed.
    let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
    unsafe { libc::ioctl(1, libc::TIOCGWINSZ, &mut ws) };
    let or = |v: u16, d: usize| if v > 0 { v as usize } else { d };
    (cols.unwrap_or(or(ws.ws_col, 80)), lines.unwrap_or(or(ws.ws_row, 24)))
}

extern "C" fn resized(_: libc::c_int) {
    let fd = WAKE.load(Ordering::Relaxed);
    if fd >= 0 {
        // SAFETY: write is async-signal-safe; the pipe is non-blocking, a full one is already woken.
        unsafe { libc::write(fd, b"r".as_ptr().cast(), 1) };
    }
}

/// Ctrl+C: quiet, exit 0, as cli.py's KeyboardInterrupt; the terminal restored first if the live view
/// changed it (write and tcsetattr are async-signal-safe).
extern "C" fn interrupted(_: libc::c_int) {
    if let Some(saved) = SAVED.get() {
        // SAFETY: async-signal-safe calls only.
        unsafe {
            libc::write(1, LEAVE.as_ptr().cast(), LEAVE.len());
            libc::tcsetattr(0, libc::TCSADRAIN, saved);
        }
    }
    // SAFETY: _exit is async-signal-safe.
    unsafe { libc::_exit(0) }
}

pub fn exit_on_interrupt() {
    // SAFETY: installs a handler that only makes async-signal-safe calls.
    unsafe { libc::signal(libc::SIGINT, interrupted as extern "C" fn(libc::c_int) as libc::sighandler_t) };
}

pub enum Event {
    Tick,
    Resize,
    Keys(String),
}

pub struct Terminal {
    shown: Vec<String>,
    clear: bool,
    keys: bool,
    wake: [i32; 2],
    old_winch: libc::sighandler_t,
}

fn out(text: &str) {
    let mut o = std::io::stdout().lock();
    let _ = o.write_all(text.as_bytes()).and_then(|_| o.flush());
}

impl Terminal {
    pub fn enter() -> std::io::Result<Terminal> {
        // SAFETY: termios calls on stdin, which interactive() found to be a terminal; a pipe of our own.
        unsafe {
            let mut saved: libc::termios = std::mem::zeroed();
            if libc::tcgetattr(0, &mut saved) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            let _ = SAVED.set(saved);
            let mut cbreak = saved; // tty.setcbreak: no echo, no line editing; Ctrl+C still interrupts
            cbreak.c_lflag &= !(libc::ECHO | libc::ICANON);
            cbreak.c_cc[libc::VMIN] = 1;
            cbreak.c_cc[libc::VTIME] = 0;
            libc::tcsetattr(0, libc::TCSAFLUSH, &cbreak);
            let mut wake = [0i32; 2];
            if libc::pipe(wake.as_mut_ptr()) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            libc::fcntl(wake[1], libc::F_SETFL, libc::fcntl(wake[1], libc::F_GETFL) | libc::O_NONBLOCK);
            WAKE.store(wake[1], Ordering::Relaxed);
            let old_winch = libc::signal(libc::SIGWINCH, resized as extern "C" fn(libc::c_int) as libc::sighandler_t);
            out(ENTER);
            Ok(Terminal { shown: vec![], clear: true, keys: true, wake, old_winch })
        }
    }

    /// Rewrites only the lines that changed since the last frame.
    pub fn draw(&mut self, lines: Vec<String>) {
        let mut o = String::new();
        if self.clear { o.push_str("\x1b[H\x1b[2J") }
        for (n, line) in lines.iter().enumerate() {
            if self.clear || n >= self.shown.len() || self.shown[n] != *line {
                o.push_str(&format!("\x1b[{};1H{}\x1b[K", n + 1, line));
            }
        }
        if lines.len() < self.shown.len() {
            o.push_str(&format!("\x1b[{};1H\x1b[J", lines.len() + 1));
        }
        if !o.is_empty() { out(&o) }
        self.shown = lines;
        self.clear = false;
    }

    /// Sleeps until timeout (Tick), a resize (Resize) or a key press (the keys). No polling.
    pub fn wait(&mut self, timeout: f64) -> Event {
        let deadline = Instant::now() + Duration::try_from_secs_f64(timeout.max(0.0)).unwrap_or(Duration::from_secs(1 << 31));
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            // SAFETY: select on two descriptors of ours; fd_set and timeval zeroed and filled here.
            let (ready, set) = unsafe {
                let mut set: libc::fd_set = std::mem::zeroed();
                libc::FD_ZERO(&mut set);
                libc::FD_SET(self.wake[0], &mut set);
                if self.keys { libc::FD_SET(0, &mut set) }
                let mut tv = libc::timeval { tv_sec: left.as_secs().min(i32::MAX as u64) as _,
                                             tv_usec: left.subsec_micros() as _ };
                (libc::select(self.wake[0].max(0) + 1, &mut set, std::ptr::null_mut(), std::ptr::null_mut(), &mut tv), set)
            };
            if ready < 0 {
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted { continue }
                return Event::Tick;
            }
            let mut buf = [0u8; 64];
            // SAFETY: reads into a buffer of ours from descriptors select found ready.
            unsafe {
                if libc::FD_ISSET(self.wake[0], &set) {
                    libc::read(self.wake[0], buf.as_mut_ptr().cast(), 64);
                    self.clear = true;
                    return Event::Resize;
                }
                if self.keys && libc::FD_ISSET(0, &set) {
                    let n = libc::read(0, buf.as_mut_ptr().cast(), 32);
                    if n <= 0 { // stdin closed: stop watching it instead of waking at once forever
                        self.keys = false;
                        return Event::Keys(String::new());
                    }
                    return Event::Keys(String::from_utf8_lossy(&buf[..n as usize]).into_owned());
                }
            }
            return Event::Tick;
        }
    }
}

impl Drop for Terminal {
    fn drop(&mut self) {
        out(LEAVE);
        WAKE.store(-1, Ordering::Relaxed);
        // SAFETY: restores what enter() changed and closes its pipe.
        unsafe {
            libc::signal(libc::SIGWINCH, self.old_winch);
            if let Some(saved) = SAVED.get() { libc::tcsetattr(0, libc::TCSADRAIN, saved); }
            libc::close(self.wake[0]);
            libc::close(self.wake[1]);
        }
    }
}

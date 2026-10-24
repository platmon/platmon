//! What the service needs from the OS outside the collectors: the elapsed clocks, random bytes for the
//! instance id, the listening socket and the exit on a stop request. One module per OS, the same functions.
#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
pub use linux::*;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::*;

#[cfg(test)]
mod tests {
    #[test]
    fn the_elapsed_clocks_run_and_sleep_only_adds_to_boottime() {
        // monotonic first: read after it, boottime is never behind it, however close the reads
        let (m0, b0) = (super::monotonic_ns().unwrap(), super::boottime_ns().unwrap());
        std::thread::sleep(std::time::Duration::from_millis(20));
        let (m1, b1) = (super::monotonic_ns().unwrap(), super::boottime_ns().unwrap());
        assert!(b1 - b0 >= 15_000_000 && m1 - m0 >= 15_000_000, "{} {} {} {}", b0, b1, m0, m1);
        assert!(b1 >= m1, "boottime {} behind monotonic {}", b1, m1);
    }
}

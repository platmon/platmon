//! A size-class cache in front of the system allocator. musl's allocator returns empty groups to the kernel,
//! so a collection that allocates and frees the same small objects every second maps and unmaps pages every
//! time (strace -c on x86: ~60 mmap/munmap pairs per collection). Freed blocks up to 4 KiB are kept on a
//! free list per power-of-two class and reused; nothing is returned, so memory stays at the peak.
//! A product build would use a vetted allocator crate instead (mimalloc and jemalloc are MIT-licensed).
use std::alloc::{GlobalAlloc, Layout, System};
use std::ptr::null_mut;
use std::sync::atomic::{AtomicBool, Ordering};

const CLASSES: usize = 9; // 16 B .. 4 KiB
const MIN: usize = 16;

pub struct Cached {
    lock: AtomicBool,
    free: std::cell::UnsafeCell<[*mut u8; CLASSES]>,
}

unsafe impl Sync for Cached {}

impl Cached {
    pub const fn new() -> Cached {
        Cached { lock: AtomicBool::new(false), free: std::cell::UnsafeCell::new([null_mut(); CLASSES]) }
    }

    fn class(layout: &Layout) -> Option<usize> {
        if layout.align() > MIN || layout.size() > MIN << (CLASSES - 1) {
            return None;
        }
        let size = layout.size().max(MIN).next_power_of_two();
        Some(size.trailing_zeros() as usize - MIN.trailing_zeros() as usize)
    }

    fn locked<T>(&self, f: impl FnOnce(&mut [*mut u8; CLASSES]) -> T) -> T {
        while self.lock.compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed).is_err() {
            std::hint::spin_loop();
        }
        let out = f(unsafe { &mut *self.free.get() });
        self.lock.store(false, Ordering::Release);
        out
    }
}

unsafe impl GlobalAlloc for Cached {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        match Cached::class(&layout) {
            Some(c) => {
                let hit = self.locked(|free| {
                    let p = free[c];
                    if !p.is_null() {
                        free[c] = *(p as *mut *mut u8); // the next block, stored in the free block itself
                    }
                    p
                });
                if hit.is_null() { System.alloc(Layout::from_size_align_unchecked(MIN << c, MIN)) } else { hit }
            }
            None => System.alloc(layout),
        }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        match Cached::class(&layout) {
            Some(c) => self.locked(|free| {
                *(ptr as *mut *mut u8) = free[c];
                free[c] = ptr;
            }),
            None => System.dealloc(ptr, layout),
        }
    }
}

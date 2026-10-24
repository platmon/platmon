//! Python's str methods as the ports need them: isspace, split, strip and isprintable.

/// str.isspace() for one character.
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// str.split() without arguments.
pub fn py_split(s: &str) -> Vec<&str> {
    s.split(py_space).filter(|x| !x.is_empty()).collect()
}

/// str.strip() without arguments.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

/// str.isprintable() for one character. Exact for ASCII, the names the kernel and the collectors use.
/// Beyond ASCII, Python goes by its Unicode database (version-dependent: unassigned code points are not
/// printable); here controls, separators, the format characters listed below and private use are not
/// printable and every other character is.
pub fn py_printable(c: char) -> bool {
    if c.is_ascii() {
        return (' '..='~').contains(&c);
    }
    let format = matches!(c as u32, 0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x890..=0x891 | 0x8e2 | 0x180e
        | 0x200b..=0x200f | 0x202a..=0x202e | 0x2060..=0x2064 | 0x2066..=0x206f | 0xfeff | 0xfff9..=0xfffb
        | 0x110bd | 0x110cd | 0x13430..=0x1343f | 0x1bca0..=0x1bca3 | 0x1d173..=0x1d17a | 0xe0001 | 0xe0020..=0xe007f);
    let private = matches!(c as u32, 0xe000..=0xf8ff | 0xf0000..=0xffffd | 0x100000..=0x10fffd);
    !(c.is_control() || c.is_whitespace() || format || private)
}

# platmon brand assets

The `p_` symbol and lowercase `platmon` wordmark are the project's identity.
The description is **Lightweight platform monitor**.

These are the canonical SVGs used by the repository README and web viewer.
The SVGs contain paths and solid fills, with no external images, fonts or runtime dependencies.
They follow the repository's [Apache-2.0 license](../../../../LICENSE).

| Asset | Use |
| --- | --- |
| `platmon-logo-light.svg` | Horizontal logo on a light background |
| `platmon-logo-dark.svg` | Horizontal logo on a dark background |
| `platmon-icon-light.svg` | Standalone symbol on a light background |
| `platmon-icon-dark.svg` | Standalone symbol on a dark background |
| `favicon.svg` | Browser icon with its own dark background |
| `brand-colors.json` | Exact sRGB palette and identity metadata |

| Element | Light background | Dark background |
| --- | --- | --- |
| `p` | `#2F7D5B` | `#4FB286` |
| Cursor `_` | `#1D2127` | `#4FB286` |
| Wordmark | `#1D2127` | `#E6E8EB` |
| Recommended background | `#F6F7F9` | `#121417` |

Use the light and dark variants with their intended backgrounds. Keep the aspect ratio,
spacing and solid fills. The transparent logo SVGs already include clear space.
Use `favicon.svg` when a small icon needs to work against either page theme.

The README selects the logo and the web page's header the icon with `prefers-color-scheme`.
The HTTP server exposes only the two logo SVGs, the two icon SVGs and the favicon, and only when the web viewer
is enabled. The other files remain repository assets; they are not general-purpose HTTP routes.
The existing systemd installer and deploy/docker/Dockerfile include this directory with `frontends/`.

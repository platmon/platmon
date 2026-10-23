#!/usr/bin/env python3
"""Build the local image the package checks run in, so that they do not use the network themselves.

  python3 scripts/packaging/prepare_test_image.py ubuntu:24.04 platmon-package-test:local

An Ubuntu userspace with the OS tools the checks need (PACKAGES), installed once per job instead of in
each check's fresh container. A mirror in the middle of a sync answers with indexes of an unexpected
size or hash, which apt does not retry, and a stalled mirror can hang an install: so the update and
install run up to ATTEMPTS times, each within ATTEMPT_TIMEOUT seconds and PAUSE seconds apart, and a
failure stops here with apt's output instead of inside a check. The image stays on this host and is
never pushed (docs/guides/deployment.md#container-image-licensing).
"""
import argparse
import subprocess

PACKAGES = ("python3", "systemd", "init-system-helpers")
ATTEMPTS = 3
ATTEMPT_TIMEOUT = 300
PAUSE = 30


def dockerfile(base):
    install = ("apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
               + " ".join(PACKAGES))
    return (f"FROM {base}\n"
            f"RUN for attempt in $(seq {ATTEMPTS}); do "
            f"timeout {ATTEMPT_TIMEOUT} sh -c '{install}' && break; "
            f"if [ $attempt = {ATTEMPTS} ]; then echo 'apt failed {ATTEMPTS} times' >&2; exit 1; fi; "
            f"echo \"apt attempt $attempt failed; again in {PAUSE} s\" >&2; sleep {PAUSE}; "
            "done && rm -rf /var/lib/apt/lists/*\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", help="the Ubuntu image, e.g. ubuntu:24.04")
    parser.add_argument("tag", help="the local tag to build")
    args = parser.parse_args()
    subprocess.run(["docker", "build", "-t", args.tag, "-"], input=dockerfile(args.base), text=True,
                   check=True)


if __name__ == "__main__":
    main()

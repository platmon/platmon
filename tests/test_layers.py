"""Import direction between the layers (docs/development/architecture.md, "Layers"): collector reads, runtime
schedules and publishes, frontends are clients of the HTTP API. Checked on the source, nothing is imported."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = {"collector", "runtime", "frontends", "platmon", "benchmarks", "scripts", "tests"}
# The one way back: /text serves the terminal client's plain screen.
ALLOWED = {("runtime/server.py", "frontends.cli", "render")}


def imports(path):
    """(module, name) for every import in path; name is None for a plain `import module`."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from ((alias.name, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: resolve against the file's own package
                package = path.relative_to(ROOT).parent.parts
                base = package[:len(package) - node.level + 1]
                module = ".".join((*base, *filter(None, [node.module])))
            else:
                module = node.module
            yield from ((module, alias.name) for alias in node.names)


def sources(directory):
    return sorted((ROOT / directory).rglob("*.py"))


def violations(directory, forbidden):
    found = []
    for path in sources(directory):
        rel = path.relative_to(ROOT).as_posix()
        for module, name in imports(path):
            if module.split(".")[0] in forbidden and (rel, module, name) not in ALLOWED:
                found.append(f"{rel}: {module}" + (f".{name}" if name else ""))
    return found


@pytest.mark.parametrize("directory, forbidden", [
    ("collector", {"runtime", "frontends", "platmon"}),
    ("runtime", {"frontends", "platmon"}),
])
def test_lower_layers_do_not_import_upper_ones(directory, forbidden):
    assert violations(directory, forbidden) == []


def test_cli_imports_nothing_from_the_project():
    """scripts/install-cli.sh copies frontends/cli.py alone onto machines without the service."""
    assert violations("frontends", PROJECT - {"frontends"}) == []
    assert [m for m, _ in imports(ROOT / "frontends/cli.py") if m.split(".")[0] in PROJECT] == []


def test_the_allowed_exception_is_still_used():
    """Drop it from ALLOWED once /text no longer needs the terminal client's renderer."""
    for rel, module, name in ALLOWED:
        assert (module, name) in set(imports(ROOT / rel))


def test_cli_version_copy_matches_the_service():
    from frontends import cli
    from runtime import VERSION
    assert cli.VERSION == VERSION

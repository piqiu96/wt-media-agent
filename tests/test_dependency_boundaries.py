"""ADR-0016 dependency boundaries, checked with the standard `ast` module (T-05).

The point of this file is that the boundary stops being a convention. Before it,
"executors reach the outside world only through clients/services" and "only
`runtime/config.py` reads the environment" were true statements that nothing
enforced: a new `os.getenv` in an executor would have looked exactly like the
five that were removed in T-04.

Each rule below is a pure function from `{relative path: source}` to a list of
violation strings. `BoundaryRuleControls` runs the same functions against
deliberately broken sources, so "the rule found nothing" cannot be a rule that
is incapable of finding anything. The rules the plan names:

- R1  every module belongs to a known layer, a declared placeholder or the root
- R2  imports only point down the ADR-0016 §1 order (single-file exceptions: D-06)
- R3  the three edges ADR-0016 §3 names are reported as such
- R4  `executors/**` does not import the capabilities it must get from services
- R5  `executors/**` does not build clients, read config or reach for `_post`
- R6  only `runtime/config.py` touches environment variables
- R7  nothing under `src/` names the release configuration directory
- R8  the frozen paths, symbols and console scripts are still there
- R9  `executors/**` and `local_api/**` contain no URL literals
- R10 the observed layer-to-layer edge set is frozen (a ratchet)

What this file cannot check, and where those live instead:

- modules dispatched at runtime (`getattr`, `importlib`, string-built names);
  the import graph only sees names that are written down
- hard-coded *values* inside `clients/` (timeouts, ports, hosts) -- R9 governs
  where URLs may appear, not which ones
- anything outside Python: `pyproject.toml` dependencies, `scripts/*.sh`,
  `tauri.conf.json`, Rust. R8 reaches into `pyproject.toml`'s scripts and the
  sidecar build script by literal string, and no further
- erosion *inside* a layer: two files of the same layer may import each other
  freely, and a helper that belongs in `services/` may quietly live in
  `clients/` without changing a single edge
- `Path` and filesystem literals, and module paths that appear only inside
  strings (R7 is the one string rule, and it is a substring match)
- `from pkg import mod` is recorded as `pkg`, since the statement binds a
  submodule of the imported name rather than the dotted path. The rules are
  conservative about this (they report the parent, which can be a false alarm
  for an exception and never a blind spot), and the narrow spelling
  `from pkg.mod import X` is what the real tree uses
- R9 sees string constants; a URL assembled at runtime (`"".join(...)`) passes
- R10 is layer-to-layer, so moving an import from one module of a layer to
  another module of the same layer keeps it green
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SOURCE_ROOT = ROOT / "src" / "wt_media_agent"
PACKAGE = "wt_media_agent"

#: ADR-0016 §2's nine layers.
KNOWN_LAYERS = frozenset(
    {
        "bootstrap",
        "runtime",
        "runner",
        "executors",
        "clients",
        "services",
        "storage",
        "local_api",
        "utils",
    }
)

#: Declared empty. `adapters/` was emptied when M3 dropped discovery and is the
#: only one left: `modes/` and `generated/` no longer exist — the architecture
#: baseline §5.2 never kept either of them, so the directories were the side that
#: was out of step; `platforms/` was revoked by ADR-0016 §5 and has never existed.
PLACEHOLDER_PACKAGES = frozenset({"adapters"})

#: Modules that sit at the package root instead of in a layer: the package face,
#: the two re-export shims (`tests/test_runner_session.py` imports their old
#: paths and must not change) and the three process entries.
ROOT_MODULES = frozenset(
    {
        "__init__",
        "cloud_agent_client",
        "cloud_agent_contract",
        "cloud_main",
        "local_main",
        "sidecar_main",
    }
)

#: ADR-0016 §1: `bootstrap -> runner -> executors -> {clients, services, storage}`.
#: `runtime/` is cross-cutting -- referenced by every layer, referencing none --
#: so it gets an empty set here. Root modules are absent on purpose: they are the
#: package's outside face and their edges are frozen by R10.
DOWNWARD_EDGES: dict[str, frozenset[str]] = {
    "bootstrap": KNOWN_LAYERS,
    "runner": frozenset({"executors", "clients", "services", "storage", "runtime", "utils"}),
    "executors": frozenset({"clients", "services", "storage", "runtime", "utils"}),
    "local_api": frozenset({"clients", "services", "storage", "runtime", "utils"}),
    "clients": frozenset({"services", "storage", "runtime", "utils"}),
    "services": frozenset({"clients", "storage", "runtime", "utils"}),
    "storage": frozenset({"runtime", "utils"}),
    "utils": frozenset(),
    "runtime": frozenset(),
}

#: The two edges that cross the descent order, each narrowed to one file (D-06).
#: A target is allowed if it is the named module or below it -- importing
#: `clients.bitbrowser.errors` is the same fact as importing the package.
SINGLE_FILE_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    f"{PACKAGE}/runtime/environment.py": (f"{PACKAGE}.clients.bitbrowser",),
    f"{PACKAGE}/local_api/server.py": (f"{PACKAGE}.bootstrap.app",),
}

#: ADR-0016 §3's explicit forbiddens, kept separate from R2 so that a failure
#: names the clause rather than the whitelist entry it happened to miss.
FORBIDDEN_BY_ADR_3: dict[tuple[str, str], str] = {
    ("clients", "executors"): "clients -> executors",
    ("services", "executors"): "services -> executors",
}
BUSINESS_LAYERS = frozenset(
    {"bootstrap", "runner", "executors", "clients", "services", "storage", "local_api"}
)

#: ADR-0016 §3: executors get external access from clients/services/storage.
#: `http` is denied whole because the clause names `http.client`.
EXECUTOR_DENIED_MODULES = frozenset(
    {"sqlite3", "subprocess", "socket", "http", "urllib", "requests", "ctypes", "os"}
)

#: The only places a client may be constructed. `clients/` owns the factory;
#: `bootstrap/app.py` is the one production assembly site (ADR-0016 §2).
CLIENT_CONSTRUCTION_ALLOWED = {
    "BitBrowserClient": (f"{PACKAGE}/clients/bitbrowser/",),
    "CloudAgentClient": (f"{PACKAGE}/clients/cloud/",),
}
CONSTRUCTION_ALLOWED_ANYWHERE = (f"{PACKAGE}/bootstrap/app.py",)

ENVIRONMENT_NAMES = frozenset({"environ", "environb", "getenv", "putenv"})

#: R8: the paths and symbols other repositories' code and scripts address.
FROZEN_DEFINITIONS: dict[str, tuple[str, ...]] = {
    f"{PACKAGE}/sidecar_main.py": ("main", "local_api_server"),
    f"{PACKAGE}/local_api/server.py": ("main",),
    f"{PACKAGE}/storage/migration.py": (
        "DEFAULT_DB_NAME",
        "MIGRATIONS",
        "default_data_dir",
        "default_db_path",
        "apply_migrations",
        "main",
    ),
}

FROZEN_CONSOLE_SCRIPTS = {
    "wt-media-local-agent": "wt_media_agent.local_main:main",
    "wt-media-cloud-agent": "wt_media_agent.cloud_main:main",
    "wt-media-local-health": "wt_media_agent.local_api.server:main",
    "wt-media-agent-storage-migrate": "wt_media_agent.storage.migration:main",
}

#: R10: the layer-to-layer edges as observed at T-05. Any new pair fails.
FROZEN_LAYER_EDGES = frozenset(
    {
        ("<package>", "runtime"),
        ("bootstrap", "clients"),
        ("bootstrap", "local_api"),
        ("bootstrap", "runner"),
        ("bootstrap", "runtime"),
        ("bootstrap", "storage"),
        ("clients", "runtime"),
        ("clients", "services"),
        ("cloud_agent_client", "clients"),
        ("cloud_agent_contract", "clients"),
        ("cloud_main", "bootstrap"),
        ("executors", "clients"),
        ("executors", "services"),
        ("local_api", "bootstrap"),
        ("local_api", "clients"),
        ("local_api", "runtime"),
        ("local_api", "services"),
        ("local_api", "storage"),
        ("local_main", "bootstrap"),
        ("runner", "clients"),
        ("runner", "executors"),
        ("runner", "runtime"),
        ("runner", "storage"),
        ("runner", "utils"),
        ("runtime", "clients"),
        ("sidecar_main", "bootstrap"),
        ("sidecar_main", "local_api"),
        ("storage", "runtime"),
        ("storage", "utils"),
    }
)

URL_SCHEMES = ("http://", "https://")
URL_LITERAL_LAYERS = frozenset({"executors", "local_api"})


# --- the scan -----------------------------------------------------------------


class Edge:
    """One `import` statement's target, with the line that wrote it."""

    __slots__ = ("lineno", "source", "target")

    def __init__(self, lineno: int, source: str, target: str) -> None:
        self.lineno = lineno
        self.source = source  # relative path, e.g. "wt_media_agent/executors/noop.py"
        self.target = target  # absolute module name, e.g. "wt_media_agent.clients.cloud"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.source}:{self.lineno} -> {self.target}"


def package_files() -> dict[str, str]:
    """Every module in the package, as `{relative path: source}`."""
    files = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        files[str(path.relative_to(SOURCE_ROOT.parent))] = path.read_text(encoding="utf-8")
    return files


def module_name_of(relpath: str) -> str:
    """`wt_media_agent/clients/cloud/client.py` -> `wt_media_agent.clients.cloud.client`."""
    parts = list(Path(relpath).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def layer_of(module: str) -> str:
    """The layer of a module: its second dotted component, always.

    `wt_media_agent.clients.cloud.client` -> `clients` (the layer is what the
    descent order talks about, not the file), `wt_media_agent.cloud_main` ->
    `cloud_main` (a root module is its own pseudo-layer), `wt_media_agent` ->
    `<package>`. Rule R1 is what insists the second component be a known one,
    working from paths rather than from this function.
    """
    parts = module.split(".")
    if len(parts) == 1:
        return "<package>"
    return parts[1]


def imports_of(relpath: str, source: str) -> list[Edge]:
    """Every `wt_media_agent.*` import, with relative imports resolved."""
    module = module_name_of(relpath)
    is_package = Path(relpath).name == "__init__.py"
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    edges: list[Edge] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    edges.append(Edge(node.lineno, relpath, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            elif node.module:
                target = node.module
            else:
                continue
            if target == PACKAGE or target.startswith(PACKAGE + "."):
                edges.append(Edge(node.lineno, relpath, target))
    return edges


def all_edges(files: dict[str, str]) -> list[Edge]:
    edges: list[Edge] = []
    for relpath, source in files.items():
        edges.extend(imports_of(relpath, source))
    return edges


def imported_roots(source: str) -> list[tuple[int, str]]:
    """Top-level module names imported by a file, with the line."""
    roots: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.append((node.lineno, node.module.split(".")[0]))
    return roots


def imported_names(source: str) -> set[str]:
    """Every name bound by an `import`/`from ... import` statement."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def calls_in(source: str) -> list[tuple[int, str, str]]:
    """Every call, as `(line, called name, receiver)`.

    `receiver` is the text before the dot (`"bitbrowser"` in
    `bitbrowser._post(...)`), or `""` for a plain function call. Rules that
    care about *whose* private method was called need it: `self._helper()` is
    an executor's own business, `bitbrowser._post()` is another layer's.
    """
    calls: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            calls.append((node.lineno, func.id, ""))
        elif isinstance(func, ast.Attribute):
            calls.append((node.lineno, func.attr, ast.unparse(func.value)))
    return calls


def string_constants(source: str) -> list[tuple[int, str]]:
    """String literals, excluding docstrings (the code must be able to explain itself)."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def defined_names(source: str) -> set[str]:
    """Names bound at module level: definitions, assignments and imports.

    Imports count because that is the question the frozen surface asks -- "is
    this name in the module's namespace?" `sidecar_main.local_api_server` is an
    alias introduced by `from ... import server as local_api_server`, and a
    patch target or a `getattr` finds it exactly as it would a `def`.
    """
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def environment_access(source: str) -> list[tuple[int, str]]:
    """Uses of the environment, as `(line, name)`, including aliased imports."""
    module_aliases = set()
    name_aliases = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    module_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in ENVIRONMENT_NAMES:
                    name_aliases.add(alias.asname or alias.name)

    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ENVIRONMENT_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.Name) and node.id in name_aliases:
            found.append((node.lineno, node.id))
    return found


# --- the rules ----------------------------------------------------------------


def r1_unknown_modules(files: dict[str, str]) -> list[str]:
    violations = []
    for relpath in sorted(files):
        parts = Path(relpath).parts
        if len(parts) < 3:  # wt_media_agent/<module>.py
            if Path(relpath).stem not in ROOT_MODULES:
                violations.append(f"R1 {relpath}: not a registered root module")
            continue
        head = parts[1]
        if head not in KNOWN_LAYERS and head not in PLACEHOLDER_PACKAGES:
            violations.append(f"R1 {relpath}: '{head}' is not a known layer (ADR-0016 §2)")
    for name in sorted(PLACEHOLDER_PACKAGES):
        directory = SOURCE_ROOT / name
        contents = sorted(p.name for p in directory.glob("*.py"))
        if contents != ["__init__.py"]:
            violations.append(f"R1 {PACKAGE}/{name}/ must stay a placeholder, found {contents}")
        source = files.get(f"{PACKAGE}/{name}/__init__.py", "")
        if imported_roots(source) or calls_in(source):
            violations.append(f"R1 {PACKAGE}/{name}/__init__.py: a placeholder holds no code")
    return violations


def r2_wrong_direction(files: dict[str, str]) -> list[str]:
    violations = []
    for edge in all_edges(files):
        source_layer = layer_of(module_name_of(edge.source))
        target_layer = layer_of(edge.target)
        if source_layer not in DOWNWARD_EDGES:
            continue  # root modules: frozen by R10 instead
        if source_layer == target_layer or target_layer in DOWNWARD_EDGES[source_layer]:
            continue
        allowed = SINGLE_FILE_EXCEPTIONS.get(edge.source, ())
        if any(edge.target == prefix or edge.target.startswith(prefix + ".") for prefix in allowed):
            continue
        violations.append(
            f"R2 {edge.source}:{edge.lineno}: {source_layer} -> {target_layer} "
            f"({edge.target}) is not an allowed direction (ADR-0016 §1)"
        )
    return violations


def r3_adr_forbidden(files: dict[str, str]) -> list[str]:
    violations = []
    for edge in all_edges(files):
        source_layer = layer_of(module_name_of(edge.source))
        target_layer = layer_of(edge.target)
        pair = (source_layer, target_layer)
        if pair in FORBIDDEN_BY_ADR_3:
            violations.append(f"R3 {edge.source}:{edge.lineno}: {FORBIDDEN_BY_ADR_3[pair]} (ADR-0016 §3)")
        elif source_layer == "utils" and target_layer in BUSINESS_LAYERS:
            violations.append(
                f"R3 {edge.source}:{edge.lineno}: utils -> {target_layer} (ADR-0016 §3: "
                "utils holds pure functions)"
            )
    return violations


def r4_executor_capabilities(files: dict[str, str]) -> list[str]:
    violations = []
    for relpath in sorted(files):
        if Path(relpath).parts[1:2] != ("executors",):
            continue
        for lineno, root in imported_roots(files[relpath]):
            if root in EXECUTOR_DENIED_MODULES:
                violations.append(
                    f"R4 {relpath}:{lineno}: executors must not import {root!r} "
                    "(ADR-0016 §3: via clients/services/storage)"
                )
    return violations


def r5_executor_seams(files: dict[str, str]) -> list[str]:
    violations = []
    for relpath in sorted(files):
        source = files[relpath]
        in_executors = Path(relpath).parts[1:2] == ("executors",)
        for lineno, name, _ in calls_in(source):
            allowed = CLIENT_CONSTRUCTION_ALLOWED.get(name)
            if allowed is None:
                continue
            if relpath.startswith(allowed) or relpath.startswith(CONSTRUCTION_ALLOWED_ANYWHERE):
                continue
            violations.append(
                f"R5 {relpath}:{lineno}: {name}(...) outside its own package and "
                "bootstrap/app.py (ADR-0016 §2: one assembly site)"
            )
        if not in_executors:
            continue
        if f"{PACKAGE}.runtime.config" in {edge.target for edge in imports_of(relpath, source)}:
            violations.append(
                f"R5 {relpath}: an executor must not read the configuration; it "
                "receives its client and agent id (T-04)"
            )
        for lineno, name, receiver in calls_in(source):
            # `self._helper()` is the executor's own business; a private method
            # on anything else is a layer reaching into another layer's
            # internals, which is what T-05 removed from this file's callers.
            # `_post` is skipped here and caught by the check below, so that one
            # method never produces two lines for the same call.
            if name.startswith("_") and name != "_post" and receiver not in ("self", "cls", ""):
                violations.append(
                    f"R5 {relpath}:{lineno}: executor calls {receiver}.{name}(), a private "
                    "method of another layer (T-05: clients expose open_url)"
                )
    for relpath in sorted(files):
        if relpath.startswith(f"{PACKAGE}/clients/bitbrowser/"):
            continue
        for lineno, name, _ in calls_in(files[relpath]):
            if name == "_post":
                violations.append(
                    f"R5 {relpath}:{lineno}: calls BitBrowserClient._post outside "
                    "clients/bitbrowser/ (T-05)"
                )
    return violations


def r6_environment_access(files: dict[str, str]) -> list[str]:
    violations = []
    for relpath in sorted(files):
        if relpath == f"{PACKAGE}/runtime/config.py":
            continue
        for lineno, name in environment_access(files[relpath]):
            violations.append(
                f"R6 {relpath}:{lineno}: reads the environment ({name}); only "
                "runtime/config.py may (ADR-0016 §8)"
            )
    return violations


def r7_release_config_reference(files: dict[str, str]) -> list[str]:
    violations = []
    for relpath in sorted(files):
        for lineno, value in string_constants(files[relpath]):
            if "config_online" in value:
                violations.append(
                    f"R7 {relpath}:{lineno}: names the release configuration "
                    "directory (ADR-0016 §6: runtime reads config/, never config_online/)"
                )
    return violations


def r8_frozen_surface(files: dict[str, str], pyproject: dict[str, object]) -> list[str]:
    violations = []
    for relpath, names in sorted(FROZEN_DEFINITIONS.items()):
        source = files.get(relpath)
        if source is None:
            violations.append(f"R8 {relpath}: frozen module is gone")
            continue
        defined = defined_names(source)
        for name in names:
            if name not in defined:
                violations.append(f"R8 {relpath}: frozen symbol {name!r} is gone")
    scripts = (pyproject.get("project") or {}).get("scripts") or {}
    for name, target in sorted(FROZEN_CONSOLE_SCRIPTS.items()):
        if scripts.get(name) != target:
            violations.append(f"R8 console script {name!r} is {scripts.get(name)!r}, expected {target!r}")
    build_script = (ROOT / "scripts" / "build_desktop_sidecar.py").read_text(encoding="utf-8")
    if "wt_media_agent\" / \"sidecar_main.py" not in build_script:
        violations.append(
            "R8 scripts/build_desktop_sidecar.py no longer passes sidecar_main.py to "
            "PyInstaller (the path is the packaged entry point)"
        )
    return violations


def r9_url_literals(files: dict[str, str]) -> list[str]:
    violations = []
    for relpath in sorted(files):
        parts = Path(relpath).parts
        if len(parts) < 3 or parts[1] not in URL_LITERAL_LAYERS:
            continue
        for lineno, value in string_constants(files[relpath]):
            if value.startswith(URL_SCHEMES):
                violations.append(
                    f"R9 {relpath}:{lineno}: URL literal {value!r}; destinations live "
                    "in clients/ (ADR-0016 §2/§3)"
                )
    return violations


def r10_layer_edges(files: dict[str, str]) -> list[str]:
    observed = set()
    for edge in all_edges(files):
        source_layer = layer_of(module_name_of(edge.source))
        target_layer = layer_of(edge.target)
        if source_layer == target_layer and source_layer in KNOWN_LAYERS:
            continue  # inside one layer: not a boundary crossing (see the header)
        observed.add((source_layer, target_layer))
    violations = [f"R10 new cross-layer edge {a} -> {b}" for a, b in sorted(observed - FROZEN_LAYER_EDGES)]
    violations += [f"R10 frozen edge {a} -> {b} is gone" for a, b in sorted(FROZEN_LAYER_EDGES - observed)]
    return violations


# --- the assertions -----------------------------------------------------------


class BoundaryScanTests(unittest.TestCase):
    """The real tree. Each rule is one assertion so a failure names its rule."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = package_files()
        cls.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_the_scan_sees_the_whole_package(self):
        """Denominator first: a rule with nothing to look at proves nothing.

        The layer set is asserted exactly, so a module that fell outside the
        scan (a new top-level directory, a file the glob skipped) shows up here
        rather than as a rule that quietly found nothing to check.
        """
        self.assertEqual(len(self.files), len(list(SOURCE_ROOT.rglob("*.py"))))
        self.assertGreaterEqual(len(self.files), 60)
        layers = {layer_of(module_name_of(path)) for path in self.files}
        self.assertTrue(KNOWN_LAYERS.issubset(layers))
        self.assertIn("<package>", layers)
        self.assertIn("sidecar_main", layers)

    def test_r1_every_module_is_in_a_known_place(self):
        self.assertEqual(r1_unknown_modules(self.files), [])

    def test_r2_imports_only_point_down_the_descent_order(self):
        self.assertEqual(r2_wrong_direction(self.files), [])

    def test_r3_the_edges_adr_0016_names_are_absent(self):
        self.assertEqual(r3_adr_forbidden(self.files), [])

    def test_r4_executors_do_not_import_capabilities_themselves(self):
        self.assertEqual(r4_executor_capabilities(self.files), [])

    def test_r5_executors_have_no_construction_config_or_private_seams(self):
        self.assertEqual(r5_executor_seams(self.files), [])

    def test_r6_only_the_config_module_reads_the_environment(self):
        self.assertEqual(r6_environment_access(self.files), [])

    def test_r7_nothing_names_the_release_configuration_directory(self):
        self.assertEqual(r7_release_config_reference(self.files), [])

    def test_r8_the_frozen_paths_symbols_and_scripts_are_intact(self):
        self.assertEqual(r8_frozen_surface(self.files, self.pyproject), [])

    def test_r9_no_url_literals_in_executors_or_the_control_plane(self):
        self.assertEqual(r9_url_literals(self.files), [])

    def test_r10_no_new_cross_layer_edge(self):
        self.assertEqual(r10_layer_edges(self.files), [])


class BoundaryRuleControls(unittest.TestCase):
    """Controls: each rule is shown rejecting a source that breaks it.

    Without these, every assertion above would also pass if the rule returned
    `[]` unconditionally -- which is the failure mode this whole file exists to
    prevent. Each control plants one violation in a copy of the real tree and
    requires the rule to name the file it was planted in.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = package_files()
        cls.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def plant(self, relpath: str, addition: str) -> dict[str, str]:
        files = dict(self.files)
        files[relpath] = files[relpath] + addition
        return files

    def test_r2_control(self):
        files = self.plant(f"{PACKAGE}/clients/cloud/client.py", f"\nfrom {PACKAGE}.executors import noop\n")
        violations = r2_wrong_direction(files)
        self.assertTrue(any("clients/cloud/client.py" in v for v in violations), violations)

    def test_r2_control_for_a_relative_import(self):
        """Relative imports are resolved, not skipped.

        `from ...executors import noop` is spelled differently from the absolute
        import above and means the same thing; a scanner that only followed
        absolute names would let it through.
        """
        files = self.plant(f"{PACKAGE}/clients/cloud/client.py", "\nfrom ...executors import noop\n")
        violations = r2_wrong_direction(files)
        self.assertTrue(any("clients -> executors" in v for v in violations), violations)

    def test_r2_keeps_the_documented_exceptions_narrow(self):
        """D-06 allows one file per exception, not the whole layer.

        Two controls: another `runtime/` module may not reach `clients/` at all,
        and `runtime/environment.py` may reach `clients.bitbrowser` but not a
        neighbouring client module. Without these, widening an exception to a
        whole layer would look exactly like the frozen state.
        """
        neighbour = self.plant(f"{PACKAGE}/runtime/paths.py", f"\nfrom {PACKAGE}.clients import bitbrowser\n")
        violations = r2_wrong_direction(neighbour)
        self.assertTrue(any("runtime/paths.py" in v and "runtime -> clients" in v for v in violations), violations)

        other_client = self.plant(f"{PACKAGE}/runtime/environment.py", f"\nfrom {PACKAGE}.clients import cloud\n")
        violations = r2_wrong_direction(other_client)
        self.assertTrue(
            any("runtime/environment.py" in v and "runtime -> clients" in v for v in violations), violations
        )

    def test_r2_control_for_an_import_that_names_only_the_package(self):
        """`from pkg import mod` is recorded as `pkg` -- conservative, not blind.

        The rule cannot tell which submodule was bound, so it reports the
        parent. For the D-06 exceptions that means the narrow form
        (`from ...clients.bitbrowser import X`) is what stays allowed, which the
        real tree uses; the wide form fails rather than passing unnoticed.
        """
        real = self.files[f"{PACKAGE}/runtime/environment.py"]
        self.assertIn(f"from {PACKAGE}.clients.bitbrowser import", real)

    def test_r3_control(self):
        files = self.plant(f"{PACKAGE}/clients/cloud/client.py", f"\nfrom {PACKAGE}.executors import noop\n")
        violations = r3_adr_forbidden(files)
        self.assertTrue(any("clients -> executors" in v for v in violations), violations)

    def test_r3_control_for_a_utils_helper(self):
        files = self.plant(f"{PACKAGE}/utils/time.py", f"\nfrom {PACKAGE}.clients import platform_urls\n")
        violations = r3_adr_forbidden(files)
        self.assertTrue(any("utils -> clients" in v for v in violations), violations)

    def test_r4_control(self):
        files = self.plant(f"{PACKAGE}/executors/noop.py", "\nimport sqlite3\n")
        self.assertTrue(any("sqlite3" in v for v in r4_executor_capabilities(files)))

    def test_r5_control_for_a_self_built_client(self):
        files = self.plant(
            f"{PACKAGE}/executors/noop.py",
            f"\nfrom {PACKAGE}.clients.bitbrowser import BitBrowserClient\n"
            "\n\ndef build():\n    return BitBrowserClient('http://127.0.0.1:54345')\n",
        )
        violations = r5_executor_seams(files)
        self.assertTrue(any("BitBrowserClient" in v for v in violations), violations)

    def test_r5_control_for_a_private_call(self):
        files = self.plant(f"{PACKAGE}/executors/noop.py", "\n\ndef go(bitbrowser):\n    bitbrowser._post('/x', {})\n")
        violations = r5_executor_seams(files)
        self.assertTrue(any("_post" in v for v in violations), violations)

    def test_r6_control(self):
        files = self.plant(f"{PACKAGE}/bootstrap/local.py", "\nimport os\n\n\ndef port():\n    return os.environ['X']\n")
        violations = r6_environment_access(files)
        self.assertTrue(any("bootstrap/local.py" in v for v in violations), violations)

    def test_r6_control_for_an_aliased_import(self):
        """`from os import getenv` is the spelling that dodges a grep."""
        files = self.plant(
            f"{PACKAGE}/bootstrap/local.py",
            "\nfrom os import getenv\n\n\ndef port():\n    return getenv('X')\n",
        )
        violations = r6_environment_access(files)
        self.assertTrue(any("bootstrap/local.py" in v for v in violations), violations)

    def test_r7_control(self):
        files = self.plant(f"{PACKAGE}/runtime/paths.py", '\nONLINE = "config_online"\n')
        violations = r7_release_config_reference(files)
        self.assertTrue(any("runtime/paths.py" in v for v in violations), violations)

    def test_r8_control(self):
        files = dict(self.files)
        files[f"{PACKAGE}/local_api/server.py"] = files[f"{PACKAGE}/local_api/server.py"].replace(
            "def main(", "def entry_point(", 1
        )
        self.assertTrue(any("main" in v for v in r8_frozen_surface(files, self.pyproject)))

    def test_r8_control_for_a_console_script(self):
        scripts = {"project": {"scripts": dict(FROZEN_CONSOLE_SCRIPTS, **{"wt-media-local-agent": "x:y"})}}
        self.assertTrue(any("wt-media-local-agent" in v for v in r8_frozen_surface(self.files, scripts)))

    def test_r9_control(self):
        files = self.plant(f"{PACKAGE}/executors/noop.py", '\nHOME = "https://www.example.com/"\n')
        self.assertTrue(any("executors/noop.py" in v for v in r9_url_literals(files)))

    def test_r10_control(self):
        files = self.plant(f"{PACKAGE}/storage/sqlite.py", f"\nfrom {PACKAGE}.clients import platform_urls\n")
        self.assertTrue(any("storage -> clients" in v for v in r10_layer_edges(files)))

    def test_r1_control(self):
        files = dict(self.files)
        files[f"{PACKAGE}/runtimes/bitbrowser.py"] = '"""Bring back the old directory."""\n'
        self.assertTrue(any("runtimes" in v for v in r1_unknown_modules(files)))

    def test_r1_control_for_a_placeholder_that_grew_code(self):
        files = dict(self.files)
        files[f"{PACKAGE}/adapters/__init__.py"] = '"""Placeholder."""\n\nimport os\n'
        self.assertTrue(any("adapters" in v for v in r1_unknown_modules(files)))


if __name__ == "__main__":
    unittest.main()

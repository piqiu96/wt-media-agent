"""A contract README's two claims must still be true of the files beside it.

Each area README makes two checkable claims: which file is the formal
definition, and which revision that definition is at. Both rot silently -- a
`revision` written in prose keeps reading as authoritative long after the YAML
has been bumped, and nothing in the suite noticed. Three of the four areas were
stale when this module was written.

The revision is read from the definitions themselves (`revision:` in the
schema/error-code files, `info.version` in the OpenAPI one), so the YAML stays
the single source and a bump is a one-file change. That reading is deliberately
dependency-free: this package declares no runtime dependencies, and PyYAML is
not one of them.

Only this repository is read. The contract areas, their READMEs and their
`v1/*.yaml` files live together, so nothing here needs the cross-repo workspace
to be checked out.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = ROOT / "contracts"

#: `revision: "2026.07.14.6"` and the OpenAPI `version: 2026.09.24.1`. Both sit
#: on their own line, and no other key in these files holds a dotted revision.
DEFINITION_REVISION = re.compile(
    r'^[ \t]*(?:revision|version):[ \t]*"?(\d+(?:\.\d+)+)"?[ \t]*$', re.MULTILINE
)

#: A revision a README states in prose: "…, revision `2026.09.06.1`."
STATED_REVISION = re.compile(r"revision[\s:]+`?(\d+(?:\.\d+)+)`?")

#: A formal-definition filename a README names, e.g. `v1/status.yaml`.
NAMED_DEFINITION = re.compile(r"`(v1/[A-Za-z0-9_.-]+\.yaml)`")

#: An endpoint a README lists as held by the definition, e.g. `GET /healthz`.
STATED_ENDPOINT = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/\S*?)`")

#: A path key in an OpenAPI `paths:` mapping, at the two-space indent it uses.
DEFINED_PATH = re.compile(r"^  (/[^\s:]*):", re.MULTILINE)


def contract_areas() -> list[Path]:
    """Every area directory that carries formal definitions."""
    return sorted(
        area
        for area in CONTRACTS_ROOT.iterdir()
        if area.is_dir() and any((area / "v1").glob("*.yaml"))
    )


def definitions(area: Path) -> list[Path]:
    return sorted((area / "v1").glob("*.yaml"))


def definition_revisions(paths: list[Path]) -> set[str]:
    found: set[str] = set()
    for path in paths:
        found.update(DEFINITION_REVISION.findall(path.read_text(encoding="utf-8")))
    return found


def stated_revisions(text: str) -> list[str]:
    return STATED_REVISION.findall(text)


def defined_paths(paths: list[Path]) -> set[str]:
    """The `paths:` keys the area's definitions declare."""
    found: set[str] = set()
    for path in paths:
        found.update(DEFINED_PATH.findall(path.read_text(encoding="utf-8")))
    return found


def dangling_revisions(text: str, revisions: set[str]) -> list[str]:
    """The revisions a README states that no definition in its area carries."""
    return [value for value in stated_revisions(text) if value not in revisions]


class ContractDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.areas = contract_areas()
        cls.readme = {
            area: (area / "README.md").read_text(encoding="utf-8") for area in cls.areas
        }
        cls.revisions = {
            area: definition_revisions(definitions(area)) for area in cls.areas
        }
        cls.paths = {area: defined_paths(definitions(area)) for area in cls.areas}

    def test_every_stale_area_is_covered(self) -> None:
        """Report the denominator: these assertions mean nothing over an empty set."""
        self.assertGreaterEqual(len(self.areas), 4)

        for area in self.areas:
            self.assertTrue(definitions(area), f"{area.name}: no v1/*.yaml")
            self.assertTrue(self.revisions[area], f"{area.name}: no revision found")
            self.assertIn(area, self.readme)

    def test_readme_names_a_definition_that_exists(self) -> None:
        missing: list[str] = []
        for area in self.areas:
            named = NAMED_DEFINITION.findall(self.readme[area])
            self.assertTrue(
                named, f"{area.name}/README.md names no formal definition file"
            )
            for name in named:
                if not (area / name).is_file():
                    missing.append(f"{area.name}/README.md: {name}")

        self.assertEqual(missing, [])

    def test_readme_lists_endpoints_the_definition_declares(self) -> None:
        """The README's endpoint list and the definition's `paths:` agree.

        Equality in both directions, because the list is presented as the paths
        the definition holds: a path in one and not the other is a document that
        is wrong about its own contract, whether it is missing an entry or
        promising one that was never defined. This is also what keeps the list
        from becoming a second, unchecked copy of `paths:`.
        """
        checked = 0
        mismatched: list[str] = []
        for area in self.areas:
            defined = self.paths[area]
            if not defined:
                continue
            stated = {path for _, path in STATED_ENDPOINT.findall(self.readme[area])}
            checked += len(stated)
            for path in sorted(stated - defined):
                mismatched.append(f"{area.name}/README.md lists {path}, not in paths:")
            for path in sorted(defined - stated):
                mismatched.append(f"{area.name}/README.md omits {path}, which paths: has")

        self.assertGreater(checked, 0, "no endpoint list was checked")
        self.assertEqual(mismatched, [])

    def test_readme_revision_matches_the_definition(self) -> None:
        stale: list[str] = []
        for area in self.areas:
            for value in dangling_revisions(self.readme[area], self.revisions[area]):
                stale.append(
                    f"{area.name}/README.md: revision {value} is on no "
                    f"{area.name}/v1/*.yaml (has {sorted(self.revisions[area])})"
                )

        self.assertEqual(stale, [])

    def test_the_revision_check_reports_a_stale_readme(self) -> None:
        """Positive control: a matching pair passes, a stale one is reported.

        Without this, `test_readme_revision_matches_the_definition` passing is
        also what a check that never compares anything would produce.
        """
        with tempfile.TemporaryDirectory() as tmp:
            definition = Path(tmp) / "v1" / "status.yaml"
            definition.parent.mkdir()
            definition.write_text('schema_version: 1\nrevision: "2026.01.01.1"\n')

            revisions = definition_revisions([definition])
            self.assertEqual(revisions, {"2026.01.01.1"})
            self.assertEqual(
                dangling_revisions("Definition: `v1/status.yaml`, revision `2026.01.01.1`.", revisions),
                [],
            )
            self.assertEqual(
                dangling_revisions("Definition: `v1/status.yaml`, revision `2025.12.31.9`.", revisions),
                ["2025.12.31.9"],
            )

    def test_openapi_information_version_is_read_as_a_revision(self) -> None:
        """`info.version` is how the OpenAPI file states its revision."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local-agent.openapi.yaml"
            path.write_text(
                "openapi: 3.0.3\ninfo:\n  title: T\n  version: 2026.09.24.1\npaths: {}\n"
            )

            self.assertEqual(definition_revisions([path]), {"2026.09.24.1"})


if __name__ == "__main__":
    unittest.main()

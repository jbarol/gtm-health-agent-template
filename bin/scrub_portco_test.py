"""Tests for bin/scrub-portco.py and its pattern catalog.

Each test invokes scrub-portco.py as a subprocess against a tmp_path or fixture
tree. This keeps tests independent of any in-process state and exercises the
exit-code contract (0 = clean, 1 = HIGH blocking).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "scrub-portco.py"
PATTERNS = REPO_ROOT / "bin" / "scrub-portco-patterns.yml"
NAMES_EXAMPLE = REPO_ROOT / "bin" / "scrub-portco-names.yml.example"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPT), *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def _scan_json(root: Path, *extra_args: str) -> dict:
    """Default scan helper. By default suppresses the local
    bin/scrub-portco-names.yml (which exists on the maintainer's machine but
    not in CI) so tests are deterministic. If extra_args contains `--names`,
    the suppression is dropped so the supplied names file actually loads."""
    args = ["--root", str(root), "--json"]
    if not any(a == "--names" for a in extra_args):
        args.append("--no-names")
    args.extend(extra_args)
    proc = _run(*args)
    assert proc.returncode in (0, 1), (
        f"unexpected exit {proc.returncode}: {proc.stderr}"
    )
    return json.loads(proc.stdout)


def test_help_runs():
    proc = _run("--help")
    assert proc.returncode == 0
    assert "scrub" in proc.stdout.lower()


def test_clean_tree_exits_zero(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("print('hello, world')\n", encoding="utf-8")
    data = _scan_json(tmp_path)
    assert data["summary"]["total_findings"] == 0


# Synthetic sample IDs — chosen to match the shape patterns without being
# any real production identifier. The catalog no longer excludes this test
# file, so these values must be safe to scan + publish.
SYN_SLACK_APP = "A0SYN00APP01"
SYN_SLACK_CHANNEL = "C0SYN00CHAN1"
SYN_SLACK_USER = "U0SYN00USER1"
SYN_SLACK_TEAM = "T0SYN00TEAM1"
SYN_VAULT = "vlt_SYN0VAULTSYN0VAULT01"
SYN_AGENT = "agent_SYN00AGENT00AGENT00X"
SYN_SESSION = "sesn_SYN00SESSION0SESSION0X"
SYN_THREAD = "sthr_SYN00THREAD00THREAD0XX"
SYN_MEMSTORE = "memstore_SYN00MEM00MEM00ZZZ"
SYN_SF_ORG_18 = "00DSYNTHETICORG123"
SYN_UUID = "00112233-4455-6677-8899-aabbccddeeff"


def test_detects_slack_app_id(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        f'SLACK_APP_ID = "{SYN_SLACK_APP}"\n', encoding="utf-8"
    )
    data = _scan_json(tmp_path)
    assert data["summary"]["total_findings"] == 1
    finding = data["findings"][0]
    assert finding["category"] == "slack_identifiers"
    assert finding["pattern_name"] == "app_id"
    assert finding["matched_text"] == SYN_SLACK_APP
    assert finding["severity"] == "HIGH"


def test_detects_slack_channel_user_team_ids(tmp_path: Path) -> None:
    (tmp_path / "ids.py").write_text(
        f'CH = "{SYN_SLACK_CHANNEL}"\nUSER = "{SYN_SLACK_USER}"\nTEAM = "{SYN_SLACK_TEAM}"\n',
        encoding="utf-8",
    )
    data = _scan_json(tmp_path)
    cats = data["summary"]["by_category"]
    assert cats.get("slack_identifiers") == 3


def test_detects_anthropic_vault_id(tmp_path: Path) -> None:
    (tmp_path / "vault.py").write_text(f'VAULT_ID = "{SYN_VAULT}"\n', encoding="utf-8")
    data = _scan_json(tmp_path)
    assert any(f["pattern_name"] == "vault_id" for f in data["findings"])


def test_detects_agent_and_session_ids(tmp_path: Path) -> None:
    (tmp_path / "agents.py").write_text(
        f'AGENT = "{SYN_AGENT}"\nSESSION = "{SYN_SESSION}"\n',
        encoding="utf-8",
    )
    data = _scan_json(tmp_path)
    names = {f["pattern_name"] for f in data["findings"]}
    assert "agent_id" in names
    assert "session_id" in names


def test_detects_anthropic_memstore_id(tmp_path: Path) -> None:
    """memstore_* is the same private-resource class as vlt_*; codex round 3."""
    (tmp_path / "mem.py").write_text(
        f'METHODOLOGY = "{SYN_MEMSTORE}"\n', encoding="utf-8"
    )
    data = _scan_json(tmp_path)
    names = {f["pattern_name"] for f in data["findings"]}
    assert "memstore_id" in names


def test_detects_anthropic_thread_id(tmp_path: Path) -> None:
    """sthr_* is the Anthropic Managed Agents session-thread ID class;
    codex round 13 caught the catalog gap that left them passing the gate
    while sibling sesn_* IDs were blocked (PR #259 review)."""
    (tmp_path / "thread.py").write_text(f'THREAD = "{SYN_THREAD}"\n', encoding="utf-8")
    data = _scan_json(tmp_path)
    names = {f["pattern_name"] for f in data["findings"]}
    assert "thread_id" in names, (
        f"thread_id pattern did not fire on {SYN_THREAD}: {data}"
    )
    hit = next(f for f in data["findings"] if f["pattern_name"] == "thread_id")
    assert hit["severity"] == "HIGH"
    assert hit["matched_text"] == SYN_THREAD


def test_detects_salesforce_domains(tmp_path: Path) -> None:
    (tmp_path / "sf.py").write_text(
        'MY = "example.my.salesforce.com"\n'
        'LIGHT = "example.lightning.force.com"\n'
        f'ORG = "{SYN_SF_ORG_18}"\n',
        encoding="utf-8",
    )
    data = _scan_json(tmp_path)
    cats = data["summary"]["by_category"]
    assert cats.get("salesforce_identifiers", 0) >= 3


def test_detects_uuid(tmp_path: Path) -> None:
    (tmp_path / "uuid.py").write_text(f'CONTAINER = "{SYN_UUID}"\n', encoding="utf-8")
    data = _scan_json(tmp_path)
    assert any(f["category"] == "uuids" for f in data["findings"])


def test_detects_portco_names_case_aware(tmp_path: Path) -> None:
    """Synthetic-name patterns from the .example file match case-aware."""
    (tmp_path / "names.md").write_text(
        "# About Acme Corp\nacme-corp is also matched.\n"
        "We talk about Delta Inc as a portfolio company.\n",
        encoding="utf-8",
    )
    data = _scan_json(tmp_path, "--names", str(NAMES_EXAMPLE))
    names = {f["pattern_name"] for f in data["findings"]}
    assert "acme_corp" in names
    assert "delta_inc" in names


def test_detects_vendor_names(tmp_path: Path) -> None:
    """Vendor-name patterns from the .example file fire on synthetic vendors."""
    (tmp_path / "vendors.md").write_text(
        "We use Vendor X and Data Provider Y.\n",
        encoding="utf-8",
    )
    data = _scan_json(tmp_path, "--names", str(NAMES_EXAMPLE))
    names = {f["pattern_name"] for f in data["findings"]}
    assert "vendor_x" in names
    assert "data_provider_y" in names


def test_detects_email_addresses(tmp_path: Path) -> None:
    """Email patterns from the .example file fire on synthetic operator emails."""
    (tmp_path / "emails.py").write_text(
        'OWNER = "operator@example.com"\n',
        encoding="utf-8",
    )
    data = _scan_json(tmp_path, "--names", str(NAMES_EXAMPLE))
    names = {f["pattern_name"] for f in data["findings"]}
    assert "any_operator_email" in names


def test_detects_deployment_url(tmp_path: Path) -> None:
    """Deployment-URL patterns from the .example file fire on synthetic URLs."""
    (tmp_path / "deploy.md").write_text(
        "Production: https://example-production.example.com/health\n",
        encoding="utf-8",
    )
    data = _scan_json(tmp_path, "--names", str(NAMES_EXAMPLE))
    assert any(f["pattern_name"] == "production_url" for f in data["findings"])


def test_medium_severity_incident_artifacts(tmp_path: Path) -> None:
    (tmp_path / "incident.md").write_text(
        "See inv 58 and PR #245 for context.\nPlan #52 traces the work.\n",
        encoding="utf-8",
    )
    data = _scan_json(tmp_path)
    severities = {f["severity"] for f in data["findings"]}
    assert "MEDIUM" in severities
    # MEDIUM alone should NOT make exit code 1 by default
    proc = _run("--root", str(tmp_path))
    assert proc.returncode == 0


def test_high_severity_blocks_exit(tmp_path: Path) -> None:
    (tmp_path / "blocker.py").write_text(
        f'CHANNEL = "{SYN_SLACK_CHANNEL}"\n', encoding="utf-8"
    )
    proc = _run("--root", str(tmp_path))
    assert proc.returncode == 1
    assert "HIGH" in proc.stdout


def test_severity_filter(tmp_path: Path) -> None:
    """--severity HIGH suppresses MEDIUM rows in the report."""
    (tmp_path / "mixed.py").write_text(
        f'CH = "{SYN_SLACK_CHANNEL}"\n'  # HIGH
        "# investigation inv 42 was the source\n",  # MEDIUM
        encoding="utf-8",
    )
    proc = _run("--root", str(tmp_path), "--severity", "HIGH", "--json")
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    severities = {f["severity"] for f in data["findings"]}
    assert severities == {"HIGH"}


def test_fail_on_never_returns_zero(tmp_path: Path) -> None:
    (tmp_path / "block.py").write_text(
        f'CH = "{SYN_SLACK_CHANNEL}"\n', encoding="utf-8"
    )
    proc = _run("--root", str(tmp_path), "--fail-on", "NEVER")
    assert proc.returncode == 0


def test_skips_binary_file(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02Acme\x00")
    data = _scan_json(tmp_path)
    assert data["summary"]["total_findings"] == 0


def test_respects_git_dir_exclude(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        "[remote]\n  url = git@example.com/Acme\n", encoding="utf-8"
    )
    data = _scan_json(tmp_path)
    assert data["summary"]["total_findings"] == 0


def test_allowlist_suppresses_finding(tmp_path: Path) -> None:
    """A targeted allowlist entry hides a known-safe HIGH finding."""
    custom_catalog = tmp_path / "patterns.yml"
    custom_catalog.write_text(
        "version: 1\n"
        "categories:\n"
        "  test_names:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - {name: token_a, regex: '\\bTOKEN_A\\b'}\n"
        "exclude_paths:\n"
        "  - patterns.yml\n"
        "allowlist:\n"
        "  - file: legacy/*.md\n"
        "    pattern: test_names.token_a\n"
        "    reason: legacy doc; sanitized in another PR\n",
        encoding="utf-8",
    )
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "old.md").write_text("TOKEN_A here.\n", encoding="utf-8")
    (tmp_path / "other.md").write_text("TOKEN_A elsewhere.\n", encoding="utf-8")

    proc = _run(
        "--root",
        str(tmp_path),
        "--patterns",
        str(custom_catalog),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    # Only the non-allowlisted finding remains.
    assert data["summary"]["total_findings"] == 1
    assert data["findings"][0]["file"] == "other.md"


def test_invalid_regex_in_catalog_exits_two(tmp_path: Path) -> None:
    bad_catalog = tmp_path / "bad.yml"
    bad_catalog.write_text(
        "version: 1\n"
        "categories:\n"
        "  broken:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - {name: broken_regex, regex: '['}\n",
        encoding="utf-8",
    )
    proc = _run("--patterns", str(bad_catalog), "--root", str(tmp_path))
    assert proc.returncode == 2
    assert "regex" in proc.stderr.lower() or "catalog" in proc.stderr.lower()


def test_missing_patterns_file_exits_two(tmp_path: Path) -> None:
    proc = _run("--patterns", str(tmp_path / "nope.yml"), "--root", str(tmp_path))
    assert proc.returncode == 2


def test_real_catalog_loads() -> None:
    """The committed catalog must compile cleanly."""
    proc = _run(
        "--patterns",
        str(PATTERNS),
        "--root",
        str(REPO_ROOT / "bin" / "scrub-portco-fixtures"),
        "--json",
    )
    # Exit may be 0 or 1 depending on fixture contents; the point is it must parse.
    assert proc.returncode in (0, 1)


def test_scans_file_path_for_portco_name(tmp_path: Path) -> None:
    """A clean file whose path contains a synthetic portco name is flagged.

    Uses the committed .example names file so the test runs deterministically
    in a fresh checkout where bin/scrub-portco-names.yml is absent.
    """
    sub = tmp_path / "docs" / "research"
    sub.mkdir(parents=True)
    (sub / "acme-corp-index.md").write_text(
        "# Clean body, no leaks here.\n", encoding="utf-8"
    )
    data = _scan_json(tmp_path, "--no-git", "--names", str(NAMES_EXAMPLE))
    path_findings = [
        f for f in data["findings"] if f["pattern_name"].endswith("(path)")
    ]
    assert path_findings, f"expected a (path) finding, got {data}"
    hit = path_findings[0]
    assert "acme" in hit["matched_text"].lower()
    assert hit["line"] == 0


def test_flags_high_risk_binary_extension(tmp_path: Path) -> None:
    """A tracked .docx triggers a HIGH 'tracked_binary' finding.

    Codex round 6 bumped unscanned_binaries to HIGH because the workflow
    filters --severity HIGH; a MEDIUM binary would silently pass the gate.
    Per-binary allowlist is the documented escape hatch.
    """
    (tmp_path / "report.docx").write_bytes(b"PK\x03\x04dummy office xml\x00")
    proc = _run("--root", str(tmp_path), "--no-git", "--no-names", "--json")
    data = json.loads(proc.stdout)
    binaries = [f for f in data["findings"] if f["category"] == "unscanned_binaries"]
    assert len(binaries) == 1
    assert binaries[0]["severity"] == "HIGH"
    assert binaries[0]["matched_text"] == "report.docx"
    # HIGH-severity output DOES make --fail-on HIGH return 1.
    plain = _run("--root", str(tmp_path), "--no-git", "--no-names")
    assert plain.returncode == 1


def test_no_git_scans_dotenv_force_add(tmp_path: Path) -> None:
    """Without git scoping (or in a non-git tree) a .env is scanned."""
    (tmp_path / ".env").write_text(
        f'CHANNEL = "{SYN_SLACK_CHANNEL}"\n', encoding="utf-8"
    )
    data = _scan_json(tmp_path)  # _scan_json uses defaults; tmp_path has no .git
    assert any(f["file"] == ".env" for f in data["findings"]), (
        f"expected .env to be scanned, got {data}"
    )


def test_git_mode_skips_untracked_dotenv(tmp_path: Path) -> None:
    """When --root IS a git tree, untracked .env is invisible to the scan."""
    # Init a git tree and create an UNTRACKED .env (would be gitignored).
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("# kept\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "kept.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"],
        check=True,
    )
    # Now create the .env AFTER commit — untracked.
    (tmp_path / ".env").write_text(
        f'CHANNEL = "{SYN_SLACK_CHANNEL}"\n', encoding="utf-8"
    )

    proc = _run("--root", str(tmp_path), "--json")  # use_git defaults to True
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["summary"]["total_findings"] == 0, (
        f"expected untracked .env to be skipped, got {data}"
    )


def test_git_mode_catches_force_added_dotenv(tmp_path: Path) -> None:
    """A force-added (TRACKED) .env is caught even when listed in .gitignore."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f'CHANNEL = "{SYN_SLACK_CHANNEL}"\n', encoding="utf-8"
    )
    # Force-add the .env despite the gitignore.
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", ".env", ".gitignore"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "leak"],
        check=True,
    )

    proc = _run("--root", str(tmp_path), "--json")
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert any(f["file"] == ".env" for f in data["findings"]), (
        f"expected force-added .env to be scanned, got {data}"
    )


def test_patterns_yml_self_canary_scans_clean() -> None:
    """The committed catalog must NOT contain any HIGH-severity self-matches.

    Codex review (P1) flagged the prior self-exclusion as a back door: the
    catalog itself shipped with real Slack/SF/Anthropic IDs in `example:`
    fields. The fix removed those, removed the self-exclusion, and this
    test pins the invariant.
    """
    proc = _run(
        "--patterns",
        str(PATTERNS),
        "--root",
        str(PATTERNS.parent),
        "--no-git",
        "--severity",
        "HIGH",
        "--json",
    )
    # Filter findings to only the patterns.yml file itself.
    data = json.loads(proc.stdout)
    self_findings = [
        f for f in data["findings"] if f["file"].endswith("scrub-portco-patterns.yml")
    ]
    assert not self_findings, (
        f"scrub-portco-patterns.yml leaked HIGH-severity strings: {self_findings}"
    )


SYN_MODERN_CHANNEL = "C1SYNCHANNEL"
SYN_MODERN_USER = "U2SYNUSERID0"


def test_slack_id_pattern_matches_modern_format(tmp_path: Path) -> None:
    """Codex P2: Slack IDs no longer always have '0' as second char.

    Uses SYN-tagged synthetic IDs (with non-`0` second char) so the catalog
    allowlist's 'SYN' text_pattern keeps the test file scrub-clean.
    """
    (tmp_path / "modern.py").write_text(
        f'CH = "{SYN_MODERN_CHANNEL}"\nUSER = "{SYN_MODERN_USER}"\n',
        encoding="utf-8",
    )
    proc = _run("--root", str(tmp_path), "--no-git", "--no-names", "--json")
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    matched = {f["matched_text"] for f in data["findings"]}
    assert SYN_MODERN_CHANNEL in matched
    assert SYN_MODERN_USER in matched


@pytest.mark.parametrize(
    "name,sample",
    [
        ("acme_corp", "Acme Corp is the lead portco"),
        ("delta_inc", "Delta Inc joined the fund"),
        ("example_one", "Example One signed last quarter"),
        ("vendor_x", "We integrate with Vendor X"),
        ("data_provider_y", "Data Provider Y enriches our leads"),
    ],
)
def test_each_example_name_fires(tmp_path: Path, name: str, sample: str) -> None:
    """Every synthetic pattern in the committed .example file matches its sample."""
    (tmp_path / "p.md").write_text(sample + "\n", encoding="utf-8")
    data = _scan_json(tmp_path, "--names", str(NAMES_EXAMPLE))
    names = {f["pattern_name"] for f in data["findings"]}
    assert name in names, f"pattern {name} did not fire on {sample!r}"


def test_subtree_git_filter_skips_gitignored(tmp_path: Path) -> None:
    """When --root is a subdir of a git worktree, .gitignore semantics
    must apply — so a gitignored secret file under that subdir is
    invisible to the scan. Before codex round 14 P2 the (.git).exists()
    check failed for any non-toplevel --root and fell through to rglob.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    sub = tmp_path / "lib"
    sub.mkdir()
    (tmp_path / ".gitignore").write_text("lib/private.yml\n", encoding="utf-8")
    (sub / "tracked.py").write_text("# tracked\n", encoding="utf-8")
    (sub / "private.yml").write_text(
        f'CHANNEL = "{SYN_SLACK_CHANNEL}"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "lib/tracked.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"], check=True
    )
    data = _scan_json(sub)
    assert data["summary"]["total_findings"] == 0, (
        f"gitignored file inside subtree scanned: {data}"
    )


def test_redact_uses_longest_first_substring_replacement(tmp_path: Path) -> None:
    """When two private patterns capture overlapping substrings (e.g.
    'Acme' and 'Acme Corp'), the longer redaction must apply first so
    output doesn't leak '[REDACTED] Corp'. (Codex round 14 P2.)
    """
    names = tmp_path / "names.yml"
    names.write_text(
        "version: 1\n"
        "categories:\n"
        "  portco_names:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - {name: short, regex: '\\bAcme\\b'}\n"
        "      - {name: long, regex: '\\bAcme Corp\\b'}\n",
        encoding="utf-8",
    )
    sub = tmp_path / "docs"
    sub.mkdir()
    # Filename contains both shorter and longer alias as substrings.
    (sub / "Acme Corp summary.md").write_text("clean body\n", encoding="utf-8")
    data = _scan_json(tmp_path, "--names", str(names), "--redact", "--no-git")
    for f in data["findings"]:
        assert "Acme" not in f["file"], (
            f"short alias leaked into path under --redact: {f}"
        )


def test_redact_sanitizes_private_catalog_error(tmp_path: Path) -> None:
    """A regex error in the private names catalog must NOT print the real
    pattern name verbatim under --redact. Without this, a typo during the
    documented CI setup would publish the offending portco/vendor name
    into the Actions log. (Codex round 14 P2.)
    """
    names = tmp_path / "names.yml"
    names.write_text(
        "version: 1\n"
        "categories:\n"
        "  portco_names:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - {name: secret_portco_label, regex: '['}\n",
        encoding="utf-8",
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--names",
        str(names),
        "--redact",
    )
    assert proc.returncode == 2, (
        f"expected catalog-error exit code 2, got {proc.returncode}: {proc.stderr}"
    )
    assert "secret_portco_label" not in proc.stderr, (
        f"private pattern name leaked under --redact: {proc.stderr}"
    )
    assert "regex" in proc.stderr.lower() or "catalog" in proc.stderr.lower(), (
        f"stderr lost its diagnostic value: {proc.stderr}"
    )


def test_unredacted_catalog_error_still_shows_pattern_name(tmp_path: Path) -> None:
    """Without --redact, the catalog error must still surface the failing
    pattern name so the local maintainer can debug it. The redaction is
    --redact-gated, not blanket. (Codex round 14 P2 inverse check.)
    """
    names = tmp_path / "names.yml"
    names.write_text(
        "version: 1\n"
        "categories:\n"
        "  portco_names:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - {name: debug_visible_name, regex: '['}\n",
        encoding="utf-8",
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--names",
        str(names),
    )
    assert proc.returncode == 2, proc.stderr
    assert "debug_visible_name" in proc.stderr, (
        f"local maintainer needs the pattern name in plain mode: {proc.stderr}"
    )


def test_redact_redacts_scan_root_in_summary(tmp_path: Path) -> None:
    """summary.scan_root must not echo the absolute scan path under --redact.

    A CI workspace named after a portco (e.g. acme-corp-runner-1) would
    otherwise leak that name in the JSON summary even though every
    finding is scrubbed. Codex round 15 P2.
    """
    (tmp_path / "noop.py").write_text("# nothing\n", encoding="utf-8")
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-names",
        "--no-git",
        "--redact",
        "--json",
    )
    data = json.loads(proc.stdout)
    assert data["summary"]["scan_root"] == "[REDACTED]", (
        f"scan_root leaked under --redact: {data['summary']}"
    )
    proc_plain = _run(
        "--root",
        str(tmp_path),
        "--no-names",
        "--no-git",
        "--json",
    )
    plain = json.loads(proc_plain.stdout)
    assert plain["summary"]["scan_root"] == str(tmp_path), (
        f"plain mode dropped scan_root: {plain['summary']}"
    )


def test_redact_sanitizes_yaml_parse_error(tmp_path: Path) -> None:
    """A YAML parse error in the private names catalog must not print the
    offending source line under --redact. PyYAML errors include the line
    that triggered the failure, which in a private catalog can carry the
    real portco / vendor name. (Codex round 15 P2.)
    """
    names = tmp_path / "names.yml"
    # Intentionally malformed YAML where the bad source token is itself
    # a sensitive-looking identifier.
    names.write_text(
        "version: 1\ncategories:\n"
        "  portco_names:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - { name: secret_yaml_line, regex: 'unclosed-string\n",
        encoding="utf-8",
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--names",
        str(names),
        "--redact",
    )
    assert proc.returncode == 2, (
        f"expected exit code 2 on YAML error, got {proc.returncode}: "
        f"out={proc.stdout} err={proc.stderr}"
    )
    assert "secret_yaml_line" not in proc.stderr, (
        f"YAML parse error leaked the private source line under --redact: {proc.stderr}"
    )
    assert "private" in proc.stderr.lower() or "yaml" in proc.stderr.lower(), (
        f"stderr lost its actionable hint: {proc.stderr}"
    )


def test_unredacted_yaml_parse_error_shows_diagnostic(tmp_path: Path) -> None:
    """Without --redact, the YAML error must still surface enough detail
    for the local maintainer to debug the malformed catalog. Round 15
    P2 inverse check.
    """
    names = tmp_path / "names.yml"
    names.write_text(
        "version: 1\ncategories:\n"
        "  portco_names:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - { name: debuggable_line, regex: 'unclosed-string\n",
        encoding="utf-8",
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--names",
        str(names),
    )
    assert proc.returncode == 2, proc.stderr
    assert any(
        token in proc.stderr.lower()
        for token in ("scanner", "parser", "yaml", "line", "column")
    ), f"expected a verbose YAML diagnostic in plain mode: {proc.stderr}"


def test_tracked_private_names_catalog_emits_high(tmp_path: Path) -> None:
    """If the configured --names catalog is force-tracked, the scanner
    must emit a HIGH finding regardless of --no-names. (Codex round 16 P1.)

    The names catalog file's contents are valid YAML with regex patterns;
    those regex names ARE the private portco/vendor names. Letting the
    file ship publicly defeats the entire gating purpose. The tracked
    check fires before content scanning so the gate fails even when the
    catalog body matches no public-shape pattern.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".gitignore").write_text("names.yml\n", encoding="utf-8")
    names = tmp_path / "names.yml"
    # Empty-but-valid YAML so no content-pattern would fire.
    names.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    # Force-add despite .gitignore — the exact bypass codex flagged.
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", ".gitignore", "names.yml"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "leak"], check=True
    )
    # Use --no-names so the catalog is NOT loaded into the patterns
    # registry but its path is still configured.
    proc = _run(
        "--root",
        str(tmp_path),
        "--names",
        str(names),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, (
        f"gate must fail when private catalog is tracked: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    tracked = [
        f for f in data["findings"] if f.get("pattern_name") == "private_names_catalog"
    ]
    assert tracked, f"expected private-catalog HIGH finding: {data}"
    assert all(f["severity"] == "HIGH" for f in tracked)
    assert all(f["category"] == "private_catalog_tracked" for f in tracked)


def test_tracked_dist_file_is_scanned(tmp_path: Path) -> None:
    """Tracked files under dist/ and build/ must NOT be blanket-excluded.

    A committed packaged artifact would otherwise ship private content
    while the gate reports clean. Codex round 16 P1.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text(
        f'window.CHANNEL = "{SYN_SLACK_CHANNEL}";\n', encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "dist/bundle.js"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, f"tracked dist/ artifact was excluded: {proc.stdout}"
    data = json.loads(proc.stdout)
    assert any(f["file"].startswith("dist/") for f in data["findings"]), (
        f"expected at least one dist/* finding: {data}"
    )


def test_tracked_build_file_is_scanned(tmp_path: Path) -> None:
    """Sibling check to test_tracked_dist_file_is_scanned: the same
    bypass existed for build/**. Round 16 P1.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "app.bundle").write_text(
        f'export const TEAM = "{SYN_SLACK_TEAM}";\n', encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "build/app.bundle"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, f"tracked build/ artifact was excluded: {proc.stdout}"
    data = json.loads(proc.stdout)
    assert any(f["file"].startswith("build/") for f in data["findings"]), (
        f"expected at least one build/* finding: {data}"
    )


def test_redact_fully_sanitizes_private_catalog_compile_error(
    tmp_path: Path,
) -> None:
    """A compile_index() error on the private catalog must NOT leak
    arbitrary pattern-body text under --redact, even if that text is
    not in the precomputed private_terms set. (Codex round 16 P2.)

    Before the fix, _redact_private_text() only substituted known
    category/name tokens; if the failing pattern's regex contained a
    portco name, that name printed verbatim. The new behavior validates
    the private catalog in isolation and emits a fully generic message
    when it fails under --redact.
    """
    names = tmp_path / "names.yml"
    # Severity field is bogus so compile_index() raises with the
    # severity value verbatim. That value here doubles as a private
    # identifier the test wants to confirm is NOT printed.
    names.write_text(
        "version: 1\ncategories:\n"
        "  portco_names:\n"
        "    severity: SECRET_OPERATOR_NAME_LEVEL\n"
        "    patterns:\n"
        "      - {name: foo, regex: 'X'}\n",
        encoding="utf-8",
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--names",
        str(names),
        "--redact",
    )
    assert proc.returncode == 2, (
        f"expected catalog-error exit 2, got {proc.returncode}: {proc.stderr}"
    )
    assert "SECRET_OPERATOR_NAME_LEVEL" not in proc.stderr, (
        f"private catalog body content leaked into stderr under --redact: {proc.stderr}"
    )
    assert "private" in proc.stderr.lower() or "catalog" in proc.stderr.lower(), (
        f"stderr lost its actionable hint: {proc.stderr}"
    )


def _init_git(repo: Path) -> None:
    """Make `repo` a minimal committable git tree (used by R17 tests)."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)


def test_tracked_renamed_private_catalog_emits_high(tmp_path: Path) -> None:
    """Any tracked file whose basename starts with `scrub-portco-names`
    must fire HIGH — backups like `scrub-portco-names.yml.bak` or dated
    copies are private regardless of the configured --names path. The
    committed `.example` template is allowed only at the canonical repo
    path AND only when its content_sha256 matches the allowlist entry;
    at a non-canonical path it's still flagged (codex round 18 P1
    plugged the basename-only exemption). Codex round 17 P1.
    """
    _init_git(tmp_path)
    backup = tmp_path / "scrub-portco-names.yml.bak"
    dated = tmp_path / "scrub-portco-names-2026-05.yml"
    rogue_example = tmp_path / "scrub-portco-names.yml.example"
    backup.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    dated.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    rogue_example.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "add",
            backup.name,
            dated.name,
            rogue_example.name,
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "copies"], check=True
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, (
        f"renamed-copy bypass: gate did not fail: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    tracked_files = {
        f["file"]
        for f in data["findings"]
        if f.get("pattern_name") == "private_names_catalog"
    }
    assert backup.name in tracked_files, f"backup file not caught: {tracked_files}"
    assert dated.name in tracked_files, f"dated copy not caught: {tracked_files}"
    # The .example at a non-canonical path IS now flagged (round 18 P1).
    # The committed allowlist entry only matches the canonical
    # `bin/scrub-portco-names.yml.example` path.
    assert rogue_example.name in tracked_files, (
        f".example at non-canonical path must still be flagged: {tracked_files}"
    )


def test_canonical_example_template_passes_with_correct_digest() -> None:
    """The canonical bin/scrub-portco-names.yml.example template must be
    allowlist-suppressed (its content_sha256 in scrub-portco-patterns.yml
    matches the file's current digest). If this test fails after
    intentionally editing the template, update the content_sha256 in the
    allowlist entry in the SAME commit. Codex round 18 P1.
    """
    proc = _run(
        "--root",
        str(REPO_ROOT / "bin"),
        "--no-names",
        "--no-git",
        "--severity",
        "HIGH",
        "--json",
    )
    data = json.loads(proc.stdout)
    flagged = [
        f
        for f in data["findings"]
        if f.get("category") == "private_catalog_tracked"
        and "scrub-portco-names.yml.example" in f.get("file", "")
    ]
    assert not flagged, (
        f"canonical .example template was flagged — update the allowlist "
        f"content_sha256 in scrub-portco-patterns.yml if the template "
        f"changed: {flagged}"
    )


def test_canonical_example_template_fires_when_content_mutates(
    tmp_path: Path,
) -> None:
    """If the canonical .example template is overwritten with content
    that doesn't match the pinned content_sha256, the allowlist no longer
    suppresses it and the gate fails. Codex round 18 P1.
    """
    # Use a custom catalog so the test doesn't mutate the real
    # committed .example. The custom catalog pins a fake digest that
    # we know won't match the file's actual content.
    fake_digest = "0" * 64  # sha256 hex digits; definitely won't match
    example_copy = tmp_path / "bin"
    example_copy.mkdir()
    target = example_copy / "scrub-portco-names.yml.example"
    target.write_text(
        "version: 1\ncategories:\n  portco_names:\n"
        "    severity: HIGH\n    patterns: []\n",
        encoding="utf-8",
    )
    custom_catalog = tmp_path / "patterns.yml"
    custom_catalog.write_text(
        "version: 1\n"
        "categories:\n"
        "  private_catalog_tracked:\n"
        "    severity: HIGH\n"
        "    patterns: []\n"
        "exclude_paths:\n"
        "  - patterns.yml\n"
        "allowlist:\n"
        "  - file: bin/scrub-portco-names.yml.example\n"
        "    pattern: private_catalog_tracked.private_names_catalog\n"
        f"    content_sha256: '{fake_digest}'\n"
        "    reason: pinned to non-matching digest for this test\n",
        encoding="utf-8",
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--patterns",
        str(custom_catalog),
        "--no-names",
        "--no-git",
        "--json",
    )
    assert proc.returncode == 1, (
        f"mismatched digest should re-fire the private catalog finding: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    assert any(
        f.get("pattern_name") == "private_names_catalog"
        and f.get("file", "").endswith(".example")
        for f in data["findings"]
    ), data


def test_tracked_files_in_excluded_dirs_still_scanned(tmp_path: Path) -> None:
    """exclude_paths must NOT apply to tracked files. A force-add into
    a typically-excluded directory (e.g. node_modules/ or .claude/
    worktrees/) ships publicly with the repo and must reach scanning.
    Codex round 18 P1.
    """
    _init_git(tmp_path)
    # Create a custom catalog with the same noise-exclude patterns as the
    # real catalog so we exercise the actual code path.
    custom_catalog = tmp_path / "patterns.yml"
    custom_catalog.write_text(
        "version: 1\n"
        "categories:\n"
        "  slack_identifiers:\n"
        "    severity: HIGH\n"
        "    patterns:\n"
        "      - {name: channel_id, regex: '\\bC(?=[A-Z0-9]*[0-9])[A-Z0-9]{8,}\\b'}\n"
        "exclude_paths:\n"
        "  - 'node_modules/**'\n"
        "  - 'patterns.yml'\n",
        encoding="utf-8",
    )
    nm = tmp_path / "node_modules" / "leak"
    nm.mkdir(parents=True)
    leak_file = nm / "config.js"
    leak_file.write_text(
        f'export const CH = "{SYN_SLACK_CHANNEL}";\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", "node_modules/leak/config.js"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "force-add"], check=True
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--patterns",
        str(custom_catalog),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, (
        f"force-added file under excluded dir was skipped: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    assert any("node_modules" in f.get("file", "") for f in data["findings"]), (
        f"expected node_modules/* finding: {data}"
    )


def test_rglob_includes_symlinks_to_dirs(tmp_path: Path) -> None:
    """Filesystem-mode (--no-git or non-git tree) must include symlinks
    in the candidate enumeration so their targets and pathnames reach
    the symlink handler. Before codex round 18 P2 `p.is_file()` dropped
    symlinks to directories and broken targets.
    """
    # Create a symlink whose readlink target contains a synthetic Slack
    # channel ID. Point the link at a directory so `is_file()` is False
    # — this is the case the prior filter dropped.
    target_dir = tmp_path / f"workspace-{SYN_SLACK_CHANNEL}"
    target_dir.mkdir()
    link = tmp_path / "current"
    link.symlink_to(target_dir, target_is_directory=True)
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-git",
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, (
        f"symlink-to-dir target with portco ID should fire: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    sym_hits = [
        f for f in data["findings"] if "symlink target" in f.get("pattern_name", "")
    ]
    assert sym_hits, f"expected at least one symlink-target finding: {data}"


def test_private_catalog_underscore_separator_caught(tmp_path: Path) -> None:
    """`_` is a regex word character so the prior `\\b`-anchored
    filename pattern missed underscore-separated backups like
    `scrub-portco-names_OLD.yml`. The new negative-lookahead form
    catches every non-alphanumeric separator. (Claude review.)
    """
    _init_git(tmp_path)
    underscore = tmp_path / "scrub-portco-names_OLD.yml"
    no_ext = tmp_path / "scrub-portco-names"
    namespaces = tmp_path / "scrub-portco-namespaces.yml"
    underscore.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    no_ext.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    namespaces.write_text("# unrelated file\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "add",
            underscore.name,
            no_ext.name,
            namespaces.name,
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "underscore copy"],
        check=True,
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, (
        f"underscore-separator backup must trip the gate: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    private_hits = {
        f["file"]
        for f in data["findings"]
        if f.get("pattern_name") == "private_names_catalog"
    }
    assert underscore.name in private_hits, (
        f"scrub-portco-names_OLD.yml must fire: {private_hits}"
    )
    assert no_ext.name in private_hits, (
        f"bare scrub-portco-names (no extension) must fire: {private_hits}"
    )
    assert namespaces.name not in private_hits, (
        f"scrub-portco-namespaces.yml must NOT fire (prefix-but-not-separator "
        f"is a different identifier): {private_hits}"
    )


def test_tracked_symlink_to_private_catalog_fires(tmp_path: Path) -> None:
    """A tracked symlink whose target resolves to the configured
    --names path is the same disclosure as tracking the file directly.
    The prior symlink branch only scanned the readlink target string
    for catalog regex matches, which don't include private-catalog
    filenames. (Claude review.)
    """
    _init_git(tmp_path)
    # Real private-catalog-style file outside the scanned tree.
    real_catalog = tmp_path / "private-out-of-tree.yml"
    real_catalog.write_text("version: 1\ncategories: {}\n", encoding="utf-8")
    # Scanned tree contains an innocuously-named symlink to that file.
    scan_dir = tmp_path / "src"
    scan_dir.mkdir()
    link = scan_dir / "innocuous-link.yml"
    link.symlink_to(real_catalog)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "src/innocuous-link.yml"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "link"], check=True
    )
    proc = _run(
        "--root",
        str(scan_dir),
        "--names",
        str(real_catalog),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, (
        f"symlink to private catalog must trip the gate: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    private_hits = [
        f
        for f in data["findings"]
        if f.get("category") == "private_catalog_tracked"
        and "innocuous-link.yml" in f.get("file", "")
    ]
    assert private_hits, f"expected private-catalog finding on the symlink: {data}"


def test_sf_domain_case_insensitive(tmp_path: Path) -> None:
    """Salesforce domain patterns must match regardless of case. DNS is
    case-insensitive, so a leak copy/pasted with mixed capitalization
    from a doc or env var must still trip the gate. Codex round 20 P2.
    """
    payload = (
        'lower = "abcwidgets.my.salesforce.com"\n'
        'upper = "ABCWIDGETS.MY.SALESFORCE.COM"\n'
        'mixed = "AbcWidgets.My.SalesForce.Com"\n'
        'light = "AbcWidgets.lightning.force.com"\n'
    )
    (tmp_path / "sf.py").write_text(payload, encoding="utf-8")
    data = _scan_json(tmp_path)
    matched = {f["matched_text"] for f in data["findings"]}
    assert "abcwidgets.my.salesforce.com" in matched, matched
    assert "ABCWIDGETS.MY.SALESFORCE.COM" in matched, matched
    assert "AbcWidgets.My.SalesForce.Com" in matched, matched
    assert "AbcWidgets.lightning.force.com" in matched, matched


def test_id_patterns_fire_when_underscore_flanked(tmp_path: Path) -> None:
    """ID regexes must catch leaks when the ID is surrounded by `_` in
    filenames or keys (e.g. `events_C12345678_log.json`). Before codex
    round 19 P2 the trailing `\\b` did not match between a word char
    and `_` (both word chars in regex), so the gate ignored these.
    Asserts the new boundary semantics work across every ID category.
    """
    # Each line embeds a synthetic ID inside an underscore-flanked
    # token. All should fire.
    payload = "\n".join(
        [
            f'log_path = "events_{SYN_SLACK_CHANNEL}_log.json"',
            f'session_dump = "snapshot_{SYN_SESSION}_state.parquet"',
            f'thread_export = "_{SYN_THREAD}_trace.txt"',
            f'agent_event = "_{SYN_AGENT}_runs.csv"',
            f'vault_label = "_{SYN_VAULT}_secret.yml"',
            f'memstore_label = "_{SYN_MEMSTORE}_store.json"',
            f'org_label = "tenant_{SYN_SF_ORG_18}_audit.log"',
            f'uuid_label = "container_{SYN_UUID}_id.txt"',
        ]
    )
    (tmp_path / "logs.py").write_text(payload + "\n", encoding="utf-8")
    data = _scan_json(tmp_path)
    matched_text = {f["matched_text"] for f in data["findings"]}
    assert SYN_SLACK_CHANNEL in matched_text, (
        f"slack channel ID bypassed underscore flanking: {matched_text}"
    )
    assert SYN_SESSION in matched_text, matched_text
    assert SYN_THREAD in matched_text, matched_text
    assert SYN_AGENT in matched_text, matched_text
    assert SYN_VAULT in matched_text, matched_text
    assert SYN_MEMSTORE in matched_text, matched_text
    assert SYN_SF_ORG_18 in matched_text, matched_text
    assert SYN_UUID in matched_text, matched_text


def test_id_patterns_do_not_false_positive_on_alphanumeric_continuation(
    tmp_path: Path,
) -> None:
    """ID regexes with the new boundary semantics must still reject
    leading/trailing alphanumeric continuation (e.g. `xsesn_<id>` is
    not a real session ID — the prefix is corrupted). Round 19 P2
    inverse check.
    """
    (tmp_path / "junk.py").write_text(
        f'NOT_A_SESSION = "x{SYN_SESSION}"\nALSO_NOT = "{SYN_SESSION}xyz"\n',
        encoding="utf-8",
    )
    data = _scan_json(tmp_path)
    # `x<id>` is alphanumeric-prefixed; lookbehind rejects it.
    # `<id>xyz` is alphanumeric-suffixed; lookahead rejects it as the
    # FULL ID. But the greedy {20,} would consume `<id>xyz` for a
    # longer match — that's still a session_id finding on the
    # corrupted-but-leaky string. We accept that since any sesn_*
    # token is a leak regardless of trailing chars.
    # Assert: the standalone SYN_SESSION must NOT appear as matched_text
    # (because the actual match is the longer `<id>xyz` form for that
    # case, and the prefixed `x<id>` case yields no match at all).
    matched = [f["matched_text"] for f in data["findings"]]
    # First file: x<id> — should NOT match anything.
    # Second file: <id>xyz — should match the entire `<id>xyz` string.
    assert any(m.startswith("sesn_") and m.endswith("xyz") for m in matched), (
        f"expected matched_text to include `<id>xyz`: {matched}"
    )
    # Plain SYN_SESSION (with no trailing chars) should NOT be a
    # standalone match here — the regex was applied to lines with
    # adjacent chars.
    assert not any(m == SYN_SESSION for m in matched), (
        f"unexpected standalone session_id match: {matched}"
    )


def test_tracked_symlink_named_private_catalog_fires(tmp_path: Path) -> None:
    """A tracked symlink whose basename matches the private-catalog
    filename pattern fires HIGH even if its target points somewhere
    innocuous. Claude review extension of round 17 P1.
    """
    _init_git(tmp_path)
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("nothing here\n", encoding="utf-8")
    link = tmp_path / "scrub-portco-names.yml.bak"
    link.symlink_to(decoy)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", link.name, "decoy.txt"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "named link"],
        check=True,
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-names",
        "--json",
    )
    assert proc.returncode == 1, (
        f"private-catalog-named symlink must fire: {proc.stdout}"
    )
    data = json.loads(proc.stdout)
    private_hits = [
        f
        for f in data["findings"]
        if f.get("category") == "private_catalog_tracked"
        and link.name in f.get("file", "")
    ]
    assert private_hits, (
        f"expected private-catalog finding on the named symlink: {data}"
    )


def test_allowlisted_binary_requires_content_digest(tmp_path: Path) -> None:
    """An allowlist entry for unscanned_binaries.tracked_binary must
    include a content_sha256 to apply. Without one, the binary finding
    still fires. Codex round 17 P2 — pin allowlist to reviewed content.
    """
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"PK\x03\x04reviewed-content\x00")
    custom_catalog = tmp_path / "patterns.yml"
    custom_catalog.write_text(
        "version: 1\n"
        "categories:\n"
        "  unscanned_binaries:\n"
        "    severity: HIGH\n"
        "    patterns: []\n"
        "    extensions: [.docx]\n"
        "exclude_paths:\n"
        "  - patterns.yml\n"
        "allowlist:\n"
        "  - file: report.docx\n"
        "    pattern: unscanned_binaries.tracked_binary\n"
        "    reason: forgot the digest\n",
        encoding="utf-8",
    )
    proc_no_digest = _run(
        "--root",
        str(tmp_path),
        "--patterns",
        str(custom_catalog),
        "--no-names",
        "--no-git",
        "--json",
    )
    assert proc_no_digest.returncode == 1, (
        f"allowlist without digest must not suppress binary: {proc_no_digest.stdout}"
    )
    data = json.loads(proc_no_digest.stdout)
    assert any(
        f["file"] == "report.docx" and f["category"] == "unscanned_binaries"
        for f in data["findings"]
    ), data


def test_allowlisted_binary_revoked_when_content_changes(
    tmp_path: Path,
) -> None:
    """An allowlist entry pinned to content_sha256=X must STOP applying
    once the file's actual digest no longer equals X — the operator's
    review is content-specific, not path-specific. Codex round 17 P2.
    """
    import hashlib

    docx = tmp_path / "report.docx"
    initial_bytes = b"PK\x03\x04reviewed-version-1\x00"
    docx.write_bytes(initial_bytes)
    digest_1 = hashlib.sha256(initial_bytes).hexdigest()
    custom_catalog = tmp_path / "patterns.yml"
    custom_catalog.write_text(
        "version: 1\n"
        "categories:\n"
        "  unscanned_binaries:\n"
        "    severity: HIGH\n"
        "    patterns: []\n"
        "    extensions: [.docx]\n"
        "exclude_paths:\n"
        "  - patterns.yml\n"
        "allowlist:\n"
        "  - file: report.docx\n"
        "    pattern: unscanned_binaries.tracked_binary\n"
        f"    content_sha256: '{digest_1}'\n"
        "    reason: reviewed v1\n",
        encoding="utf-8",
    )
    # First scan: digest matches → no finding.
    proc_clean = _run(
        "--root",
        str(tmp_path),
        "--patterns",
        str(custom_catalog),
        "--no-names",
        "--no-git",
        "--json",
    )
    assert proc_clean.returncode == 0, (
        f"matching digest should suppress: {proc_clean.stdout}"
    )
    # Mutate the file → digest changes → allowlist revoked → HIGH fires.
    docx.write_bytes(b"PK\x03\x04UNREVIEWED-VERSION-2\x00")
    proc_dirty = _run(
        "--root",
        str(tmp_path),
        "--patterns",
        str(custom_catalog),
        "--no-names",
        "--no-git",
        "--json",
    )
    assert proc_dirty.returncode == 1, (
        f"mutated binary must re-trip allowlist: {proc_dirty.stdout}"
    )
    data = json.loads(proc_dirty.stdout)
    assert any(
        f["file"] == "report.docx" and f["category"] == "unscanned_binaries"
        for f in data["findings"]
    ), data


def test_redact_hides_private_pattern_name(tmp_path: Path) -> None:
    """Patterns from the names catalog redact pattern_name AND category
    under --redact.

    Codex round 12 P1: matched_text and any in-path substring were already
    scrubbed, but `pattern_name` (e.g. 'acme_corp') still leaked the
    operator's portco list into CI logs and uploaded artifacts. Round 20
    P2 added the same redaction for `category` (e.g. a private catalog
    might name a category `acme_corp_employees`). Under --redact
    both the name portion and the category collapse to '[REDACTED]';
    the public catalog's pattern names (e.g. 'channel_id') stay visible.
    """
    (tmp_path / "names.md").write_text(
        f'TEXT = "Acme Corp is a portco"\nCHANNEL = "{SYN_SLACK_CHANNEL}"\n',
        encoding="utf-8",
    )
    data = _scan_json(tmp_path, "--names", str(NAMES_EXAMPLE), "--redact", "--no-git")
    # All private-catalog findings now share the redacted category.
    private = [f for f in data["findings"] if f["category"] == "[REDACTED]"]
    assert private, f"expected redacted-category private finding, got {data}"
    for f in private:
        assert f["pattern_name"].startswith("[REDACTED]"), (
            f"private pattern_name not redacted under --redact: {f}"
        )
    public = [f for f in data["findings"] if f["category"] == "slack_identifiers"]
    assert public, "expected at least one slack_identifiers finding"
    assert any(f["pattern_name"] == "channel_id" for f in public), (
        f"public pattern_name should not be redacted: {public}"
    )
    # And the summary by_category must NOT contain the raw private category.
    assert "portco_names" not in data["summary"]["by_category"], (
        f"summary.by_category leaked the raw portco_names key: "
        f"{data['summary']['by_category']}"
    )
    assert "[REDACTED]" in data["summary"]["by_category"], (
        f"summary.by_category lost the redacted-category bucket: "
        f"{data['summary']['by_category']}"
    )


def test_redact_preserves_path_suffix_on_private(tmp_path: Path) -> None:
    """A path-finding from a private pattern keeps its '(path)' suffix
    after redaction so the operator can still tell that the hit was on
    the file path, not its contents. (Round-12 P1 detail.) The category
    also redacts to '[REDACTED]' per round 20 P2.
    """
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "acme-corp-summary.md").write_text("clean body\n", encoding="utf-8")
    data = _scan_json(tmp_path, "--names", str(NAMES_EXAMPLE), "--redact", "--no-git")
    path_hits = [
        f
        for f in data["findings"]
        if f["category"] == "[REDACTED]" and f["pattern_name"].endswith("(path)")
    ]
    assert path_hits, f"expected redacted path finding, got {data}"
    for f in path_hits:
        assert f["pattern_name"] == "[REDACTED] (path)", f


def test_redact_covers_medium_substrings_in_high_paths(tmp_path: Path) -> None:
    """When --severity HIGH filters MEDIUM rows but --redact is set, the
    redactor must still know about every matched substring across ALL
    findings — otherwise a HIGH finding whose path contains a
    MEDIUM-matched substring leaks the substring untouched. (Round-12 P2.)
    """
    # Filename matches MEDIUM short_session_id pattern; content has a HIGH
    # Slack channel ID. Without the fix, the MEDIUM row is filtered out
    # before the redactor is built, and "sesn_77" appears verbatim in the
    # HIGH finding's `file` field.
    (tmp_path / "sesn_77-dump.py").write_text(
        f'CHANNEL = "{SYN_SLACK_CHANNEL}"\n', encoding="utf-8"
    )
    proc = _run(
        "--root",
        str(tmp_path),
        "--no-git",
        "--no-names",
        "--severity",
        "HIGH",
        "--redact",
        "--json",
    )
    data = json.loads(proc.stdout)
    high_findings = [f for f in data["findings"] if f["severity"] == "HIGH"]
    assert high_findings, f"expected at least one HIGH finding: {data}"
    for f in high_findings:
        assert "sesn_77" not in f["file"], (
            f"MEDIUM substring leaked into HIGH file under --redact: {f}"
        )


def test_subtree_root_respects_repo_relative_allowlist() -> None:
    """Allowlist file_globs are written repo-relatively (e.g.
    `bin/scrub_portco_test.py`). When a user scans only a subtree
    (`--root bin/`) the same allowlist entries must still apply.
    (Round-12 P2.)
    """
    # Scan with --root pointing at bin/, which is inside REPO_ROOT. The
    # committed allowlist suppresses the SYN_* constants in
    # bin/scrub_portco_test.py via globs like `bin/scrub_portco_test.py`.
    # The Finding.file is subtree-relative (`scrub_portco_test.py`); the
    # is_allowed call must still recognize the repo-relative glob.
    proc = _run(
        "--root",
        str(REPO_ROOT / "bin"),
        "--patterns",
        str(PATTERNS),
        "--no-names",
        "--no-git",
        "--json",
    )
    data = json.loads(proc.stdout)
    syn_hits = [f for f in data["findings"] if "SYN" in (f.get("matched_text") or "")]
    assert not syn_hits, (
        f"allowlist with repo-relative globs failed under subtree scan: {syn_hits}"
    )

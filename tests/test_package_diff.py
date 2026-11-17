"""Tests for container/cosmic-atomic-nix-diff and scripts/add-package-label.sh.

skopeo, cosign, rpm-ostree and podman are replaced by small fake commands
(see MOCK below), so the tests need no network, registry or container
runtime. Run with:

  nix flake check                          # sandboxed, as CI does
  python3 -m unittest discover -s tests -v # from the repo root
"""

import base64
import contextlib
import gzip
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIFF_TOOL = os.path.join(ROOT, "container", "cosmic-atomic-nix-diff")
LABEL_SCRIPT = os.path.join(ROOT, "scripts", "add-package-label.sh")
CONTAINERFILE = os.path.join(ROOT, "container", "Containerfile")

DIGEST = "sha256:" + "a" * 64
LABEL = "io.github.cosmic-atomic-nix.packages"

LOCAL_LIST = (
    "bash.x86_64\t5.3.0-1.fc45\n"
    "kernel.x86_64\t6.17.0-300.fc45\n"
    "kernel.x86_64\t6.17.1-300.fc45\n"
    "old.noarch\t1.0-1.fc45\n"
    "same.x86_64\t1.0-1.fc45\n"
)
REMOTE_LIST = (
    "bash.x86_64\t5.3.0-2.fc45\n"
    "kernel.x86_64\t6.17.4-300.fc45\n"
    "new.x86_64\t2:1.0-1.fc45\n"
    "same.x86_64\t1.0-1.fc45\n"
)

# One fake for every external command. It logs its arguments as a JSON line
# to $MOCK_LOG and behaves according to MOCK_* environment variables.
MOCK = r'''
import json, os, re, sys
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["MOCK_LOG"], "a") as f:
    f.write(json.dumps([name] + args) + "\n")

def read(var):
    with open(os.environ[var]) as f:
        return f.read()

if name == "skopeo":
    if os.environ.get("MOCK_SKOPEO_FAIL"):
        sys.stderr.write("manifest unknown\n")
        sys.exit(1)
    sys.stdout.write(read("MOCK_SKOPEO_JSON"))
elif name == "cosign":
    if os.environ.get("MOCK_COSIGN_FAIL"):
        sys.stderr.write("Error: no matching signatures\x1b[2J\n")
        sys.exit(1)
    sys.stdout.write("[]\n")
elif name == "nix":
    if os.environ.get("MOCK_NIX_FAIL"):
        sys.stderr.write("error: unable to download nixpkgs\x1b[2J\n")
        sys.exit(1)
    sys.stdout.write(os.environ["MOCK_NIX_OUT"] + "\n")
elif name == "rpm-ostree":
    sys.stdout.write(read("MOCK_RPMOSTREE_JSON"))
elif name == "podman":
    state = os.environ["MOCK_PODMAN_STATE"]
    if args[0] == "run":
        sys.stdout.write(read("MOCK_PODMAN_LIST"))
    elif args[0] == "build":
        cfile = args[args.index("-f") + 1]
        with open(cfile) as f:
            m = re.search(r'^LABEL (\S+)="([^"]*)"$', f.read(), re.M)
        with open(state, "w") as f:
            json.dump({m[1]: m[2]}, f)
    elif args[:2] == ["image", "inspect"]:
        key = re.search(r'index \.Labels "([^"]+)"', args[args.index("--format") + 1])[1]
        with open(state) as f:
            sys.stdout.write(json.load(f).get(key, "") + "\n")
    else:
        sys.exit("unexpected podman call: %r" % args)
else:
    sys.exit("unknown mock " + name)
'''


def load_tool():
    loader = importlib.machinery.SourceFileLoader("cand_diff", DIFF_TOOL)
    spec = importlib.util.spec_from_loader("cand_diff", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def encode(text, compress=None):
    data = (compress or (lambda b: gzip.compress(b, mtime=0)))(text.encode())
    return base64.b64encode(data).decode()


class MockEnv:
    """Temp dir with fake commands, fixtures and a call log."""

    def __init__(self, commands):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.script = os.path.join(self.dir, "mock.py")
        with open(self.script, "w") as f:
            f.write(MOCK)
        for name in commands:
            self.add_command(self.bin, name)
        # What the fake `nix build` prints: a store path with bin/cosign.
        self.nix_out = os.path.join(self.dir, "nix-store-cosign")
        os.makedirs(os.path.join(self.nix_out, "bin"))
        self.add_command(os.path.join(self.nix_out, "bin"), "cosign")
        self.log = self.path("calls.log")
        open(self.log, "w").close()
        self.env = {
            "MOCK_LOG": self.log,
            "MOCK_PODMAN_STATE": self.path("podman-state.json"),
            "MOCK_NIX_OUT": self.nix_out,
        }

    def add_command(self, directory, name):
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\n"
                    f"exec(open({self.script!r}).read())\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def remove_command(self, name):
        os.remove(os.path.join(self.bin, name))

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, content):
        with open(self.path(name), "w") as f:
            f.write(content)
        return self.path(name)

    def calls(self, name=None):
        with open(self.log) as f:
            calls = [json.loads(line) for line in f]
        return [c for c in calls if name is None or c[0] == name]

    def cleanup(self):
        self.tmp.cleanup()


class DiffToolTest(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()
        self.mocks = MockEnv(["skopeo", "cosign", "nix", "rpm-ostree"])
        self.addCleanup(self.mocks.cleanup)
        self.local = self.mocks.write("packages.tsv", LOCAL_LIST)
        self.set_remote(labels={LABEL: encode(REMOTE_LIST)})
        self.set_booted(
            "ostree-image-signed:docker://ghcr.io/me/cosmic-atomic-nix:latest")

    def set_remote(self, labels, digest=DIGEST, created="2026-11-17T04:10:00Z"):
        self.mocks.env["MOCK_SKOPEO_JSON"] = self.mocks.write(
            "skopeo.json",
            json.dumps({"Digest": digest, "Created": created, "Labels": labels}))

    def set_booted(self, ref):
        self.mocks.env["MOCK_RPMOSTREE_JSON"] = self.mocks.write(
            "status.json", json.dumps({"deployments": [
                {"booted": False,
                 "container-image-reference": "ostree-image-signed:docker://x/old:1"},
                {"booted": True, "container-image-reference": ref},
            ]}))

    def run_tool(self, *args, cosign=True, nix=True, env=None):
        """Run main() in-process; returns (exit code, stdout, stderr).

        cosign/nix=False remove that command from PATH.
        """
        if not cosign:
            self.mocks.remove_command("cosign")
        if not nix:
            self.mocks.remove_command("nix")
        environ = {"PATH": self.mocks.bin, **self.mocks.env, **(env or {})}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(self.tool, "LOCAL_LIST", self.local), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.tool.main(["cosmic-atomic-nix-diff", *args])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def assert_failed(self, result, message):
        code, out, err = result
        self.assertEqual(code, 2, (out, err))
        self.assertIn(message, err)
        self.assertNotIn("Changed", out)
        self.assertNotIn("\x1b", out + err)

    # --- normal operation ------------------------------------------------

    def test_verified_diff(self):
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, err)
        self.assertIn("Signature: verified with /etc/pki/containers/cosign.pub", out)
        self.assertIn("Changed (2):\n"
                      "  bash.x86_64 5.3.0-1.fc45 -> 5.3.0-2.fc45\n"
                      "  kernel.x86_64 6.17.0-300.fc45, 6.17.1-300.fc45"
                      " -> 6.17.4-300.fc45\n", out)
        self.assertIn("Removed (1):\n  old.noarch 1.0-1.fc45\n", out)
        self.assertIn("Added (1):\n  new.x86_64 2:1.0-1.fc45\n", out)
        self.assertNotIn("same.x86_64", out)

    def test_follows_booted_image_and_verifies_inspected_digest(self):
        self.run_tool()
        self.assertEqual(self.mocks.calls("skopeo"), [[
            "skopeo", "inspect", "--no-tags",
            "docker://ghcr.io/me/cosmic-atomic-nix:latest"]])
        self.assertEqual(self.mocks.calls("cosign"), [[
            "cosign", "verify", "--key", "/etc/pki/containers/cosign.pub",
            f"ghcr.io/me/cosmic-atomic-nix@{DIGEST}"]])

    def test_explicit_image(self):
        code, _, err = self.run_tool("ghcr.io/me/other:testing")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.mocks.calls("skopeo")[0][-1],
                         "docker://ghcr.io/me/other:testing")
        self.assertEqual(self.mocks.calls("rpm-ostree"), [])

    def test_no_changes(self):
        self.set_remote(labels={LABEL: encode(LOCAL_LIST)})
        code, out, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("No package changes.\n"))

    def test_without_cosign_or_nix_marks_output_unverified(self):
        code, out, err = self.run_tool(cosign=False, nix=False)
        self.assertEqual(code, 0, err)
        self.assertIn("NOT VERIFIED (cosign not found and nix not available)", out)
        self.assertIn("cosign verify --key /etc/pki/containers/cosign.pub "
                      f"ghcr.io/me/cosmic-atomic-nix@{DIGEST}", out)
        self.assertIn("Changed (2):", out)

    # --- cosign from nix ------------------------------------------------------

    def test_cosign_on_path_does_not_use_nix(self):
        code, out, err = self.run_tool()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.mocks.calls("nix"), [])
        self.assertNotIn("building nixpkgs#cosign", err)

    def test_cosign_built_with_nix_when_not_on_path(self):
        code, out, err = self.run_tool(cosign=False)
        self.assertEqual(code, 0, err)
        self.assertIn("building nixpkgs#cosign with nix", err)
        self.assertEqual(self.mocks.calls("nix"), [[
            "nix", "build", "--no-link", "--print-out-paths",
            "nixpkgs#cosign"]])
        # The cosign from the nix output path did the verification.
        self.assertEqual(self.mocks.calls("cosign"), [[
            "cosign", "verify", "--key", "/etc/pki/containers/cosign.pub",
            f"ghcr.io/me/cosmic-atomic-nix@{DIGEST}"]])
        self.assertIn("Signature: verified", out)
        self.assertIn("Changed (2):", out)

    def test_nix_built_cosign_failure_shows_nothing(self):
        code, out, err = self.run_tool(cosign=False, env={"MOCK_COSIGN_FAIL": "1"})
        self.assert_failed((code, out, err), "signature verification failed")
        self.assertEqual(out, "")

    def test_nix_build_failure_marks_output_unverified(self):
        code, out, err = self.run_tool(cosign=False, env={"MOCK_NIX_FAIL": "1"})
        self.assertEqual(code, 0, err)
        self.assertIn("NOT VERIFIED (nix build nixpkgs#cosign failed)", out)
        self.assertIn("unable to download nixpkgs", err)
        self.assertNotIn("\x1b", out + err)
        self.assertEqual(self.mocks.calls("cosign"), [])

    def test_nix_output_without_cosign_marks_output_unverified(self):
        empty = self.mocks.path("empty-out")
        os.mkdir(empty)
        code, out, err = self.run_tool(cosign=False, env={"MOCK_NIX_OUT": empty})
        self.assertEqual(code, 0, err)
        self.assertIn("NOT VERIFIED (nix build nixpkgs#cosign did not produce "
                      "bin/cosign)", out)

    def test_nix_not_used_for_non_registry_image(self):
        self.run_tool("containers-storage:localhost/x:latest", cosign=False)
        self.assertEqual(self.mocks.calls("nix"), [])

    def test_non_registry_image_is_unverified(self):
        code, out, _ = self.run_tool("containers-storage:localhost/x:latest")
        self.assertEqual(code, 0)
        self.assertIn("NOT VERIFIED (not a registry image)", out)
        self.assertEqual(self.mocks.calls("cosign"), [])

    def test_reference_conversion(self):
        cases = {
            "ostree-image-signed:docker://ghcr.io/a/b:latest":
                "docker://ghcr.io/a/b:latest",
            "ostree-unverified-image:docker://ghcr.io/a/b:1":
                "docker://ghcr.io/a/b:1",
            "ostree-unverified-registry:quay.io/a/b:45":
                "docker://quay.io/a/b:45",
            "ostree-remote-registry:fedora:quay.io/a/b:45":
                "docker://quay.io/a/b:45",
            "ostree-remote-image:fedora:docker://quay.io/a/b:45":
                "docker://quay.io/a/b:45",
            "ostree-unverified-image:containers-storage:localhost/x:1":
                "containers-storage:localhost/x:1",
            "ghcr.io/a/b": "docker://ghcr.io/a/b",
            "-rf": "docker://-rf",
        }
        for ref, want in cases.items():
            self.assertEqual(self.tool.to_skopeo_ref(ref), want, ref)

    def test_repository(self):
        cases = {
            "docker://ghcr.io/a/b": "ghcr.io/a/b",
            "docker://ghcr.io/a/b:latest": "ghcr.io/a/b",
            f"docker://ghcr.io/a/b@{DIGEST}": "ghcr.io/a/b",
            "docker://localhost:5000/a/b:tag": "localhost:5000/a/b",
            f"docker://localhost:5000/a/b@{DIGEST}": "localhost:5000/a/b",
        }
        for ref, want in cases.items():
            self.assertEqual(self.tool.repository(ref), want, ref)

    # --- errors ----------------------------------------------------------

    def test_digest_pinned_booted_image(self):
        self.set_booted(f"ostree-image-signed:docker://ghcr.io/a/b@{DIGEST}")
        self.assert_failed(self.run_tool(), "pinned to a digest")

    def test_missing_label(self):
        self.set_remote(labels={"other": "x"})
        self.assert_failed(self.run_tool(), "has no " + LABEL)

    def test_skopeo_failure(self):
        self.assert_failed(self.run_tool(env={"MOCK_SKOPEO_FAIL": "1"}),
                           "skopeo inspect")

    def test_invalid_local_list(self):
        self.local = self.mocks.write("bad-local.tsv", "not a package line\n")
        self.assert_failed(self.run_tool(), "not a valid package entry")

    # --- 1: signature verification ----------------------------------------

    def test_failed_signature_shows_nothing(self):
        code, out, err = self.run_tool(env={"MOCK_COSIGN_FAIL": "1"})
        self.assert_failed((code, out, err), "signature verification failed")
        self.assertIn("\\x1b[2J", err)  # cosign's stderr is escaped too
        self.assertEqual(out, "")

    def test_signature_checked_before_label_is_decoded(self):
        # A forged, malformed label must be rejected for its signature, not
        # parsed first.
        self.set_remote(labels={LABEL: encode("evil\x1b[2J\tx\n")})
        self.assert_failed(self.run_tool(env={"MOCK_COSIGN_FAIL": "1"}),
                           "signature verification failed")

    def test_invalid_digest_is_not_verified(self):
        self.set_remote(labels={LABEL: encode(REMOTE_LIST)},
                        digest="sha256:zz\x1b[2J")
        self.assert_failed(self.run_tool(), "invalid digest")
        self.assertEqual(self.mocks.calls("cosign"), [])

    # --- 3: size limits -----------------------------------------------------

    def test_gzip_bomb(self):
        bomb = encode("\0" * (64 * 1024 * 1024),
                      compress=lambda b: gzip.compress(b, 9, mtime=0))
        self.assertLess(len(bomb), 200_000)
        self.set_remote(labels={LABEL: bomb})
        start = time.monotonic()
        self.assert_failed(self.run_tool(), "expands to more than")
        self.assertLess(time.monotonic() - start, 10)

    def test_oversized_encoded_label(self):
        self.set_remote(labels={LABEL: "A" * (self.tool.MAX_ENCODED + 4)})
        self.assert_failed(self.run_tool(), "label is too large")

    def test_trailing_data(self):
        raw = gzip.compress(REMOTE_LIST.encode(), mtime=0) + b"junk"
        self.set_remote(labels={LABEL: base64.b64encode(raw).decode()})
        self.assert_failed(self.run_tool(), "not a single complete gzip stream")

    def test_concatenated_gzip_streams(self):
        part = gzip.compress(REMOTE_LIST.encode(), mtime=0)
        self.set_remote(labels={LABEL: base64.b64encode(part + part).decode()})
        self.assert_failed(self.run_tool(), "not a single complete gzip stream")

    def test_truncated_gzip(self):
        raw = gzip.compress(REMOTE_LIST.encode(), mtime=0)[:-8]
        self.set_remote(labels={LABEL: base64.b64encode(raw).decode()})
        self.assert_failed(self.run_tool(), "not a single complete gzip stream")

    def test_not_base64(self):
        self.set_remote(labels={LABEL: "!!!not base64"})
        self.assert_failed(self.run_tool(), "cannot decode")

    def test_not_gzip(self):
        self.set_remote(labels={LABEL: base64.b64encode(b"plain").decode()})
        self.assert_failed(self.run_tool(), "cannot decode")

    # --- 4: escape sequences ------------------------------------------------

    def test_escape_sequence_in_label_rejects_list(self):
        self.set_remote(labels={LABEL: encode(
            REMOTE_LIST + "evil\x1b[2K.x86_64\t1-1\n")})
        code, out, err = self.run_tool()
        self.assert_failed((code, out, err), "line 5 is not a valid package entry")
        self.assertIn("\\x1b[2K", err)

    def test_invalid_entries_rejected(self):
        bad_lines = [
            "bash.x86_64 5.3.0-1.fc45",       # space instead of tab
            "bash.x86_64\t5.3.0",             # no release
            "bash.x86_64\t5.3.0-1-\x07",      # control character
            "bash.x86_64\tx:5.3.0-1",         # non-numeric epoch
            "b\u00e4sh.x86_64\t5.3.0-1",      # non-ASCII
            "\t5.3.0-1",                      # no name
        ]
        for line in bad_lines:
            with self.subTest(line=line):
                self.mocks.env.pop("MOCK_COSIGN_FAIL", None)
                self.set_remote(labels={LABEL: encode(REMOTE_LIST + line + "\n")})
                code, _, err = self.run_tool()
                self.assertEqual(code, 2, err)

    def test_escape_sequence_in_created_is_escaped(self):
        self.set_remote(labels={LABEL: encode(REMOTE_LIST)},
                        created="\x1b[31mRED\x1b[0m\nSignature: verified")
        code, out, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertIn("Created:   \\x1b[31mRED\\x1b[0m\\nSignature: verified\n", out)


class LabelScriptTest(unittest.TestCase):
    def setUp(self):
        self.mocks = MockEnv(["podman"])
        self.addCleanup(self.mocks.cleanup)

    def run_script(self, package_list, *args):
        self.mocks.env["MOCK_PODMAN_LIST"] = self.mocks.write("list.tsv", package_list)
        env = {**os.environ, **self.mocks.env,
               "PATH": self.mocks.bin + os.pathsep + os.environ.get("PATH", "")}
        args = args or ("ghcr.io/me/cosmic-atomic-nix:latest",)
        return subprocess.run(["bash", LABEL_SCRIPT, *args], env=env,
                              capture_output=True, text=True)

    def label(self):
        with open(self.mocks.env["MOCK_PODMAN_STATE"]) as f:
            return json.load(f)[LABEL]

    def test_label_round_trips_through_diff_tool(self):
        result = self.run_script(REMOTE_LIST)
        self.assertEqual(result.returncode, 0, result.stderr)
        tool = load_tool()
        self.assertEqual(tool.decode_label(self.label()), REMOTE_LIST)
        self.assertIn("Added io.github.cosmic-atomic-nix.packages label", result.stdout)

    def test_rebuilds_from_local_image_only(self):
        self.run_script(REMOTE_LIST)
        builds = [c for c in self.mocks.calls("podman") if c[1] == "build"]
        self.assertEqual(len(builds), 1)
        self.assertIn("--pull=never", builds[0])
        self.assertEqual(builds[0][builds[0].index("-t") + 1],
                         "ghcr.io/me/cosmic-atomic-nix:latest")

    def test_label_is_deterministic(self):
        self.run_script(REMOTE_LIST)
        first = self.label()
        self.run_script(REMOTE_LIST)
        self.assertEqual(self.label(), first)
        # gzip -n: no file name flag (FLG byte 3) and a zero MTIME (bytes 4-7),
        # so the label does not depend on when the build ran.
        header = base64.b64decode(first)[:8]
        self.assertEqual(header[3] & 0x08, 0)
        self.assertEqual(header[4:8], b"\0\0\0\0")

    def test_rejects_invalid_entries_before_building(self):
        result = self.run_script(REMOTE_LIST + "evil\x1b[2K.x86_64\t1-1\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected package entries", result.stderr)
        self.assertFalse([c for c in self.mocks.calls("podman") if c[1] == "build"])

    def test_rejects_empty_list(self):
        result = self.run_script("")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty", result.stderr)

    def test_requires_image_argument(self):
        self.assertNotEqual(self.run_script(REMOTE_LIST, "a", "b").returncode, 0)
        self.assertEqual(self.mocks.calls("podman"), [])


class ConsistencyTest(unittest.TestCase):
    """The three files must agree on the label name, path and format."""

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_label_name_matches(self):
        tool = load_tool()
        script = re.search(r'^label="([^"]+)"$', self.read(LABEL_SCRIPT), re.M)[1]
        self.assertEqual(tool.LABEL, script)
        self.assertEqual(tool.LABEL, LABEL)

    def test_list_path_matches(self):
        tool = load_tool()
        script = re.search(r'^list_path="([^"]+)"$', self.read(LABEL_SCRIPT), re.M)[1]
        self.assertEqual(tool.LOCAL_LIST, script)
        self.assertIn(f"> {tool.LOCAL_LIST}", self.read(CONTAINERFILE))

    def test_containerfile_writes_expected_format(self):
        self.assertIn(r"rpm -qa --qf '%{NAME}.%{ARCH}\t%{EVR}\n'",
                      self.read(CONTAINERFILE))

    def test_shell_and_python_line_formats_agree(self):
        tool = load_tool()
        script = re.search(r"grep -Evn \$'([^']+)'", self.read(LABEL_SCRIPT))[1]
        shell_re = re.compile(script.replace("\\t", "\t"))
        samples = [
            "bash.x86_64\t5.3.0-1.fc45", "new.x86_64\t2:1.0-1.fc45",
            "python3.13-libs.x86_64\t3.13.1-1.fc45", "a+b.noarch\t1.0~rc1^g1-1",
            "bad line", "x\t1", "evil\x1b.x\t1-1", "x\ta:1-1",
        ]
        for line in samples:
            with self.subTest(line=line):
                self.assertEqual(bool(shell_re.fullmatch(line)),
                                 bool(tool.LINE_RE.fullmatch(line)))


if __name__ == "__main__":
    unittest.main()

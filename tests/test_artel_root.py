"""The root wrapper (artel/data/artel-root.sh): run as the current user with fake apt-get / apparmor_parser on PATH
(ARTEL_ROOT_TEST_BIN, a hook the installer strips from the installed copy)."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "artel" / "data" / "artel-root.sh"
SUDOERS = ROOT / "artel" / "data" / "artel-sudoers.tpl"
SH = shutil.which("sh")
posix_only = pytest.mark.skipif(os.name == "nt" or not SH, reason="POSIX sh wrapper")


@pytest.fixture
def box(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    calls = tmp_path / "calls.log"
    for name, body in {
        "apt-get": 'echo "apt-get $*" >> "$CALLS"\n[ "${FAKE_APT_FAIL:-}" = 1 ] && [ "$1" = install ] && exit 100\nexit 0\n',
        "apparmor_parser": 'echo "apparmor_parser $*" >> "$CALLS"\nexit 0\n',
    }.items():
        p = bin_ / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
    (tmp_path / "abi").mkdir()
    (tmp_path / "abi" / "4.0").write_text("# abi\n")
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\n")
    fake_bwrap.chmod(0o755)
    env = {**os.environ, "ARTEL_ROOT_TEST_BIN": str(bin_), "ARTEL_ROOT_TEST_CONF": str(tmp_path / "artel-root.conf"),
           "ARTEL_ROOT_TEST_PROFILE": str(tmp_path / "artel-bwrap"), "ARTEL_ROOT_TEST_ABI": str(tmp_path / "abi" / "4.0"),
           "ARTEL_ROOT_TEST_BWRAP": str(fake_bwrap), "CALLS": str(calls)}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)

    def run(*args, **over):
        return subprocess.run([SH, str(SCRIPT), *args], env={**env, **over}, capture_output=True, text=True, timeout=30)

    run.calls = lambda: calls.read_text().splitlines() if calls.exists() else []
    run.tmp = tmp_path
    return run


@posix_only
def test_script_is_valid_posix_sh():
    assert subprocess.run([SH, "-n", str(SCRIPT)], capture_output=True).returncode == 0


@posix_only
def test_apt_install_runs_exactly_the_fixed_command(box):
    r = box("apt-install", "ripgrep", "g++", "libstdc++6", "python3.11-venv")
    assert r.returncode == 0, r.stderr
    assert box.calls() == ["apt-get install -y --no-install-recommends --no-remove -- ripgrep g++ libstdc++6 python3.11-venv"]


@posix_only
@pytest.mark.parametrize("bad", [
    "x", "", "-o", "--", "-oAPT::Update::Pre-Invoke::=sh", "Rg", "ripgrep;id", "a b", "a\nb", "ripgrep\n--purge", "$(id)",
    "`id`", "a&b", "a|b", "../x", "/etc/passwd", "pkg-", "foo.deb", ".hidden", "+x", "-", "a=1.0", "a/stable", "ünï"])
def test_apt_install_rejects_injection(box, bad):
    r = box("apt-install", "ripgrep", bad)
    assert r.returncode != 0
    assert box.calls() == [], f"{bad!r} reached apt-get"


@posix_only
def test_apt_install_rejects_option_after_a_valid_name_and_empty_list(box):
    assert box("apt-install").returncode != 0
    assert box("apt-install", "ripgrep", "--allow-unauthenticated").returncode != 0
    assert box.calls() == []


@posix_only
@pytest.mark.parametrize("name", ["openssh-server", "nginx-server", "foo-daemon", "sudo", "sudo-ldap", "apparmor-utils",
                                  "apparmor", "docker.io", "postgresql-16", "systemd", "linux-image-generic"])
def test_deny_list_builtin(box, name):
    r = box("apt-install", name)
    assert r.returncode != 0 and "denied" in r.stderr
    assert box.calls() == []


@posix_only
def test_deny_list_from_root_config(box):
    conf = box.tmp / "artel-root.conf"
    conf.write_text("# comment\ndeny=ffmpeg\ndeny=lib*-secret\n")
    assert box("apt-install", "ffmpeg").returncode != 0
    assert box("apt-install", "libfoo-secret").returncode != 0
    assert box("apt-install", "ripgrep").returncode == 0
    assert box.calls() == ["apt-get install -y --no-install-recommends --no-remove -- ripgrep"]


@posix_only
def test_config_cannot_inject_commands(box):
    (box.tmp / "artel-root.conf").write_text("deny=$(touch %s/pwned)\nrun=touch %s/pwned2\n" % (box.tmp, box.tmp))
    assert box("apt-install", "ripgrep").returncode == 0
    assert not (box.tmp / "pwned").exists() and not (box.tmp / "pwned2").exists()


@posix_only
def test_update_and_retry_once_when_install_fails_first(box, tmp_path):
    fail_once = box.tmp / "bin" / "apt-get"
    fail_once.write_text('#!/bin/sh\necho "apt-get $*" >> "$CALLS"\n'
                         'if [ "$1" = install ] && [ ! -e "$CALLS.seen" ]; then : > "$CALLS.seen"; exit 100; fi\nexit 0\n')
    r = box("apt-install", "ripgrep")
    assert r.returncode == 0
    assert [c.split()[1] for c in box.calls()] == ["install", "update", "install"]


@posix_only
def test_apt_install_failure_is_reported(box):
    r = box("apt-install", "ripgrep", FAKE_APT_FAIL="1")
    assert r.returncode != 0


@posix_only
def test_unknown_action_and_no_args(box):
    assert box().returncode != 0
    for bad in ("remove", "purge", "apt-remove", "apt-get", "sh", "apt-install;id", "update", "--help;id"):
        r = box(bad, "ripgrep")
        assert r.returncode != 0
    assert box.calls() == []


@posix_only
def test_apparmor_bwrap_writes_profile_and_reloads(box):
    r = box("apparmor-bwrap")
    assert r.returncode == 0, r.stderr
    profile = (box.tmp / "artel-bwrap").read_text()
    assert "abi <abi/4.0>," in profile and "include <tunables/global>" in profile
    assert "/bwrap flags=(unconfined)" in profile and "  userns," in profile
    assert "include if exists <local/artel-bwrap>" in profile
    assert box.calls() == [f"apparmor_parser -r {box.tmp / 'artel-bwrap'}"]
    assert box("apparmor-bwrap").returncode == 0  # idempotent


@posix_only
def test_apparmor_bwrap_takes_no_arguments_and_needs_abi4(box):
    assert box("apparmor-bwrap", "extra").returncode != 0
    (box.tmp / "abi" / "4.0").unlink()  # older AppArmor: userns is not restricted, nothing to do
    r = box("apparmor-bwrap")
    assert r.returncode == 0 and not (box.tmp / "artel-bwrap").exists() and box.calls() == []


@posix_only
def test_installed_copy_has_no_test_hook(tmp_path):
    out = subprocess.run([SH, "-c", f"sed '/TESTHOOK-BEGIN/,/TESTHOOK-END/d' {SCRIPT}"], capture_output=True, text=True).stdout
    assert "ARTEL_ROOT_TEST" not in out and "apt-install" in out
    p = tmp_path / "installed.sh"
    p.write_text(out)
    assert subprocess.run([SH, "-n", str(p)], capture_output=True).returncode == 0


def test_sudoers_line_allows_exactly_the_wrapper():
    text = SUDOERS.read_text()
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    assert "@USER@ ALL=(root) NOPASSWD: /usr/local/sbin/artel-root *" in lines
    assert all("ALL=(ALL)" not in l and "NOPASSWD: ALL" not in l for l in lines)
    assert [l for l in lines if "NOPASSWD" in l] == ["@USER@ ALL=(root) NOPASSWD: /usr/local/sbin/artel-root *"]


@posix_only
@pytest.mark.skipif(not shutil.which("visudo"), reason="visudo not installed")
def test_sudoers_rendered_passes_visudo(tmp_path):
    f = tmp_path / "artel"
    f.write_text(SUDOERS.read_text().replace("@USER@", "alice"))
    f.chmod(0o440)
    r = subprocess.run(["visudo", "-cf", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout


@posix_only
def test_wrapper_is_not_executable_in_repo_or_is_installed_with_mode(tmp_path):
    # the repo copy is data; install.sh installs it root-owned 0755 (checked in test_install_script)
    assert not (SCRIPT.stat().st_mode & stat.S_IWOTH)


INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")


def test_installer_asks_once_defaults_to_no_and_removes_both_files():
    assert "Разрешить Artel ставить системные пакеты (apt) и профиль AppArmor для песочницы?" in INSTALL
    ask = INSTALL[INSTALL.index("ask_root() {"):INSTALL.index("root_remove() {")]
    assert "ARTEL_ALLOW_ROOT" in ask and "[y/N]" in ask and "else return 1" in ask  # no terminal: no
    assert "install -m 0755 -o root -g root" in INSTALL and "visudo -cf" in INSTALL and "TESTHOOK-BEGIN" in INSTALL
    remove = INSTALL[INSTALL.index("root_remove() {"):INSTALL.index("root_setup() {")]
    assert "$ROOT_SUDOERS" in remove and "$ROOT_WRAPPER" in remove
    prep = INSTALL[INSTALL.index("reinstall_prepare() {"):INSTALL.index("# --- root wrapper")]
    assert "root_remove" not in prep  # reinstall keeps what the owner consented to; see test_reinstall.py
    assert "--remove-root" in INSTALL

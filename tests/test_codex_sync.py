"""在临时目录中验证同步部署，不连接网络或修改真实配置。"""

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "codex/skills/zsh-codex-sync/scripts/sync.py"
SPEC = importlib.util.spec_from_file_location("codex_sync", HELPER)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
REAL_RUN = subprocess.run
GIT = shutil.which("git")


class CodexSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zsh-codex-sync-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repository = self.base / "source repo"
        self.source = self.repository / "codex"
        (self.source / "skills").mkdir(parents=True)
        (self.source / "AGENTS.md").write_text("全局指令\n")
        self.target = self.base / "target codex"
        self.output = io.StringIO()
        self.stdout_patch = redirect_stdout(self.output)
        self.stdout_patch.__enter__()
        self.addCleanup(self.stdout_patch.__exit__, None, None, None)
        self.env_patch = mock.patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def add_skill(self, name="示例 skill"):
        skill = self.source / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: example\ndescription: 示例\n---\n指令\n")
        return skill

    def git(self, directory, *args):
        return REAL_RUN([GIT, "-C", str(directory), *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, directory, message="测试提交"):
        self.git(directory, "add", ".")
        self.git(directory, "-c", "user.name=测试", "-c", "user.email=test@example.invalid", "commit", "-m", message)

    def prepare_git(self):
        self.git(self.repository, "init", "--initial-branch=main")
        self.commit(self.repository)
        upstream = self.base / "upstream"
        self.git(self.base, "clone", str(self.repository), str(upstream))
        self.git(self.repository, "remote", "add", "origin", sync.REPO_URL)
        self.git(self.repository, "fetch", str(upstream), "main:refs/remotes/origin/main")
        self.git(self.repository, "branch", "--set-upstream-to=origin/main")
        return upstream

    def local_network(self, upstream):
        def run(command, **kwargs):
            command = list(command)
            if command[0] == "git" and "clone" in command:
                command[command.index(sync.REPO_URL)] = str(upstream)
                result = REAL_RUN(command, **kwargs)
                self.git(Path(command[-1]), "remote", "set-url", "origin", sync.REPO_URL)
                return result
            if command[0] == "git" and "pull" in command:
                command[1:1] = ["-c", f"url.file://{upstream}.insteadOf={sync.REPO_URL}"]
            return REAL_RUN(command, **kwargs)
        stack = ExitStack()
        stack.enter_context(mock.patch.object(sync.subprocess, "run", side_effect=run))
        stack.enter_context(mock.patch.object(sync, "network_environment", return_value=dict(os.environ)))
        return stack

    def test_local_deployment_and_incremental_skills(self):
        skill = self.add_skill()
        (self.source / "config.toml").write_text("# 配置\n")
        (self.source / "skills" / "incomplete").mkdir()
        sync.deploy(self.repository, self.target)
        self.assertEqual((self.target / "AGENTS.md").resolve(), self.source / "AGENTS.md")
        self.assertEqual((self.target / "config.toml").resolve(), self.source / "config.toml")
        self.assertEqual((self.target / "skills" / skill.name).resolve(), skill)
        self.assertFalse((self.target / "skills" / "incomplete").exists())
        second = self.add_skill("后续技能")
        sync.deploy(self.repository, self.target)
        self.assertTrue((self.target / "skills" / second.name).is_symlink())

    def test_bootstrap_and_invocation_through_installed_skill(self):
        skill = self.source / "skills" / "zsh-codex-sync"
        shutil.copytree(HELPER.parents[1], skill)
        env = dict(os.environ, CODEX_HOME=str(self.target))
        REAL_RUN([sys.executable, str(skill / "scripts" / "sync.py")],
                 cwd=self.base, env=env, capture_output=True, text=True, check=True)
        installed = self.target / "skills" / "zsh-codex-sync"
        self.assertTrue(installed.is_symlink())
        REAL_RUN([sys.executable, str(installed / "scripts" / "sync.py")],
                 cwd=self.base, env=env, capture_output=True, text=True, check=True)
        self.assertEqual((self.target / "AGENTS.md").resolve(), self.source / "AGENTS.md")
        self.assertFalse(list(self.target.rglob("*.backup.*")))

    def test_backs_up_conflicts_and_preserves_other_skills(self):
        skill = self.add_skill()
        (self.target / "skills" / skill.name).mkdir(parents=True)
        (self.target / "skills" / skill.name / "original").write_text("保留")
        (self.target / "skills" / "other").mkdir()
        (self.target / "AGENTS.md").write_text("旧指令")
        sync.deploy(self.repository, self.target)
        backup = next((self.target / "skills").glob(skill.name + ".backup.*"))
        self.assertEqual((backup / "original").read_text(), "保留")
        self.assertEqual(next(self.target.glob("AGENTS.md.backup.*")).read_text(), "旧指令")
        self.assertTrue((self.target / "skills" / "other").is_dir())
        before = sorted(self.target.rglob("*.backup.*"))
        sync.deploy(self.repository, self.target)
        self.assertEqual(before, sorted(self.target.rglob("*.backup.*")))

    def test_backs_up_broken_and_circular_links(self):
        self.target.mkdir()
        link = self.target / "AGENTS.md"
        link.symlink_to("/missing/codex-agents")
        sync.deploy(self.repository, self.target)
        backup = next(self.target.glob("AGENTS.md.backup.*"))
        self.assertEqual(os.readlink(backup), "/missing/codex-agents")
        link.unlink()
        link.symlink_to("AGENTS.md")
        sync.deploy(self.repository, self.target)
        self.assertEqual(link.resolve(), self.source / "AGENTS.md")

    def test_failed_link_restores_previous_item(self):
        self.target.mkdir()
        destination = self.target / "AGENTS.md"
        destination.write_text("保留")
        with mock.patch.object(Path, "symlink_to", side_effect=OSError("模拟权限错误")):
            with self.assertRaises(OSError):
                sync.deploy(self.repository, self.target)
        self.assertEqual(destination.read_text(), "保留")
        self.assertFalse(list(self.target.glob("*.backup.*")))

    def test_rejects_target_inside_source_and_reverse(self):
        for target in (self.source, self.source / "nested", self.repository):
            with self.subTest(target=target), self.assertRaises(ValueError):
                sync.deploy(self.repository, target)

    def test_rejects_skills_directory_linked_back_to_source(self):
        skill = self.add_skill()
        self.target.mkdir()
        (self.target / "skills").symlink_to(self.source / "skills")
        with self.assertRaises(ValueError):
            sync.deploy(self.repository, self.target)
        self.assertFalse(skill.is_symlink())
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertFalse((self.target / "AGENTS.md").exists())

    def test_honors_codex_home(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.target)}):
            sync.deploy(self.repository)
        self.assertTrue((self.target / "AGENTS.md").is_symlink())

    def test_missing_source_leaves_target_untouched(self):
        with self.assertRaises(ValueError):
            sync.deploy(self.base / "missing", self.target)
        self.assertFalse(self.target.exists())

    def test_https_clone_and_fast_forward_update(self):
        upstream = self.prepare_git()
        destination = self.base / "fresh"
        with self.local_network(upstream):
            sync.deploy(destination, self.target, update=True)
            (upstream / "codex" / "new.ini").write_text("# 新配置\n")
            self.commit(upstream)
            sync.deploy(destination, self.target, update=True)
        self.assertEqual((self.target / "new.ini").read_text(), "# 新配置\n")
        self.assertEqual(self.git(destination, "remote", "get-url", "origin"), sync.REPO_URL)

    def test_update_refuses_dirty_and_unrelated_repository(self):
        self.prepare_git()
        (self.repository / "local-file").touch()
        with self.assertRaisesRegex(ValueError, "本地修改"):
            sync.deploy(self.repository, self.target, update=True)
        self.assertFalse(self.target.exists())
        (self.repository / "local-file").unlink()
        self.git(self.repository, "remote", "set-url", "origin", "https://example.invalid/other.git")
        with self.assertRaisesRegex(ValueError, "origin"):
            sync.deploy(self.repository, self.target, update=True)

    def test_update_refuses_diverged_history(self):
        upstream = self.prepare_git()
        (self.repository / "local").touch()
        self.commit(self.repository, "本地提交")
        (upstream / "remote").touch()
        self.commit(upstream, "远程提交")
        with self.local_network(upstream), self.assertRaises(subprocess.CalledProcessError):
            sync.deploy(self.repository, self.target, update=True)
        self.assertTrue((self.repository / "local").exists())
        self.assertFalse(self.target.exists())

    def run_remote(self, password=None, fail=False):
        temporary_paths = []
        secret = "空 格'\"$;`\\密码" if password else None
        args = type("Args", (), dict(host="192.0.2.1", user="alice", password=secret,
                                     source=str(self.repository), target=str(self.target), update=False))()

        def run(command, **kwargs):
            self.assertEqual(command[0], "ssh")
            self.assertIn("StrictHostKeyChecking=accept-new", command)
            self.assertNotIn("-p", command)
            if secret:
                self.assertNotIn(secret, " ".join(command))
                self.assertTrue(kwargs["start_new_session"])
                env = kwargs["env"]
                password_file = Path(env["CODEX_SYNC_PASSWORD_FILE"])
                temporary_paths.append(password_file.parent)
                self.assertEqual(password_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(password_file.parent.stat().st_mode & 0o777, 0o700)
                actual = REAL_RUN([env["SSH_ASKPASS"]], env=env, capture_output=True, text=True, check=True).stdout
                self.assertEqual(actual, secret + "\n")
            if fail:
                raise subprocess.CalledProcessError(255, command)
            remote_command = shlex.split(command[-1])
            self.assertEqual(remote_command[:2], ["python3", "-c"])
            return REAL_RUN([sys.executable, "-c", remote_command[-1]],
                            input=kwargs["input"], text=True, capture_output=True, check=True)

        with mock.patch.object(sync.subprocess, "run", side_effect=run):
            if fail:
                with self.assertRaises(subprocess.CalledProcessError):
                    sync.deploy_remote(args)
            else:
                sync.deploy_remote(args)
        for path in temporary_paths:
            self.assertFalse(path.exists())

    def test_remote_executes_transmitted_helper(self):
        self.run_remote()
        self.assertTrue((self.target / "AGENTS.md").is_symlink())

    def test_remote_password_and_cleanup(self):
        self.run_remote(password=True)
        self.assertTrue((self.target / "AGENTS.md").is_symlink())

    def test_remote_failure_also_cleans_credentials(self):
        self.run_remote(password=True, fail=True)
        self.assertFalse(self.target.exists())

    def test_cli_rejects_incomplete_connection(self):
        for args in (("--host", "example.com"), ("--port", "22"),
                     ("--host", "", "--user", "")):
            with self.subTest(args=args):
                result = REAL_RUN([sys.executable, str(HELPER), *args], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)

    def test_network_fallback_is_scoped_to_subprocesses(self):
        original = dict(os.environ)
        with mock.patch.object(sync, "probe_network", side_effect=[OSError("无法直连"), None]) as probe:
            env = sync.network_environment()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(env["https_proxy"], "http://192.168.1.56:7890")
        self.assertEqual(env["NO_PROXY"], "")
        self.assertEqual(dict(os.environ), original)

    def test_proxy_probe_preserves_github_host_inside_https_tunnel(self):
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        with mock.patch.object(sync.urllib.request, "build_opener", return_value=opener):
            sync.probe_network(sync.proxy_environment())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.host, "192.168.1.56:7890")
        self.assertEqual(request.get_header("Host"), "github.com")
        self.assertEqual(request._tunnel_host, "github.com")

    def test_working_network_does_not_use_fallback_proxy(self):
        with mock.patch.object(sync, "probe_network") as probe:
            env = sync.network_environment()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(env, dict(os.environ))

    def test_both_network_routes_fail_without_installation(self):
        with mock.patch.object(sync.shutil, "which", return_value=None), \
                mock.patch.object(sync, "probe_network", side_effect=OSError("不可达")), \
                mock.patch.object(sync.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "备用代理均无法"):
                sync.update_repository(self.repository, "secret")
        run.assert_not_called()

    def test_install_git_uses_sudo_password_and_proxy_for_apt(self):
        installed = False
        commands = []

        def which(name):
            return None if name == "git" and not installed else "/usr/bin/" + name

        def run(command, **kwargs):
            nonlocal installed
            commands.append(command)
            self.assertEqual(command[:5], ["sudo", "-S", "-p", "", "--"])
            self.assertEqual(kwargs["input"], "secret\n")
            self.assertNotIn("secret", " ".join(command))
            self.assertIn("Acquire::https::Proxy=http://192.168.1.56:7890", command)
            if "install" in command:
                installed = True
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(sync.os, "geteuid", return_value=1000), \
                mock.patch.object(sync.shutil, "which", side_effect=which), \
                mock.patch.object(sync.subprocess, "run", side_effect=run):
            sync.install_git("secret", sync.proxy_environment())
        self.assertTrue(installed)
        self.assertEqual(len(commands), 2)
        self.assertIn("update", commands[0])
        self.assertIn("ca-certificates", commands[1])

    def test_existing_git_does_not_install_packages(self):
        with mock.patch.object(sync.shutil, "which", return_value="/usr/bin/git"), \
                mock.patch.object(sync.subprocess, "run") as run:
            sync.install_git(None, dict(os.environ))
        run.assert_not_called()

    def test_install_failure_stops_before_clone(self):
        def which(name):
            return None if name == "git" else "/usr/bin/" + name

        with mock.patch.object(sync.shutil, "which", side_effect=which), \
                mock.patch.object(sync.os, "geteuid", return_value=1000), \
                mock.patch.object(sync, "network_environment", return_value=dict(os.environ)), \
                mock.patch.object(sync.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["sudo"])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                sync.update_repository(self.base / "new", "secret")
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.base / "new").exists())


if __name__ == "__main__":
    unittest.main()

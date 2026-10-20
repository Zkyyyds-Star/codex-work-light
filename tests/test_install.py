"""Run the real installer against an inert temporary package, never user settings.

The disposable Python venv contains an inert fake BLE package. No test installs from pip,
opens Bluetooth, or imports the installed controller.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
STUB_CONTROLLER = '''import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ["install-hooks"], "Installer must only register hooks"
root = Path(os.environ["CODEX_HOME"])
path = root / "hooks.json"
document = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"hooks": {}}
command = str(Path(__file__).resolve()) + " hook"
for name in ["SessionStart", "UserPromptSubmit", "Stop"]:
    groups = document["hooks"].setdefault(name, [])
    groups[:] = [group for group in groups if not group.get("test_aaa_wudi")]
    groups.append({"test_aaa_wudi": True, "hooks": [{"type": "command", "command": command}]})
content = json.dumps(document, ensure_ascii=False)
changed = not path.exists() or path.read_text(encoding="utf-8") != content
path.write_text(content, encoding="utf-8")
print(json.dumps({"changed": changed, "trust_required": changed, "hooks_file": str(path)}))
'''


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell installer")
class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = tempfile.TemporaryDirectory(prefix="aaa installer environment ")
        cls.venv = Path(cls.environment.name) / "专用 Python"
        subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "venv", "--without-pip", str(cls.venv)],
                       check=True, capture_output=True, timeout=40)
        cls.python = cls.venv / "Scripts" / "python.exe"
        metadata = cls.venv / "Lib" / "site-packages" / "bleak-3.0.2.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: bleak\nVersion: 3.0.2\n", encoding="utf-8")
        (metadata.parent / "bleak.py").write_text('"""Inert install-test dependency."""\n', encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.environment.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aaa installer 测试 ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "发布 source"
        self.source.mkdir()
        for name in ("install.ps1", "requirements.txt", "SKILL.md", "README.md", "CHANGES.md"):
            shutil.copyfile(ROOT / name, self.source / name)
        scripts = self.source / "scripts"
        scripts.mkdir()
        (scripts / "status_light.py").write_text(STUB_CONTROLLER, encoding="utf-8")
        (self.source / "agents").mkdir()
        (self.source / "agents" / "openai.yaml").write_text("policy: {}\n", encoding="utf-8")
        self.home = self.directory / "Codex home 空格"
        self.data = self.directory / "本机 data"
        self.target = self.home / "skills" / "aaa-wudi"
        self.env = dict(os.environ, PIP_NO_INDEX="1", PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("CODEX_HOME", None)

    def run_install(self, *arguments, source=None, skip=True):
        command = [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str((source or self.source) / "install.ps1"),
                   "-CodexHome", str(self.home)]
        if skip:
            command.append("-SkipDependencies")
        command.extend(arguments)
        return subprocess.run(command, capture_output=True, encoding="utf-8-sig", errors="replace",
                              env=self.env, timeout=30)

    def install(self, *arguments, skip=True):
        result = self.run_install("-Python", str(self.python), "-DeviceAddress", "AA:BB:CC:DD:EE:FF",
                                  "-DataDir", str(self.data), *arguments, skip=skip)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_first_install_requires_explicit_device_before_writing(self):
        result = self.run_install("-Python", str(self.python), "-DataDir", str(self.data))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DeviceAddress", result.stderr)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.data.exists())

    def test_invalid_address_and_data_inside_package_are_rejected(self):
        for address, data in (("not-a-device", self.data), ("AA:BB:CC:DD:EE:FF", self.source / "data")):
            with self.subTest(address=address, data=data):
                result = self.run_install("-Python", str(self.python), "-DeviceAddress", address, "-DataDir", str(data))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.target.exists())

    def test_unicode_space_install_records_runtime_and_keeps_unrelated_hooks(self):
        self.home.mkdir(parents=True)
        unrelated = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo existing"}]}]}, "custom": 42}
        (self.home / "hooks.json").write_text(json.dumps(unrelated), encoding="utf-8")
        result = self.install()
        settings = json.loads((self.target / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(settings["python"]), self.python)
        self.assertEqual(Path(settings["data_dir"]), self.data)
        self.assertEqual(settings["device_address"], "AA:BB:CC:DD:EE:FF")
        self.assertIsNone(result["backup_dir"])
        document = json.loads((self.home / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(document["custom"], 42)
        self.assertIn(unrelated["hooks"]["Stop"][0], document["hooks"]["Stop"])
        self.assertFalse((self.data / "endpoint.json").exists())
        self.assertTrue((self.target / "scripts" / "status_light.py").exists())
        self.assertTrue((self.target / "CHANGES.md").exists())

    def test_repeat_install_reuses_runtime_and_hooks_and_saves_bounded_backup(self):
        self.install()
        original = (self.target / "runtime.json").read_bytes()
        (self.target / "scripts" / "__pycache__").mkdir()
        (self.target / "scripts" / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second = json.loads(result.stdout)
        self.assertFalse(second["hooks"]["changed"])
        backup = Path(second["backup_dir"])
        self.assertEqual((backup / "runtime.json").read_bytes(), original)
        self.assertFalse((backup / "scripts" / "__pycache__").exists())
        self.assertFalse((backup / "backups").exists())
        document = json.loads((self.home / "hooks.json").read_text(encoding="utf-8"))
        for groups in document["hooks"].values():
            self.assertEqual(sum(bool(group.get("test_aaa_wudi")) for group in groups), 1)

    def test_in_place_install_preserves_source_without_recursive_copy(self):
        self.install()
        result = self.run_install(source=self.target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.target / "install.ps1").exists())
        self.assertFalse((self.target / "aaa-wudi").exists())
        self.assertFalse((self.target / "backups").exists())
        self.assertTrue(Path(json.loads(result.stdout)["backup_dir"]).is_dir())

    def test_install_target_inside_source_is_rejected(self):
        self.home = self.source / "scripts" / "nested-home"
        result = self.run_install("-Python", str(self.python), "-DeviceAddress", "AA:BB:CC:DD:EE:FF",
                                  "-DataDir", str(self.data))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CodexHome", result.stderr)
        self.assertFalse(self.home.exists())

    def test_broken_dependency_does_not_report_success(self):
        module = self.venv / "Lib" / "site-packages" / "bleak.py"
        previous = module.read_text(encoding="utf-8")
        try:
            module.write_text('raise ImportError("broken installed dependency")\n', encoding="utf-8")
            result = self.run_install("-Python", str(self.python), "-DeviceAddress", "AA:BB:CC:DD:EE:FF",
                                      "-DataDir", str(self.data), skip=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.target.exists())
        finally:
            module.write_text(previous, encoding="utf-8")

    def test_old_python_is_rejected_before_creating_installation(self):
        old_python = self.directory / "old-python.cmd"
        old_python.write_text('@echo off\necho {"version":[3,11],"venv":true}\n', encoding="ascii")
        result = self.run_install("-Python", str(old_python), "-DeviceAddress", "AA:BB:CC:DD:EE:FF",
                                  "-DataDir", str(self.data))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("3.12", result.stderr)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.data.exists())

    def test_base_python_creates_a_dedicated_venv(self):
        base_python = str(getattr(sys, "_base_executable", sys.executable))
        result = self.run_install("-Python", base_python, "-DeviceAddress", "AA:BB:CC:DD:EE:FF",
                                  "-DataDir", str(self.data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed_python = Path(json.loads(result.stdout)["runtime_python"])
        self.assertEqual(installed_python, self.data / "venv" / "Scripts" / "python.exe")
        check = subprocess.run([str(installed_python), "-X", "utf8", "-B", "-c",
                                "import sys; assert sys.prefix != sys.base_prefix"], capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_corrupt_existing_configuration_has_clear_error_and_is_preserved(self):
        self.target.mkdir(parents=True)
        invalid = b'{"python": invalid}'
        (self.target / "runtime.json").write_bytes(invalid)
        result = self.run_install("-Python", str(self.python), "-DeviceAddress", "AA:BB:CC:DD:EE:FF",
                                  "-DataDir", str(self.data))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime.json", result.stderr)
        self.assertIn("JSON", result.stderr)
        self.assertEqual((self.target / "runtime.json").read_bytes(), invalid)
        self.assertFalse(self.data.exists())

    def test_production_hooks_install_twice_preserves_other_hooks_without_ble(self):
        scripts = self.source / "scripts"
        shutil.rmtree(scripts)
        shutil.copytree(ROOT / "scripts", scripts, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.home.mkdir(parents=True)
        other_hook = {"type": "command", "command": "echo independent", "timeout": 5}
        original = {"hooks": {"Stop": [{"hooks": [other_hook]}]}, "metadata": {"preserve": True}}
        hook_file = self.home / "hooks.json"
        hook_file.write_text(json.dumps(original), encoding="utf-8")
        config_file = self.home / "config.toml"
        config_file.write_text('# independent settings\nmodel = "example"\n', encoding="utf-8")
        config_bytes = config_file.read_bytes()
        first = self.install()
        self.assertTrue(first["hooks"]["changed"])
        self.assertTrue(first["hooks"]["trust_required"])
        hooks_before = hook_file.read_bytes()
        modified_before = hook_file.stat().st_mtime_ns
        second = self.run_install()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertFalse(json.loads(second.stdout)["hooks"]["changed"])
        self.assertFalse(json.loads(second.stdout)["hooks"]["trust_required"])
        self.assertEqual(hook_file.read_bytes(), hooks_before)
        self.assertEqual(hook_file.stat().st_mtime_ns, modified_before)
        self.assertEqual(config_file.read_bytes(), config_bytes)
        self.assertFalse((self.data / "endpoint.json").exists())
        self.assertFalse((self.data / "controller.log").exists())
        document = json.loads(hooks_before)
        self.assertEqual(document["metadata"], {"preserve": True})
        self.assertIn(other_hook, document["hooks"]["Stop"][0]["hooks"])
        for name in first["hooks"]["events"]:
            registrations = [item for group in document["hooks"][name] for item in group["hooks"]
                             if item.get("statusMessage") == "aaa无敌状态灯"]
            self.assertEqual(len(registrations), 1)
            self.assertIn("powershell.exe", registrations[0]["command"])

    def test_normal_install_verifies_existing_dependency_without_pip_or_network(self):
        result = self.install(skip=False)
        self.assertFalse(result["dependencies_skipped"])
        # 测试 venv 没有 pip；成功证明已存在的固定版本不会触发联网安装。
        check = subprocess.run([str(self.python), "-B", "-m", "pip", "--version"], capture_output=True)
        self.assertNotEqual(check.returncode, 0)


if __name__ == "__main__":
    unittest.main()

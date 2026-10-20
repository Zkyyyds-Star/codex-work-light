"""Exercise generated shell commands using disposable, harmless Python scripts."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import status_light

POWERSHELL = shutil.which("powershell")


def run_shell(command, payload, shell):
    if shell == "powershell":
        source = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);" + command + ";exit $LASTEXITCODE"
        arguments = [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                     base64.b64encode(source.encode("utf-16-le")).decode("ascii")]
    else:
        executable = os.environ.get("COMSPEC", "cmd.exe")
        arguments = f'"{executable}" /d /s /c "{command}"'
    return subprocess.run(arguments, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          capture_output=True, timeout=15)


@unittest.skipUnless(sys.platform == "win32" and POWERSHELL, "Windows PowerShell is required")
class HookCommandTests(unittest.TestCase):
    def test_safe_paths_preserve_existing_command(self):
        executable = r"C:\Python\python.exe"
        script = r"C:\space folder\信号灯.py"
        self.assertEqual(status_light.hook_command(executable, script), f'{executable} -X utf8 -B "{script}" hook')

    def test_invalid_paths_are_rejected(self):
        for executable, script in (("python.exe", r"C:\hook.py"), (sys.executable, "relative.py"),
                                   (r"C:\python.exe" + "\n", r"C:\hook.py"), (sys.executable, 'C:\\a"b.py')):
            with self.subTest(executable=executable, script=script), self.assertRaises(ValueError):
                status_light.hook_command(executable, script)

    def test_encoded_paths_use_literal_quoting(self):
        executable, script = r"C:\中文 空格\python.exe", r"C:\'$env%NAME%!\hook.py"
        command = status_light.hook_command(executable, script)
        self.assertTrue(command.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "))
        source = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")
        self.assertIn("& '" + executable + "'", source)
        self.assertIn("'" + script.replace("'", "''") + "'", source)

    def test_real_shells_preserve_json_stdin_unicode_flags_and_exit_code(self):
        # 本测试只创建无 pip 的临时环境与无害脚本，不调用生产 Hook。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            special = root / "中文 空格 & $%!'环境"
            venv.EnvBuilder(with_pip=False).create(special)
            script_source = ("import json,sys\n"
                             "value=json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))\n"
                             "print(json.dumps(dict(payload=value,utf8=sys.flags.utf8_mode,bytecode=sys.dont_write_bytecode,arg=sys.argv[1]),ensure_ascii=False))\n"
                             "sys.exit(value.get('exit',0))\n")
            for executable, script in ((sys.executable, root / "inert.py"),
                                       (str(special / "Scripts" / "python.exe"), special / "信号灯 '$%!.py")):
                script.write_text(script_source, encoding="utf-8")
                command = status_light.hook_command(executable, script)
                for shell in ("cmd", "powershell"):
                    payload = {"文字": "中文 ' $env:PRIVATE %NAME% !", "exit": 7}
                    with self.subTest(executable=executable, shell=shell):
                        result = run_shell(command, payload, shell)
                        self.assertEqual(result.returncode, 7, result.stderr.decode("utf-8", errors="replace"))
                        output = json.loads(result.stdout.decode("utf-8-sig"))
                        self.assertEqual(output["payload"], payload)
                        self.assertEqual(output["utf8"], 1)
                        self.assertTrue(output["bytecode"])
                        self.assertEqual(output["arg"], "hook")

    def test_install_preserves_unrelated_config_and_replaces_encoded_registration(self):
        executable = r"C:\中文 空格\python.exe"
        with tempfile.TemporaryDirectory() as temporary:
            root, data = Path(temporary) / "codex", Path(temporary) / "runtime"
            root.mkdir()
            data.mkdir()
            unrelated = {"type": "command", "command": "other-notification-command", "timeout": 7}
            lookalike = {"type": "command", "command": "other-script-with-same-label", "statusMessage": "aaa无敌状态灯"}
            old_command = status_light.hook_command(r"C:\previous environment\python.exe")
            document = {"custom": True, "hooks": {"Stop": [{"matcher": "keep", "hooks": [unrelated, lookalike,
                {"type": "command", "command": old_command, "statusMessage": "aaa无敌状态灯"}]}]}}
            path, config = root / "hooks.json", root / "config.toml"
            path.write_text(json.dumps(document), encoding="utf-8")
            original = 'notify = ["existing-notify"]\n[hooks]\ntrusted_hashes = ["existing-trust"]\n'
            config.write_text(original, encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(root)}), \
                    patch.object(status_light, "runtime_config", return_value={"python": executable}), \
                    patch.object(status_light, "data_dir", return_value=data):
                result = status_light.install_hooks()
                repeated = status_light.install_hooks()
            installed = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertTrue(installed["custom"])
            self.assertEqual(installed["hooks"]["Stop"][0], {"matcher": "keep", "hooks": [unrelated, lookalike]})
            registrations = [hook for groups in installed["hooks"].values() for group in groups
                             for hook in group["hooks"] if status_light.is_skill_hook(hook.get("command"))]
            self.assertEqual(len(registrations), 9)
            self.assertTrue(all(item["command"] == status_light.hook_command(executable) for item in registrations))
            self.assertEqual(len(result["events"]), 9)
            self.assertTrue(result["trust_required"])
            self.assertFalse(repeated["changed"])
            self.assertFalse(repeated["trust_required"])
            self.assertEqual(len(list(data.glob("hooks-before-install-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()

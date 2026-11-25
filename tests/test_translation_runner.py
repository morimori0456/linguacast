"""Safe, offline tests for the isolated Antigravity translation runner.

Every CLI invocation in this module uses a synthetic executable. It never runs
agy, contacts a model provider, or reads a real credential.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import translation_runner as runner


class TranslationRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.record = self.root / "invocation.json"
        self.safe_user_home = self.root / "empty-user-home"
        self.safe_user_home.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_agy(self, *, output: str = '', exit_code: int = 0, sleep_seconds: float = 0) -> Path:
        """Create a fake CLI that records only local invocation details."""
        script = self.root / "fake-agy"
        source = f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, time
            record = pathlib.Path({str(self.record)!r})
            stdin = sys.stdin.read()
            argv = sys.argv[1:]
            settings = None
            if '--settings' in argv:
                settings = json.loads(pathlib.Path(argv[argv.index('--settings') + 1]).read_text())
            record.write_text(json.dumps({{
                'argv': argv,
                'stdin': stdin,
                'cwd': os.getcwd(),
                'home': os.environ.get('HOME'),
                'xdg_config': os.environ.get('XDG_CONFIG_HOME'),
                'sentinel_env': os.environ.get('AGY_TEST_SECRET'),
                'api_key_env': os.environ.get('GEMINI_API_KEY'),
                'settings': settings,
            }}))
            time.sleep({sleep_seconds!r})
            sys.stdout.write({output!r})
            sys.stderr.write('synthetic diagnostic that must not escape')
            raise SystemExit({exit_code})
        """
        script.write_text(textwrap.dedent(source), encoding="utf-8")
        script.chmod(0o700)
        return script

    def invoke(self, executable: Path, sentence: str = "Synthetic English sentence.") -> str:
        # Never inspect/copy a real user's Antigravity OAuth token during tests.
        with patch.object(runner.Path, "home", return_value=self.safe_user_home):
            return runner.translate_sentence(sentence, model="gemini-4-hydrogen-medium", executable=str(executable))

    def test_sends_transcript_over_stdin_and_uses_restricted_isolated_cli(self) -> None:
        sentence = "Synthetic transcript must never appear in process arguments."
        output = (
            '{"event":"message","message":{"role":"assistant","content":"intermediate"}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"日本語訳です。"}}\n'
        )
        fake = self.fake_agy(output=output)
        with patch.dict(os.environ, {
            "AGY_TEST_SECRET": "synthetic-secret-must-be-filtered",
            "GEMINI_API_KEY": "synthetic-key-must-be-filtered",
            "PATH": os.environ.get("PATH", ""),
        }, clear=True):
            translated = self.invoke(fake, sentence)

        details = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(translated, "日本語訳です。")
        self.assertNotIn(sentence, json.dumps(details["argv"], ensure_ascii=False))
        self.assertIn(sentence, details["stdin"])
        self.assertEqual(details["sentinel_env"], None)
        self.assertEqual(details["api_key_env"], None)
        self.assertNotEqual(details["home"], str(self.safe_user_home))
        self.assertTrue(details["cwd"].startswith(details["home"]))
        self.assertEqual(details["xdg_config"], str(Path(details["home"]) / ".config"))
        self.assertIn("--model", details["argv"])
        self.assertIn("--sandbox", details["argv"])
        self.assertIn("--input-format", details["argv"])
        self.assertIn("--output-format", details["argv"])
        self.assertNotIn("-p", details["argv"])
        self.assertEqual(details["argv"][details["argv"].index("--input-format") + 1], "stream-json")
        permissions = details["settings"]["permissions"]
        self.assertEqual(permissions["deny"], runner.DENIED_ACTIONS)
        self.assertEqual(permissions["allow"], [])
        self.assertTrue({"*", "command(*)", "read_file(*)", "write_file(*)", "mcp(*)"}.issubset(permissions["deny"]))
        self.assertEqual(details["settings"]["toolPermission"], "strict")
        self.assertFalse(details["settings"]["allowNonWorkspaceAccess"])

    def test_subprocess_is_started_without_shell(self) -> None:
        fake = self.fake_agy(output='{"event":"result","result":{"status":"SUCCESS","response":"訳"}}\n')
        popen = runner.subprocess.Popen
        calls = []

        def capture(*args, **kwargs):
            calls.append((args, kwargs))
            return popen(*args, **kwargs)

        with patch.object(runner.subprocess, "Popen", side_effect=capture):
            self.assertEqual(self.invoke(fake), "訳")
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][1]["shell"], False)
        self.assertIsNotNone(calls[0][1]["stdin"])
        self.assertNotIn("Synthetic English sentence.", calls[0][0][0])

    def test_extracts_successful_terminal_result_ignoring_intermediate_events(self) -> None:
        fake = self.fake_agy(output=(
            '{"event":"message","message":{"role":"assistant","content":"intermediate"}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":" final answer "}}\n'
        ))
        self.assertEqual(self.invoke(fake), "final answer")

    def test_malformed_and_empty_output_are_rejected_without_leaking_payload(self) -> None:
        for output in (
            "not-json",
            '{"event":"result","result":{"status":"ERROR","response":"private payload"}}\n',
            '{"event":"result","result":{"status":"SUCCESS","response":""}}\n',
            '{"event":"result","result":{"status":"SUCCESS","response":"first"}}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"second"}}\n',
            '{"event":"tool_call","tool":"dangerous"}\n'
            '{"event":"result","result":{"status":"SUCCESS","response":"should reject"}}\n',
        ):
            with self.subTest(output=output):
                fake = self.fake_agy(output=output)
                with self.assertRaises(runner.TranslationRunnerError) as raised:
                    self.invoke(fake)
                self.assertNotIn(output, str(raised.exception))
                self.assertNotIn("synthetic diagnostic", str(raised.exception))

    def test_sign_in_failure_is_sanitized_and_does_not_expose_stderr(self) -> None:
        fake = self.fake_agy(exit_code=1, output="sign-in required: synthetic account details")
        with self.assertRaises(runner.TranslationRunnerError) as raised:
            self.invoke(fake)
        self.assertEqual(str(raised.exception), "Translation is unavailable.")
        self.assertNotIn("synthetic", str(raised.exception))

    def test_model_failure_exit_is_sanitized(self) -> None:
        fake = self.fake_agy(exit_code=2, output="model not available: private diagnostic")
        with self.assertRaises(runner.TranslationRunnerError) as raised:
            self.invoke(fake)
        self.assertEqual(str(raised.exception), "Translation is unavailable.")
        self.assertNotIn("private diagnostic", str(raised.exception))

    def test_timeout_terminates_fake_cli(self) -> None:
        fake = self.fake_agy(sleep_seconds=3)
        with patch.object(runner, "DEFAULT_TIMEOUT_SECONDS", 0.1):
            with self.assertRaises(runner.TranslationRunnerError) as raised:
                self.invoke(fake)
        self.assertIn("timed out", str(raised.exception).lower())

    def test_finds_user_local_agy_when_path_lookup_fails(self) -> None:
        fake = self.fake_agy(output='{"event":"result","result":{"status":"SUCCESS","response":"fallback works"}}\n')
        fallback = self.safe_user_home / ".local" / "bin" / "agy"
        fallback.parent.mkdir(parents=True)
        shutil.copyfile(fake, fallback)
        fallback.chmod(0o700)
        with patch.object(runner.shutil, "which", return_value=None), patch.object(runner.Path, "home", return_value=self.safe_user_home):
            result = runner.translate_sentence("Synthetic fallback sentence.")
        self.assertEqual(result, "fallback works")
        details = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertIn("Synthetic fallback sentence.", details["stdin"])

    def test_never_read_stdin_child_is_stopped_by_overall_deadline(self) -> None:
        script = self.root / "fake-agy-never-reads-stdin"
        script.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import fcntl, time
            try:
                fcntl.fcntl(0, 1031, 4096)  # F_SETPIPE_SZ: force a small pipe on Linux.
            except OSError:
                pass
            time.sleep(5)
        """), encoding="utf-8")
        script.chmod(0o700)
        started = time.monotonic()
        with patch.object(runner, "DEFAULT_TIMEOUT_SECONDS", 0.15):
            with self.assertRaises(runner.TranslationRunnerError) as raised:
                self.invoke(script, "x" * 4_000)
        elapsed = time.monotonic() - started
        self.assertIn("timed out", str(raised.exception).lower())
        self.assertLess(elapsed, 2.0, "non-reading child must not block the request thread")

    def test_child_holding_output_pipes_and_ignoring_term_is_killed_with_group(self) -> None:
        child_pid_file = self.root / "child.pid"
        child_code = (
            "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"open({str(child_pid_file)!r},'w').write(str(os.getpid())); time.sleep(30)"
        )
        script = self.root / "fake-agy-with-pipe-holder"
        source = f"""\
            #!/usr/bin/env python3
            import subprocess, sys
            subprocess.Popen([sys.executable, '-c', {child_code!r}])
            sys.stdin.read()
        """
        script.write_text(textwrap.dedent(source), encoding="utf-8")
        script.chmod(0o700)
        started = time.monotonic()
        with patch.object(runner, "DEFAULT_TIMEOUT_SECONDS", 0.15):
            with self.assertRaises(runner.TranslationRunnerError) as raised:
                self.invoke(script)
        elapsed = time.monotonic() - started
        self.assertIn("timed out", str(raised.exception).lower())
        self.assertLess(elapsed, 2.0, "inherited output pipes must not make cleanup hang")
        self.assertTrue(child_pid_file.exists(), "fake child should have started and inherited stdout/stderr")
        pid = int(child_pid_file.read_text(encoding="utf-8"))
        proc_status = Path(f"/proc/{pid}/stat")
        cleanup_deadline = time.monotonic() + 1.0
        while proc_status.exists() and proc_status.read_text(encoding="utf-8").split()[2] != "Z" and time.monotonic() < cleanup_deadline:
            time.sleep(0.02)
        if proc_status.exists():
            # A killed orphan can briefly remain as a zombie until init reaps it.
            self.assertEqual(proc_status.read_text(encoding="utf-8").split()[2], "Z")

    def test_invalid_model_and_missing_executable_fail_closed(self) -> None:
        with self.assertRaises(runner.TranslationRunnerError):
            with patch.object(runner.Path, "home", return_value=self.safe_user_home):
                runner.translate_sentence("Synthetic.", model="model; touch /tmp/unsafe", executable="/bin/true")
        with patch.object(runner.shutil, "which", return_value=None), patch.object(runner.Path, "home", return_value=self.safe_user_home):
            with self.assertRaises(runner.TranslationRunnerError):
                runner.translate_sentence("Synthetic.")

    def test_batch_translation_uses_one_cli_request_and_parses_indexed_json(self) -> None:
        batch = ["Synthetic first sentence.", "Synthetic second sentence."]
        output = json.dumps({"translations": [
            {"index": 0, "translation": "一つ目。"},
            {"index": 1, "translation": "二つ目。"},
        ]}, ensure_ascii=False)
        fake = self.fake_agy(output=(json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": output,
        }}, ensure_ascii=False) + "\n"))
        with patch.object(runner.Path, "home", return_value=self.safe_user_home):
            self.assertEqual(runner.translate_sentences(batch, executable=str(fake)), ["一つ目。", "二つ目。"])
        details = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertIn("Synthetic first sentence.", details["stdin"])
        self.assertIn("Synthetic second sentence.", details["stdin"])
        self.assertNotIn(batch[0], json.dumps(details["argv"], ensure_ascii=False))

    def test_batch_translation_rejects_partial_duplicate_or_extra_items(self) -> None:
        malformed = [
            {"translations": [{"index": 0, "translation": "only one"}]},
            {"translations": [{"index": 0, "translation": "one"}, {"index": 0, "translation": "duplicate"}]},
            {"translations": [{"index": 0, "translation": "one"}, {"index": 1, "translation": "two", "extra": "x"}]},
            {"translations": [{"index": 0, "translation": "one"}, {"index": 2, "translation": "out of range"}]},
        ]
        for payload in malformed:
            event = json.dumps({"event": "result", "result": {
                "status": "SUCCESS", "response": json.dumps(payload, ensure_ascii=False),
            }}, ensure_ascii=False).encode() + b"\n"
            with self.subTest(payload=payload), self.assertRaises(runner.TranslationRunnerError):
                runner._extract_batch_translations(event, 2)

    def test_batch_bounds_input_count_and_total_characters(self) -> None:
        with self.assertRaises(runner.TranslationRunnerError):
            runner.translate_sentences(["x"] * (runner.MAX_BATCH_SENTENCES + 1), executable="/bin/true")
        with self.assertRaises(runner.TranslationRunnerError):
            runner.translate_sentences(["x" * runner.MAX_BATCH_INPUT_CHARS, "y"], executable="/bin/true")


if __name__ == "__main__":
    unittest.main()

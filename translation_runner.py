"""Run one private, tool-disabled Antigravity CLI translation request."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any


DEFAULT_MODEL = "gemini-4-hydrogen-medium"
DEFAULT_TIMEOUT_SECONDS = 75
CLI_PRINT_TIMEOUT_SECONDS = 60
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TRANSLATION_CHARS = 8_000
ALLOWED_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
DENIED_ACTIONS = [
    "*", "command(*)", "read_file(*)", "write_file(*)", "mcp(*)",
    "execute_url(*)", "download_url(*)", "search_web(*)", "read_url(*)",
    "browser(*)", "terminal(*)", "shell(*)", "run_command(*)",
]


class TranslationRunnerError(RuntimeError):
    """A sanitized failure suitable for mapping to a generic HTTP response."""


def build_agy_settings() -> dict[str, Any]:
    """Disable every agent capability; the CLI is only used as a text model."""
    return {
        "toolPermission": "strict",
        "agentMode": "plan",
        "enableTerminalSandbox": True,
        "allowNonWorkspaceAccess": False,
        "historySize": 0,
        "permissions": {"deny": DENIED_ACTIONS, "allow": []},
    }


def _prepare_isolated_home(home: Path) -> tuple[Path, Path]:
    """Build a disposable home/workspace and copy only agy's OAuth token file."""
    source_cli = Path.home() / ".gemini" / "antigravity-cli"
    token = source_cli / "antigravity-oauth-token"

    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    gemini_dir = home / ".gemini"
    cli_dir = gemini_dir / "antigravity-cli"
    workspace = home / "workspace"
    for directory in (gemini_dir, cli_dir, workspace, home / ".config", home / ".cache", home / ".local", home / "tmp"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    # Never follow a symlink, copy a whole profile, or expose credentials in argv.
    try:
        if token.is_symlink():
            raise TranslationRunnerError("Translation is unavailable.")
        if token.is_file():
            import stat

            info = token.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1024 * 1024:
                raise TranslationRunnerError("Translation is unavailable.")
            isolated_token = cli_dir / token.name
            shutil.copyfile(token, isolated_token, follow_symlinks=False)
            os.chmod(isolated_token, 0o600)
    except (OSError, shutil.Error):
        raise TranslationRunnerError("Translation is unavailable.") from None

    settings = home / "settings.json"
    settings.write_text(json.dumps(build_agy_settings(), separators=(",", ":")), encoding="utf-8")
    os.chmod(settings, 0o600)
    return settings, workspace


def _contains_tool_request(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"tool", "tool_call", "toolcall", "function_call", "functioncall", "action"}:
                return True
            if _contains_tool_request(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_tool_request(item) for item in value)
    return False


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    # The group can outlive its leader while holding inherited output pipes.
    # Always send SIGKILL after the grace period, even if proc.wait() returned.
    time.sleep(0.5)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _close_process_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _resolve_agy_executable(executable: str | None = None) -> str | None:
    """Resolve agy via PATH, then its standard per-user install location."""
    if executable:
        return executable
    located = shutil.which("agy")
    if located:
        try:
            return str(Path(located).resolve(strict=True))
        except OSError:
            return None
    fallback = Path.home() / ".local" / "bin" / "agy"
    try:
        resolved = fallback.resolve(strict=True)
    except OSError:
        return None
    if resolved.is_file() and os.access(resolved, os.X_OK):
        return str(resolved)
    return None


def _extract_translation(raw: bytes) -> str:
    """Extract the single successful terminal result from agy's JSONL stream."""
    results: list[str] = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            kind = event.get("event", event.get("type"))
            normalized_kind = kind.lower().replace("-", "_") if isinstance(kind, str) else ""
            if "tool" in normalized_kind or "action" in normalized_kind or "function_call" in normalized_kind:
                raise TranslationRunnerError("Translation could not be completed.")
            # Reject envelopes that indicate any requested tool/action even if
            # the event name changes. Never accept a result after tool activity.
            if _contains_tool_request(event):
                raise TranslationRunnerError("Translation could not be completed.")
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "tool":
                raise TranslationRunnerError("Translation could not be completed.")
            if kind == "result":
                result = event.get("result")
                if not isinstance(result, dict) or result.get("status") != "SUCCESS":
                    raise TranslationRunnerError("Translation could not be completed.")
                response = result.get("response")
                if not isinstance(response, str):
                    raise TranslationRunnerError("Translation could not be completed.")
                results.append(response)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TranslationRunnerError("Translation could not be completed.") from None
    if len(results) != 1:
        raise TranslationRunnerError("Translation could not be completed.")
    translated = results[0].strip()
    if not translated or len(translated) > MAX_TRANSLATION_CHARS:
        raise TranslationRunnerError("Translation could not be completed.")
    return translated


def translate_sentence(
    sentence: str,
    *,
    model: str | None = None,
    executable: str | None = None,
) -> str:
    """Translate a sentence through agy without putting transcript text in argv."""
    selected_model = model or os.environ.get("AGY_TRANSLATION_MODEL", DEFAULT_MODEL)
    if not isinstance(selected_model, str) or not MODEL_RE.fullmatch(selected_model):
        raise TranslationRunnerError("Translation is unavailable.")
    if not isinstance(sentence, str) or not sentence.strip() or len(sentence) > 4_000:
        raise TranslationRunnerError("Translation could not be completed.")
    agy = _resolve_agy_executable(executable)
    if not agy:
        raise TranslationRunnerError("Translation is unavailable.")

    prompt = (
        "Translate this English sentence into natural Japanese for an English learner. "
        "Return only the Japanese translation, without explanation or quotation marks.\n\n"
        + sentence.strip()
    )
    input_event = json.dumps(
        {"event": "user", "message": {"content": prompt}}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8") + b"\n"

    try:
        with tempfile.TemporaryDirectory(prefix="linguacast-agy-") as tmp:
            home = Path(tmp) / "home"
            settings, workspace = _prepare_isolated_home(home)
            env = {key: os.environ[key] for key in ALLOWED_ENV_KEYS if key in os.environ}
            env.update({
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "TMPDIR": str(home / "tmp"),
                "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
            })
            argv = [
                agy, "--settings", str(settings), "--sandbox", "--mode", "plan",
                "--disable-slash-commands", "--input-format", "stream-json",
                "--output-format", "stream-json", "--model", selected_model,
                "--print-timeout", f"{CLI_PRINT_TIMEOUT_SECONDS}s",
            ]
            proc = subprocess.Popen(
                argv,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            collected = {"stdout": bytearray(), "stderr": bytearray()}
            streams_open = {"stdout", "stderr"}
            selector: selectors.BaseSelector | None = None
            try:
                selector = selectors.DefaultSelector()
                assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
                stdin_fd, stdout_fd, stderr_fd = proc.stdin.fileno(), proc.stdout.fileno(), proc.stderr.fileno()
                for fd in (stdin_fd, stdout_fd, stderr_fd):
                    os.set_blocking(fd, False)
                selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
                selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
                selector.register(stderr_fd, selectors.EVENT_READ, "stderr")
                input_offset = 0
                deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS
                while streams_open or input_offset < len(input_event) or proc.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TranslationRunnerError("Translation timed out. Please try again.")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        stream, fd = key.data, key.fd
                        if stream == "stdin":
                            try:
                                written = os.write(fd, input_event[input_offset:input_offset + 4096])
                            except BlockingIOError:
                                continue
                            except (BrokenPipeError, OSError):
                                raise TranslationRunnerError("Translation could not be completed.") from None
                            input_offset += written
                            if input_offset == len(input_event):
                                selector.unregister(fd)
                                proc.stdin.close()
                        else:
                            try:
                                chunk = os.read(fd, 4096)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                selector.unregister(fd)
                                streams_open.discard(stream)
                                continue
                            collected[stream].extend(chunk)
                            if len(collected[stream]) > MAX_OUTPUT_BYTES:
                                raise TranslationRunnerError("Translation could not be completed.")
                    if proc.poll() is not None and not streams_open and input_offset == len(input_event):
                        break
                if input_offset != len(input_event):
                    raise TranslationRunnerError("Translation could not be completed.")
            except TranslationRunnerError as error:
                raise error from None
            except (OSError, ValueError):
                raise TranslationRunnerError("Translation could not be completed.") from None
            finally:
                if selector is not None:
                    selector.close()
                # The leader can exit cleanly while a descendant in its group
                # stays alive without holding a pipe. Reap any such children.
                _terminate_process_group(proc)
                _close_process_pipes(proc)
            if proc.poll() is None:
                _terminate_process_group(proc)
                raise TranslationRunnerError("Translation timed out. Please try again.")
            if proc.returncode != 0:
                raise TranslationRunnerError("Translation is unavailable.")
            return _extract_translation(bytes(collected["stdout"]))
    except TranslationRunnerError:
        raise
    except (OSError, subprocess.SubprocessError, TimeoutError):
        raise TranslationRunnerError("Translation is unavailable.") from None


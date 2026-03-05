"""Concrete agent-provider implementations (Claude, Codex, Gemini, OpenCode)."""

import json
import logging
import os
import re
import subprocess
import time

from millstone.agent_providers.base import CLIProvider, CLIResult

logger = logging.getLogger(__name__)


class ClaudeProvider(CLIProvider):
    """Provider for Claude Code CLI (Anthropic)."""

    @property
    def name(self) -> str:
        return "Claude Code"

    @property
    def command(self) -> str:
        return "claude"

    @property
    def install_instructions(self) -> str:
        return "Install with: npm install -g @anthropic-ai/claude-code"

    # Claude Code env vars that cause a spawned subprocess to hook back into the
    # parent session via SSE instead of running standalone (introduced ~2.1.50).
    # Strip these so `claude -p` runs as an independent process.
    _PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

    def version_command(self) -> list[str]:
        return ["claude", "--version"]

    def run(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> "CLIResult":
        """Run claude with parent-session env vars stripped and stdin closed.

        Claude Code ≥2.1.50 uses CLAUDE_CODE_SSE_PORT to register spawned
        subprocesses back into the parent session.  When invoked from inside an
        existing Claude Code session (e.g. via the Bash tool), inheriting those
        vars causes `claude -p` to hang waiting for the parent to orchestrate it.
        Stripping them and closing stdin forces fully standalone execution.
        """
        cmd = self.build_command(
            prompt,
            resume=resume,
            model=model,
            output_schema=output_schema,
            schema_work_dir=schema_work_dir,
        )
        clean_env = {k: v for k, v in os.environ.items() if k not in self._PARENT_SESSION_VARS}
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=clean_env,
            stdin=subprocess.DEVNULL,
        )

        output = result.stdout
        if result.stdout and result.stderr:
            output += "\n"
        output += result.stderr

        return CLIResult(
            output=output,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def build_command(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> list[str]:
        """Build Claude Code CLI command.

        Claude Code uses:
            claude -p <prompt> --dangerously-skip-permissions [--resume <id>] [--model <model>]
            [--json-schema <schema>]

        For structured output, uses --json-schema with inline JSON schema.
        """
        cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
        if resume:
            cmd.extend(["--resume", resume])
        if model:
            cmd.extend(["--model", model])
        if output_schema:
            from millstone.policy.schemas import get_schema_json
            cmd.extend(["--output-format", "json"])
            cmd.extend(["--json-schema", get_schema_json(output_schema)])
        return cmd


class CodexProvider(CLIProvider):
    """Provider for Codex CLI (OpenAI)."""

    @property
    def name(self) -> str:
        return "Codex CLI"

    @property
    def command(self) -> str:
        return "codex"

    @property
    def install_instructions(self) -> str:
        return "Install with: npm install -g @openai/codex"

    def version_command(self) -> list[str]:
        return ["codex", "--version"]

    def build_command(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> list[str]:
        """Build Codex CLI command.

        Codex uses:
            codex exec - --yolo [--model <model>] [--output-schema <path>]

        The prompt is always passed via stdin (using the '-' sentinel) to avoid
        hitting the OS execve argument-size limit (E2BIG / errno 7) on large
        review prompts.  The caller's run() method must pass input=prompt to
        subprocess.run when using this command.

        For resume:
            codex exec resume <session_id> [<follow_up_prompt>]

        Note: --yolo bypasses approvals and sandboxing (equivalent to claude's
        --dangerously-skip-permissions).
        """
        if resume:
            # Resume an existing session
            cmd = ["codex", "exec", "resume", resume]
            if prompt:
                # Follow-up prompts are short enough to be safe as positional args.
                cmd.append(prompt)
        else:
            # New session — use '-' so codex reads the prompt from stdin.
            cmd = ["codex", "exec", "-", "--yolo"]

        if model:
            cmd.extend(["--model", model])

        if output_schema and schema_work_dir:
            from millstone.policy.schemas import get_schema_path
            schema_path = get_schema_path(output_schema, schema_work_dir)
            cmd.extend(["--output-schema", schema_path])

        return cmd

    def run(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> "CLIResult":
        """Run Codex with the prompt delivered via stdin.

        Overrides the base-class run() to pipe the prompt through stdin rather
        than embedding it as a command-line argument.  This avoids ERRNO 7
        (E2BIG — argument list too long) on large review/build prompts.
        """
        cmd = self.build_command(
            prompt,
            resume=resume,
            model=model,
            output_schema=output_schema,
            schema_work_dir=schema_work_dir,
        )
        result = subprocess.run(
            cmd,
            input=prompt if not resume else None,
            capture_output=True,
            text=True,
            cwd=cwd,
        )

        output = result.stdout
        if result.stdout and result.stderr:
            output += "\n"
        output += result.stderr

        return CLIResult(
            output=output,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


class GeminiProvider(CLIProvider):
    """Provider for Gemini CLI (Google)."""

    RETRY_MAX_ATTEMPTS = 4
    RETRY_BASE_DELAY_SECONDS = 1.0

    @property
    def name(self) -> str:
        return "Gemini CLI"

    @property
    def command(self) -> str:
        return "gemini"

    @property
    def install_instructions(self) -> str:
        return "Install with: npm install -g @google/gemini-cli"

    def version_command(self) -> list[str]:
        return ["gemini", "--version"]

    def build_command(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> list[str]:
        """Build Gemini CLI command.

        Gemini uses:
            gemini [prompt] -y [-r <resume>] [-m <model>] [-o json]

        For structured output, we append schema instructions to the prompt
        since the CLI doesn't support --json-schema natively.
        """
        cmd = ["gemini"]

        # Options first
        cmd.append("-y")  # YOLO mode (skip confirmations)
        cmd.extend(["-o", "json"]) # structured output for safer parsing

        if resume:
            cmd.extend(["-r", resume])

        if model:
            cmd.extend(["-m", model])

        # Prepare prompt with schema if needed
        full_prompt = prompt
        if output_schema:
            from millstone.policy.schemas import get_schema_json
            schema = get_schema_json(output_schema)
            # clear instructions for JSON
            full_prompt = (
                f"{prompt}\n\n"
                "IMPORTANT: You MUST return a valid JSON object strictly conforming to this schema. "
                "Do not include any text before or after the JSON.\n"
                f"{schema}"
            )

        if full_prompt:
            cmd.append(full_prompt)

        return cmd

    def run(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> CLIResult:
        # Build command (this will include the schema in prompt if needed)
        cmd = self.build_command(
            prompt,
            resume=resume,
            model=model,
            output_schema=output_schema,
            schema_work_dir=schema_work_dir,
        )

        retry_notes: list[str] = []
        result = None
        for attempt in range(1, self.RETRY_MAX_ATTEMPTS + 1):
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd,
            )
            combined_output = self._combine_output(result.stdout, result.stderr)
            retry_reason = self._get_retryable_reason(result.returncode, combined_output)
            if retry_reason is None or attempt == self.RETRY_MAX_ATTEMPTS:
                if retry_reason and attempt == self.RETRY_MAX_ATTEMPTS:
                    logger.error(
                        "Gemini CLI retry exhausted after %d attempts (model=%s, reason=%s, returncode=%d)",
                        self.RETRY_MAX_ATTEMPTS,
                        model or "<default>",
                        retry_reason,
                        result.returncode,
                    )
                break

            delay_seconds = self.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            note = (
                f"Gemini retry {attempt}/{self.RETRY_MAX_ATTEMPTS - 1} in {delay_seconds:.1f}s: "
                f"reason={retry_reason}, returncode={result.returncode}, model={model or '<default>'}"
            )
            retry_notes.append(note)
            logger.warning("%s", note)
            time.sleep(delay_seconds)

        if result is None:
            return CLIResult(output="", returncode=1, stdout="", stderr="Gemini command did not execute")

        stdout = result.stdout
        stderr = result.stderr

        # Save raw stdout for the combined output log
        raw_stdout = stdout

        # Attempt to unwrap Gemini's JSON envelope if successful
        if result.returncode == 0 and stdout.strip():
            try:
                data = json.loads(stdout)
                if isinstance(data, dict) and "response" in data:
                    # Unwrap the actual model response
                    stdout = data["response"]
            except json.JSONDecodeError:
                # Fallback: scan for a valid JSON object containing "response"
                decoder = json.JSONDecoder()
                search_pos = 0
                while True:
                    # Find next opening brace
                    start = stdout.find('{', search_pos)
                    if start == -1:
                        break

                    try:
                        data, idx = decoder.raw_decode(stdout[start:])
                        # idx is relative to stdout[start:], so absolute end is start + idx
                        if isinstance(data, dict) and "response" in data:
                            stdout = data["response"]
                            break
                        # Found valid JSON but not the one we want, advance past it
                        search_pos = start + idx
                    except json.JSONDecodeError:
                        # Parsing failed starting at 'start', advance past it
                        search_pos = start + 1

        output = self._combine_output(raw_stdout, stderr)
        if retry_notes:
            output = "\n".join(retry_notes) + ("\n" + output if output else "")

        return CLIResult(
            output=output,
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def _combine_output(stdout: str, stderr: str) -> str:
        output = stdout
        if stdout and stderr:
            output += "\n"
        output += stderr
        return output

    @staticmethod
    def _get_retryable_reason(returncode: int, combined_output: str) -> str | None:
        """Return a retry reason string for transient capacity/overload errors."""
        if returncode == 0:
            return None

        haystack = (combined_output or "").lower()
        patterns = {
            "model_capacity_exhausted": [
                r"model_capacity_exhausted",
                r"no capacity available for model",
            ],
            "resource_exhausted": [
                r"resource_exhausted",
                r"retryablequotaerror",
                r"\b429\b",
            ],
            "service_unavailable": [
                r"\b503\b",
                r"\bunavailable\b",
                r"temporarily overloaded",
            ],
        }

        for reason, reason_patterns in patterns.items():
            if any(re.search(pattern, haystack) for pattern in reason_patterns):
                return reason
        return None


class OpenCodeProvider(CLIProvider):
    """Provider for OpenCode CLI."""

    DEFAULT_MODEL = "opencode/trinity-large-preview-free"

    @property
    def name(self) -> str:
        return "OpenCode"

    @property
    def command(self) -> str:
        return "opencode"

    @property
    def install_instructions(self) -> str:
        return "Install with: npm install -g @opencode/cli"

    def version_command(self) -> list[str]:
        return ["opencode", "--version"]

    def build_command(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> list[str]:
        """Build OpenCode CLI command.

        OpenCode uses:
            opencode run [message] [-s <session>] [-m <model>] [--format json]
        """
        cmd = ["opencode", "run"]

        # If it's a new session, the first argument can be the prompt
        # but it's safer to use --prompt if we might have weird chars
        # however, 'opencode run [message..]' is the standard.
        if prompt and not resume:
            cmd.append(prompt)

        # Force JSON format for safer parsing of events
        cmd.extend(["--format", "json"])

        if resume:
            cmd.extend(["--session", resume])
            if prompt:
                # Follow-up prompt in an existing session
                cmd.append(prompt)

        # Use provided model or default
        cmd.extend(["-m", model or self.DEFAULT_MODEL])

        # Prepare prompt with schema if needed (OpenCode doesn't have native --json-schema)
        if output_schema:
            from millstone.policy.schemas import get_schema_json
            schema = get_schema_json(output_schema)
            # Append schema instructions to the prompt if it's the last arg
            schema_instruction = (
                "\n\nIMPORTANT: You MUST return a valid JSON object strictly conforming to this schema. "
                "Do not include any text before or after the JSON.\n"
                f"{schema}"
            )
            if cmd[-1] == prompt:
                cmd[-1] = f"{prompt}\n\n{schema_instruction}"
            else:
                # If prompt wasn't the last arg (rare in this logic), append a new message
                cmd.append(schema_instruction)

        return cmd

    def run(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        output_schema: str | None = None,
        schema_work_dir: str | None = None,
    ) -> CLIResult:
        cmd = self.build_command(
            prompt,
            resume=resume,
            model=model,
            output_schema=output_schema,
            schema_work_dir=schema_work_dir,
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
        )

        stdout = result.stdout
        stderr = result.stderr

        # Parse JSON events from stdout to extract the final text
        extracted_text = []

        if stdout.strip():
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if event.get("type") == "text":
                        extracted_text.append(event.get("part", {}).get("text", ""))
                except json.JSONDecodeError:
                    # Ignore non-JSON lines (like database migration warnings)
                    continue

        final_stdout = "".join(extracted_text)

        # If we failed to extract any text but had stdout, it might not be in JSON format
        # or it might be an error.
        if (
            not final_stdout
            and stdout.strip()
            and not any(
                json.loads(line).get("type") == "text"
                for line in stdout.splitlines()
                if line.strip() and line.startswith("{")
            )
        ):
            final_stdout = stdout

        combined_output = stdout
        if stdout and stderr:
            combined_output += "\n"
        combined_output += stderr

        return CLIResult(
            output=combined_output,
            returncode=result.returncode,
            stdout=final_stdout,
            stderr=stderr,
        )


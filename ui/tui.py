import json
from pathlib import Path
from typing import Any
from rich.console import Console, Group
from rich.theme import Theme
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.live import Live
from rich.markdown import Markdown
from config.config import Config
from tools.base import ToolConfirmation
from utils.paths import display_path_rel_to_cwd

AGENT_THEME = Theme(
    {
        # General
        "info": "cyan",
        "warning": "yellow",
        "error": "bright_red",
        "success": "green",
        "dim": "dim",
        "muted": "grey54",
        "border": "grey35",
        "highlight": "bold bright_cyan",
        "accent": "medium_purple1",
        # Roles
        "user": "bright_cyan bold",
        "assistant": "bright_white",
        # Tools
        "tool": "bright_white bold",
        "tool.read": "cyan",
        "tool.write": "yellow",
        "tool.shell": "medium_purple1",
        "tool.network": "bright_blue",
        "tool.memory": "green",
        "tool.mcp": "bright_cyan",
        # Code / blocks
        "code": "white",
        "diff.add": "green",
        "diff.del": "red",
        "diff.hunk": "cyan",
    }
)

_console: Console | None = None

# Kind -> bullet color for the ⏺ indicator
_KIND_STYLE = {
    "read": "cyan",
    "write": "yellow",
    "shell": "medium_purple1",
    "network": "bright_blue",
    "memory": "green",
    "mcp": "bright_cyan",
}

# Primary argument shown inline next to the tool name
_PRIMARY_ARG = {
    "read_file": "path",
    "write_file": "path",
    "edit": "path",
    "shell": "command",
    "list_dir": "path",
    "grep": "pattern",
    "glob": "pattern",
    "web_search": "query",
    "web_fetch": "url",
    "memory": "key",
    "todos": "action",
}

_PREVIEW_LINES = 8


def get_console() -> Console:
    global _console
    if _console is None:
        _console = Console(theme=AGENT_THEME, highlight=False)

    return _console


class TUI:
    def __init__(
        self,
        config: Config,
        console: Console | None = None,
    ) -> None:
        self.console = console or get_console()
        self._tool_args_by_call_id: dict[str, dict[str, Any]] = {}
        self.config = config
        self.cwd = self.config.cwd
        self._live: Live | None = None
        self._stream_buffer: str = ""
        self._assistant_header_shown = False
        self._status = None

    # ------------------------------------------------------------------ #
    # Thinking indicator
    # ------------------------------------------------------------------ #

    def start_thinking(self) -> None:
        if self._status is None and self._live is None:
            self._status = self.console.status(
                Text("thinking…", style="muted"), spinner="dots", spinner_style="accent"
            )
            self._status.start()

    def stop_thinking(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    # ------------------------------------------------------------------ #
    # Assistant streaming (live markdown)
    # ------------------------------------------------------------------ #

    def begin_assistant(self) -> None:
        self.stop_thinking()
        self._stream_buffer = ""
        self._assistant_header_shown = False
        self.console.print()

    def stream_assistant_delta(self, content: str) -> None:
        self.stop_thinking()
        self._stream_buffer += content
        self._commit_finished_blocks()
        self._update_preview()

    def end_assistant(self) -> None:
        self._flush_stream()

    # A live region can only redraw itself while it fits in the window: once a
    # reply grows past the bottom, its earlier frames scroll out of reach and
    # stay on screen, so the answer ends up printed several times over, each
    # copy a little longer than the last. Two rules keep that from happening -
    # a finished paragraph is committed rather than previewed, and a block that
    # outgrows the window on its own is committed early. Committing never
    # reprints: the live region's final frame is the permanent output, so
    # nothing has to be erased.

    def _commit_finished_blocks(self) -> None:
        while True:
            split_at = self._stream_buffer.find("\n\n")
            if split_at == -1:
                return

            block = self._stream_buffer[:split_at]

            # An odd number of fences means this blank line sits inside a code
            # block, where it is content rather than a separator.
            if block.count("```") % 2:
                return

            self._stream_buffer = self._stream_buffer[split_at + 2 :]
            self._commit_block(block)

    def _update_preview(self) -> None:
        pending = self._stream_buffer

        if not pending.strip():
            return

        if self._estimate_height(pending) >= self.console.height - 4:
            self._stream_buffer = ""
            self._commit_block(pending)
            return

        if self._live is None:
            self._print_assistant_header()
            self._live = Live(
                console=self.console,
                refresh_per_second=12,
                vertical_overflow="visible",
            )
            self._live.start()

        self._live.update(Markdown(pending, code_theme="monokai"))

    def _commit_block(self, block: str) -> None:
        if not block.strip():
            return

        markdown = Markdown(block, code_theme="monokai")

        if self._live is not None:
            # The live region is already showing this block half-written, so
            # settling it in place is both the commit and the final render.
            self._live.update(markdown)
            self._live.stop()
            self._live = None
            return

        self._print_assistant_header()
        self.console.print(markdown)

    def _print_assistant_header(self) -> None:
        if not self._assistant_header_shown:
            self.console.print(Text("⏺ assistant", style="muted"))
            self._assistant_header_shown = True

    def _estimate_height(self, text: str) -> int:
        width = max(self.console.width - 4, 20)
        return sum(max(1, -(-len(line) // width)) for line in text.splitlines()) + 2

    def _flush_stream(self) -> None:
        """Commit whatever is still buffered and close the live region."""
        pending = self._stream_buffer
        self._stream_buffer = ""

        if pending.strip():
            self._commit_block(pending)
        elif self._live is not None:
            self._live.stop()
            self._live = None

        self._assistant_header_shown = False

    def _render_assistant(self, text: str) -> Group:
        return Group(
            Text("⏺ assistant", style="muted"),
            Markdown(text, code_theme="monokai"),
        )

    def _close_stream(self) -> None:
        """Settle any live rendering before printing static output."""
        self.stop_thinking()
        self._flush_stream()

    def print_transcript(
        self,
        messages: list[dict[str, Any]],
        limit: int = 20,
    ) -> None:
        """Replay a restored conversation so the user can see what was said.

        Only the visible turns are drawn: tool results stay collapsed to the
        call line, the way they look once a live turn has finished.
        """
        self._close_stream()

        visible = [m for m in messages if m.get("role") in {"user", "assistant"}]
        if not visible:
            return

        hidden = max(0, len(visible) - limit)
        visible = visible[-limit:] if hidden else visible

        self.console.print()
        self.console.rule(Text("restored conversation", style="muted"), style="border")

        if hidden:
            self.console.print(
                Text(f"  … {hidden} earlier messages not shown", style="muted")
            )

        for message in visible:
            content = (message.get("content") or "").strip()

            if message["role"] == "user":
                self.console.print()
                self.console.print(Text(f"> {content}", style="user"))
                continue

            if content:
                self.console.print()
                self.console.print(self._render_assistant(content))

            for call in message.get("tool_calls") or []:
                self.console.print(self._format_restored_call(call))

        self.console.rule(style="border")
        self.console.print()

    def _format_restored_call(self, call: dict[str, Any]) -> Text:
        function = call.get("function") or {}
        name = function.get("name") or "unknown"

        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            args = {}

        if not isinstance(args, dict):
            args = {}

        return Text.assemble(
            ("⏺ ", "muted"),
            (name, "muted"),
            ("(", "muted"),
            (self._format_call(name, args), "muted"),
            (")", "muted"),
        )

    # ------------------------------------------------------------------ #
    # Welcome / help
    # ------------------------------------------------------------------ #

    def print_welcome(self, title: str, lines: list[str]) -> None:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="muted", justify="right", no_wrap=True)
        grid.add_column(style="code")

        for line in lines:
            if ":" in line:
                key, _, value = line.partition(":")
                grid.add_row(key.strip(), value.strip())
            else:
                grid.add_row("", line)

        header = Text.assemble(("✳ ", "accent"), (title, "highlight"))

        self.console.print()
        self.console.print(
            Panel(
                Group(header, Text(), grid),
                border_style="border",
                box=box.ROUNDED,
                padding=(1, 2),
                expand=False,
            )
        )
        self.console.print(
            Text("  type a message, or /help for commands", style="dim")
        )

    # ------------------------------------------------------------------ #
    # Tool calls (compact, Claude Code style)
    # ------------------------------------------------------------------ #

    def _shorten(self, value: str, limit: int = 64) -> str:
        value = " ".join(value.split())
        if len(value) > limit:
            return value[: limit - 1] + "…"
        return value

    def _format_call(self, name: str, args: dict[str, Any]) -> str:
        primary_key = _PRIMARY_ARG.get(name)
        primary = args.get(primary_key) if primary_key else None

        if primary is None and args:
            primary = next(iter(args.values()))

        if primary is None:
            return ""

        text = str(primary)
        if primary_key in {"path", "cwd"} and self.cwd:
            text = str(display_path_rel_to_cwd(text, self.cwd))

        return self._shorten(text)

    def tool_call_start(
        self,
        call_id: str,
        name: str,
        tool_kind: str | None,
        arguments: dict[str, Any],
    ) -> None:
        self._close_stream()
        self._tool_args_by_call_id[call_id] = arguments

        bullet_style = _KIND_STYLE.get(tool_kind or "", "accent")
        call_args = self._format_call(name, arguments)

        line = Text.assemble(
            ("⏺ ", bullet_style),
            (name, "tool"),
            ("(", "muted"),
            (call_args, "muted"),
            (")", "muted"),
        )
        self.console.print()
        self.console.print(line)

    def _result_summary(
        self,
        name: str,
        success: bool,
        output: str,
        metadata: dict[str, Any] | None,
        exit_code: int | None,
    ) -> str:
        md = metadata if isinstance(metadata, dict) else {}

        if name == "read_file":
            total = md.get("total_lines")
            if isinstance(total, int):
                return f"Read {total} lines"
        elif name in {"write_file", "edit"}:
            line = output.strip().splitlines()
            if line:
                return line[0]
        elif name == "shell":
            if exit_code is not None and exit_code != 0:
                return f"exit code {exit_code}"
            lines = len(output.strip().splitlines())
            return f"{lines} lines of output" if lines else "done"
        elif name == "list_dir":
            entries = md.get("entries")
            if isinstance(entries, int):
                return f"{entries} entries"
        elif name == "grep":
            matches = md.get("matches")
            searched = md.get("files_searched")
            parts = []
            if isinstance(matches, int):
                parts.append(f"{matches} matches")
            if isinstance(searched, int):
                parts.append(f"{searched} files searched")
            if parts:
                return " • ".join(parts)
        elif name == "glob":
            matches = md.get("matches")
            if isinstance(matches, int):
                return f"{matches} matches"
        elif name == "web_search":
            results = md.get("results")
            if isinstance(results, int):
                return f"{results} results"
        elif name == "web_fetch":
            status_code = md.get("status_code")
            size = md.get("content_length")
            parts = []
            if isinstance(status_code, int):
                parts.append(f"HTTP {status_code}")
            if isinstance(size, int):
                parts.append(f"{size} bytes")
            if parts:
                return " • ".join(parts)

        first_line = output.strip().splitlines()
        if first_line:
            return self._shorten(first_line[0], 80)

        return "done" if success else "failed"

    def _preview_lines(self, output: str, max_lines: int = _PREVIEW_LINES) -> Text:
        lines = output.rstrip().splitlines()
        shown = lines[:max_lines]
        body = Text()

        for i, line in enumerate(shown):
            body.append("    " + line[:200], style="dim")
            if i < len(shown) - 1:
                body.append("\n")

        hidden = len(lines) - len(shown)
        if hidden > 0:
            body.append("\n")
            body.append(f"    … +{hidden} more lines", style="muted")

        return body

    def _render_diff(self, diff: str, max_lines: int = 30) -> Text:
        body = Text()
        lines = diff.rstrip().splitlines()
        shown = lines[:max_lines]

        for i, line in enumerate(shown):
            if line.startswith("+") and not line.startswith("+++"):
                style = "diff.add"
            elif line.startswith("-") and not line.startswith("---"):
                style = "diff.del"
            elif line.startswith("@@"):
                style = "diff.hunk"
            else:
                style = "dim"

            body.append("    " + line, style=style)
            if i < len(shown) - 1:
                body.append("\n")

        hidden = len(lines) - len(shown)
        if hidden > 0:
            body.append("\n")
            body.append(f"    … +{hidden} more lines", style="muted")

        return body

    def tool_call_complete(
        self,
        call_id: str,
        name: str,
        tool_kind: str | None,
        success: bool,
        output: str,
        error: str | None,
        metadata: dict[str, Any] | None,
        diff: str | None,
        truncated: bool,
        exit_code: int | None,
    ) -> None:
        self._close_stream()

        summary = self._result_summary(name, success, output, metadata, exit_code)

        if success:
            line = Text.assemble(
                ("  ⎿ ", "muted"),
                (summary, "muted"),
            )
        else:
            line = Text.assemble(
                ("  ⎿ ", "error"),
                (self._shorten(error or summary, 120), "error"),
            )

        self.console.print(line)

        # Show what matters: diffs for edits, previews for searches/shell,
        # full error text for failures.
        if not success and error and len(error.splitlines()) > 1:
            self.console.print(self._preview_lines(error, 12))
        elif name in {"write_file", "edit"} and success and diff:
            self.console.print(self._render_diff(diff))
        elif name in {"shell", "grep", "glob", "list_dir", "web_search", "todos"}:
            if success and output.strip():
                self.console.print(self._preview_lines(output))

        if truncated:
            self.console.print(
                Text("    note: output truncated for the model", style="warning")
            )

    # ------------------------------------------------------------------ #
    # Approval prompt
    # ------------------------------------------------------------------ #

    def handle_confirmation(self, confirmation: ToolConfirmation) -> bool:
        self._close_stream()

        blocks: list[Any] = [
            Text.assemble(
                (confirmation.tool_name, "tool"),
                ("  ", "muted"),
                (confirmation.description, "muted"),
            )
        ]

        if confirmation.command:
            blocks.append(Text())
            blocks.append(Text(f"  $ {confirmation.command}", style="warning"))

        if confirmation.diff:
            blocks.append(Text())
            blocks.append(self._render_diff(confirmation.diff.to_diff(), 40))

        self.console.print()
        self.console.print(
            Panel(
                Group(*blocks),
                title=Text("● approval required", style="warning"),
                title_align="left",
                border_style="warning",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

        response = Prompt.ask(
            Text.assemble(("  approve? ", "warning"), ("[y/n]", "muted")),
            choices=["y", "n", "yes", "no"],
            default="n",
            show_choices=False,
        )

        return response.lower() in {"y", "yes"}

    # ------------------------------------------------------------------ #
    # Help
    # ------------------------------------------------------------------ #

    def show_help(self) -> None:
        help_text = """
## Commands

- `/help` - Show this help
- `/exit` or `/quit` - Exit the agent
- `/clear` - Clear conversation history
- `/config` - Show current configuration
- `/model <name>` - Change the model
- `/approval <mode>` - Change approval mode
- `/stats` - Show session statistics
- `/tools` - List available tools
- `/mcp` - Show MCP server status
- `/save` - Save current session
- `/checkpoint` - Create a checkpoint
- `/checkpoints` - List available checkpoints
- `/restore <checkpoint_id>` - Restore a checkpoint
- `/sessions` - List saved sessions
- `/resume <session_id>` - Resume a saved session

## Tips

- Just type your message to chat with the agent
- The agent can read, write, and execute code
- Some operations require approval (can be configured)
"""
        self.console.print(Markdown(help_text))

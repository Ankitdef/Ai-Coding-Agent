import os
import re
import sys
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion, merge_completers
from prompt_toolkit.document import Document
from prompt_toolkit.filters import completion_is_selected
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.text import Text

from config.config import Config
from config.loader import get_data_dir

COMMANDS = {
    "/help": "Show help",
    "/exit": "Exit the agent",
    "/quit": "Exit the agent",
    "/clear": "Clear conversation history",
    "/config": "Show current configuration",
    "/model": "Change the model",
    "/approval": "Change approval mode",
    "/stats": "Show session statistics",
    "/tools": "List available tools",
    "/mcp": "Show MCP server status",
    "/save": "Save current session",
    "/checkpoint": "Create a checkpoint",
    "/checkpoints": "List checkpoints",
    "/restore": "Restore a checkpoint",
    "/sessions": "List saved sessions",
    "/resume": "Resume a saved session",
}

INPUT_STYLE = Style.from_dict(
    {
        "prompt": "ansicyan bold",
        "placeholder": "#666666 italic",
        "bottom-toolbar": "#888888 bg:default noreverse",
        "completion-menu": "bg:#1c1c1c #cccccc",
        "completion-menu.completion.current": "bg:#333333 #00d7ff bold",
        "completion-menu.meta.completion": "bg:#1c1c1c #777777",
        "completion-menu.meta.completion.current": "bg:#333333 #999999",
    }
)


class SlashCommandCompleter(Completer):
    """Autocomplete for /commands, shown only at the start of the line."""

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        if not text.startswith("/") or " " in text:
            return

        for command, description in COMMANDS.items():
            if command.startswith(text.lower()):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display_meta=description,
                )


class MentionPathCompleter(Completer):
    """Autocomplete for @file mentions - suggests project files after '@'."""

    SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv"}
    MAX_PATHS = 2000
    MAX_RESULTS = 20
    CACHE_SECONDS = 10

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self._cached_paths: list[str] = []
        self._cached_at: float = 0.0

    def _project_paths(self) -> list[str]:
        now = time.monotonic()
        if self._cached_paths and now - self._cached_at < self.CACHE_SECONDS:
            return self._cached_paths

        paths: list[str] = []
        for root, dirs, files in os.walk(self.cwd):
            dirs[:] = [
                d
                for d in dirs
                if d not in self.SKIP_DIRS and not d.startswith(".")
            ]

            for name in files:
                rel = os.path.relpath(os.path.join(root, name), self.cwd)
                paths.append(rel.replace("\\", "/"))

                if len(paths) >= self.MAX_PATHS:
                    break
            if len(paths) >= self.MAX_PATHS:
                break

        self._cached_paths = paths
        self._cached_at = now
        return paths

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        match = re.search(r"@([A-Za-z0-9_\-./\\]*)$", text)
        if not match:
            return

        typed = match.group(1)
        prefix = typed.lower()
        shown = 0

        for rel_path in self._project_paths():
            if prefix in rel_path.lower():
                yield Completion(
                    "@" + rel_path,
                    start_position=-(len(typed) + 1),
                    display=rel_path,
                    display_meta="file",
                )
                shown += 1
                if shown >= self.MAX_RESULTS:
                    return


class InputBox:
    """Claude Code style bordered input with history and command completion."""

    def __init__(self, console: Console, config: Config) -> None:
        self.console = console
        self.config = config
        self._is_tty = sys.stdin.isatty() and sys.stdout.isatty()
        self.session: PromptSession | None = None

        if not self._is_tty:
            return

        history_path = get_data_dir() / "input_history"
        history_path.parent.mkdir(parents=True, exist_ok=True)

        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _insert_newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add("enter", filter=completion_is_selected)
        def _accept_completion(event) -> None:
            # Enter with a highlighted completion confirms the selection
            # instead of submitting the message.
            event.current_buffer.complete_state = None

        try:
            self.session = PromptSession(
                history=FileHistory(str(history_path)),
                auto_suggest=AutoSuggestFromHistory(),
                completer=merge_completers(
                    [
                        SlashCommandCompleter(),
                        MentionPathCompleter(self.config.cwd),
                    ]
                ),
                complete_while_typing=True,
                key_bindings=bindings,
                style=INPUT_STYLE,
            )
        except Exception:
            self.session = None

    def _toolbar(self) -> HTML:
        model = self.config.model_name
        approval = self.config.approval.value
        return HTML(
            f"  {model}  •  approval: {approval}"
            "  •  alt+enter newline  •  tab complete"
        )

    async def read(self) -> str:
        if self.session is None:
            return self.console.input("\n[user]❯[/user] ").strip()

        width = max(self.console.width, 20)

        self.console.print()
        self.console.print(Text("─" * width, style="grey23"))

        text = await self.session.prompt_async(
            [("class:prompt", "❯ ")],
            placeholder=[("class:placeholder", " plan, search, build anything")],
            prompt_continuation=[("class:prompt", "  ")],
            bottom_toolbar=self._toolbar,
        )

        return text.strip()

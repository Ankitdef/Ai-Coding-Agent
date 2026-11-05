import asyncio
from pathlib import Path
import sys
import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from agent.agent import Agent
from agent.events import AgentEventType
from agent.persistence import PersistenceManager, SessionSnapshot
from agent.session import Session
from client.response import TokenUsage
from config.config import ApprovalPolicy, Config
from config.loader import load_config
from ui.input import InputBox
from ui.tui import TUI, get_console
from utils.mentions import expand_mentions, is_only_mentions

console = get_console()


class CLI:
    def __init__(
        self,
        config: Config,
        auto_save: bool = True,
        continue_last: bool = False,
    ):
        self.agent: Agent | None = None
        self.config = config
        self.tui = TUI(config, console)
        self.auto_save = auto_save
        self.continue_last = continue_last
        self.persistence = PersistenceManager()

    async def run_single(self, message: str) -> str | None:
        async with Agent(self.config) as agent:
            self.agent = agent
            return await self._process_message(message)

    async def run_interactive(self) -> str | None:
        self.tui.print_welcome(
            "AI Agent",
            lines=[
                f"model: {self.config.model_name}",
                f"cwd: {self.config.cwd}",
                "commands: /help /config /approval /model /exit",
            ],
        )

        input_box = InputBox(console, self.config)

        async with Agent(
            self.config,
            confirmation_callback=self.tui.handle_confirmation,
        ) as agent:
            self.agent = agent

            if self.continue_last:
                await self._continue_last_session()

            try:
                await self._interactive_loop(input_box)
            finally:
                self._autosave()

        console.print("\n[dim]Goodbye![/dim]")

    async def _interactive_loop(self, input_box: InputBox) -> None:
        pending_mentions: list[str] = []

        while True:
            try:
                user_input = await input_box.read()
                if not user_input:
                    continue

                if user_input.startswith("/"):
                    should_continue = await self._handle_command(user_input)
                    if not should_continue:
                        break
                    continue

                if is_only_mentions(user_input):
                    pending_mentions.append(user_input)
                    attached = ", ".join(
                        m.lstrip("@").rstrip(".,;:?!")
                        for m in user_input.split()
                        if m.startswith("@")
                    )
                    console.print(f"[muted]  ⎿ attached {attached}[/muted]")
                    continue

                if pending_mentions:
                    user_input = " ".join(pending_mentions) + "\n" + user_input
                    pending_mentions = []

                await self._process_message(user_input)
                self._autosave()
            except KeyboardInterrupt:
                console.print("\n[dim]Use /exit to quit[/dim]")
            except EOFError:
                break

    async def _continue_last_session(self) -> None:
        snapshot = self.persistence.latest_session()

        if not snapshot:
            console.print("[muted]No saved session to continue[/muted]")
            return

        await self._activate_snapshot(
            snapshot,
            f"Continuing session: {snapshot.session_id}"
            f" ({snapshot.turn_count} turns)",
        )

    def _snapshot(self) -> SessionSnapshot:
        session = self.agent.session

        return SessionSnapshot(
            session_id=session.session_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            turn_count=session.turn_count,
            messages=session.context_manager.get_messages(),
            total_usage=session.context_manager.total_usage,
            tool_state=session.tool_registry.export_state(),
        )

    def _autosave(self) -> None:
        if not self.auto_save or not self.agent or not self.agent.session:
            return

        # Nothing to save after /clear - and overwriting the file would throw
        # away history the user may still want to /resume.
        if self.agent.session.context_manager.message_count == 0:
            return

        try:
            self.persistence.save_session(self._snapshot())
        except Exception as e:
            console.print(f"[muted]  ⎿ auto-save failed: {e}[/muted]")

    async def _activate_snapshot(
        self,
        snapshot: SessionSnapshot,
        header: str,
    ) -> None:
        session = Session(config=self.config)
        await session.initialize()

        session.session_id = snapshot.session_id
        session.created_at = snapshot.created_at
        session.updated_at = snapshot.updated_at
        session.turn_count = snapshot.turn_count
        session.context_manager.total_usage = snapshot.total_usage
        session.tool_registry.restore_state(snapshot.tool_state)

        for msg in snapshot.messages:
            role = msg.get("role")
            if role == "user":
                session.context_manager.add_user_message(msg.get("content", ""))
            elif role == "assistant":
                session.context_manager.add_assistant_message(
                    msg.get("content", ""), msg.get("tool_calls")
                )
            elif role == "tool":
                session.context_manager.add_tool_result(
                    msg.get("tool_call_id", ""), msg.get("content", "")
                )

        # Restored messages still count against the context window. Real usage
        # only arrives with the next response, so seed an estimate - otherwise
        # the first turn skips the compaction check and can overflow.
        restored_tokens = session.context_manager.estimate_prompt_tokens()
        session.context_manager.set_latest_usage(
            TokenUsage(
                prompt_tokens=restored_tokens,
                total_tokens=restored_tokens,
            )
        )

        session.approval_manager.confirmation_callback = self.tui.handle_confirmation

        await self.agent.session.client.close()
        await self.agent.session.mcp_manager.shutdown()
        self.agent.session = session

        console.print(f"[success]{header}[/success]")
        self.tui.print_transcript(session.context_manager.get_messages())

    def _get_tool_kind(self, tool_name: str) -> str | None:
        tool_kind = None
        tool = self.agent.session.tool_registry.get(tool_name)
        if not tool:
            tool_kind = None

        tool_kind = tool.kind.value

        return tool_kind

    async def _process_message(self, message: str) -> str | None:
        if not self.agent:
            return None

        message = expand_mentions(message, self.config.cwd)

        assistant_streaming = False
        final_response: str | None = None

        self.tui.start_thinking()
        try:
            async for event in self.agent.run(message):
                if event.type == AgentEventType.TEXT_DELTA:
                    content = event.data.get("content", "")
                    if not assistant_streaming:
                        self.tui.begin_assistant()
                        assistant_streaming = True
                    self.tui.stream_assistant_delta(content)
                elif event.type == AgentEventType.TEXT_COMPLETE:
                    final_response = event.data.get("content")
                    if assistant_streaming:
                        self.tui.end_assistant()
                        assistant_streaming = False
                elif event.type == AgentEventType.AGENT_ERROR:
                    self.tui.stop_thinking()
                    error = event.data.get("error", "Unknown error")
                    console.print(f"\n[error]Error: {error}[/error]")
                elif event.type == AgentEventType.TOOL_CALL_START:
                    tool_name = event.data.get("name", "unknown")
                    tool_kind = self._get_tool_kind(tool_name)
                    self.tui.tool_call_start(
                        event.data.get("call_id", ""),
                        tool_name,
                        tool_kind,
                        event.data.get("arguments", {}),
                    )
                elif event.type == AgentEventType.TOOL_CALL_COMPLETE:
                    tool_name = event.data.get("name", "unknown")
                    tool_kind = self._get_tool_kind(tool_name)
                    self.tui.tool_call_complete(
                        event.data.get("call_id", ""),
                        tool_name,
                        tool_kind,
                        event.data.get("success", False),
                        event.data.get("output", ""),
                        event.data.get("error"),
                        event.data.get("metadata"),
                        event.data.get("diff"),
                        event.data.get("truncated", False),
                        event.data.get("exit_code"),
                    )
                    self.tui.start_thinking()
        finally:
            self.tui.stop_thinking()

        return final_response

    async def _handle_command(self, command: str) -> bool:
        cmd = command.lower().strip()
        parts = cmd.split(maxsplit=1)
        cmd_name = parts[0]
        cmd_args = parts[1] if len(parts) > 1 else ""
        if cmd_name == "/exit" or cmd_name == "/quit":
            return False
        elif command == "/help":
            self.tui.show_help()
        elif command == "/clear":
            self.agent.session.context_manager.clear()
            self.agent.session.loop_detector.clear()
            console.print("[success]Conversation cleared [/success]")
        elif command == "/config":
            console.print("\n[bold]Current Configuration[/bold]")
            console.print(f"  Model: {self.config.model_name}")
            console.print(f"  Temperature: {self.config.temperature}")
            console.print(f"  Approval: {self.config.approval.value}")
            console.print(f"  Working Dir: {self.config.cwd}")
            console.print(f"  Max Turns: {self.config.max_turns}")
            console.print(f"  Hooks Enabled: {self.config.hooks_enabled}")
        elif cmd_name == "/model":
            if cmd_args:
                self.config.model_name = cmd_args
                console.print(f"[success]Model changed to: {cmd_args} [/success]")
            else:
                console.print(f"Current model: {self.config.model_name}")
        elif cmd_name == "/approval":
            if cmd_args:
                try:
                    approval = ApprovalPolicy(cmd_args)
                    self.config.approval = approval
                    console.print(
                        f"[success]Approval policy changed to: {cmd_args} [/success]"
                    )
                except:
                    console.print(
                        f"[error]Incorrect approval policy: {cmd_args} [/error]"
                    )
                    console.print(
                        f"Valid options: {', '.join(p for p in ApprovalPolicy)}"
                    )
            else:
                console.print(f"Current approval policy: {self.config.approval.value}")
        elif cmd_name == "/stats":
            stats = self.agent.session.get_stats()
            console.print("\n[bold]Session Statistics [/bold]")
            for key, value in stats.items():
                console.print(f"   {key}: {value}")
        elif cmd_name == "/tools":
            tools = self.agent.session.tool_registry.get_tools()
            console.print(f"\n[bold]Available tools ({len(tools)}) [/bold]")
            for tool in tools:
                console.print(f"  • {tool.name}")
        elif cmd_name == "/mcp":
            mcp_servers = self.agent.session.mcp_manager.get_all_servers()
            console.print(f"\n[bold]MCP Servers ({len(mcp_servers)}) [/bold]")
            for server in mcp_servers:
                status = server["status"]
                status_color = "green" if status == "connected" else "red"
                console.print(
                    f"  • {server['name']}: [{status_color}]{status}[/{status_color}] ({server['tools']} tools)"
                )
        elif cmd_name == "/save":
            self.persistence.save_session(self._snapshot())
            console.print(
                f"[success]Session saved: {self.agent.session.session_id}[/success]"
            )
        elif cmd_name == "/sessions":
            sessions = self.persistence.list_sessions()
            if not sessions:
                console.print("\n[muted]No saved sessions[/muted]")
            else:
                console.print("\n[bold]Saved Sessions[/bold]")
                for s in sessions:
                    console.print(
                        f"  • {s['session_id']} (turns: {s['turn_count']}, updated: {s['updated_at']})"
                    )
        elif cmd_name == "/resume":
            if not cmd_args:
                console.print("[error]Usage: /resume <session_id>[/error]")
            else:
                snapshot = self.persistence.load_session(cmd_args)
                if not snapshot:
                    console.print("[error]Session does not exist[/error]")
                else:
                    await self._activate_snapshot(
                        snapshot,
                        f"Resumed session: {snapshot.session_id}"
                        f" ({snapshot.turn_count} turns)",
                    )
        elif cmd_name == "/checkpoint":
            checkpoint_id = self.persistence.save_checkpoint(self._snapshot())
            console.print(f"[success]Checkpoint created: {checkpoint_id}[/success]")
        elif cmd_name == "/checkpoints":
            checkpoints = self.persistence.list_checkpoints()
            if not checkpoints:
                console.print("\n[muted]No checkpoints[/muted]")
            else:
                console.print("\n[bold]Checkpoints[/bold]")
                for c in checkpoints:
                    console.print(
                        f"  • {c['checkpoint_id']} (turns: {c['turn_count']}, created: {c['created_at']})"
                    )
        elif cmd_name == "/restore":
            if not cmd_args:
                console.print("[error]Usage: /restore <checkpoint_id>[/error]")
            else:
                snapshot = self.persistence.load_checkpoint(cmd_args)
                if not snapshot:
                    console.print("[error]Checkpoint does not exist[/error]")
                else:
                    await self._activate_snapshot(
                        snapshot,
                        f"Restored checkpoint: {cmd_args}"
                        f" (session: {snapshot.session_id})",
                    )
        else:
            console.print(f"[error]Unknown command: {cmd_name}[/error]")

        return True


@click.command()
@click.argument("prompt", required=False)
@click.option(
    "--cwd",
    "-c",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Current working directory",
)
@click.option(
    "--continue",
    "continue_last",
    is_flag=True,
    help="Resume the most recently saved session",
)
@click.option(
    "--no-autosave",
    is_flag=True,
    help="Do not save the session automatically after each turn",
)
def main(
    prompt: str | None,
    cwd: Path | None,
    continue_last: bool,
    no_autosave: bool,
):
    try:
        config = load_config(cwd=cwd)
    except Exception as e:
        console.print(f"[error]Configuration Error: {e}[/error]")

    errors = config.validate()

    if errors:
        for error in errors:
            console.print(f"[error]{error}[/error]")

        sys.exit(1)

    cli = CLI(
        config,
        auto_save=not no_autosave,
        continue_last=continue_last,
    )

    if prompt:
        result = asyncio.run(cli.run_single(prompt))
        if result is None:
            sys.exit(1)
    else:
        asyncio.run(cli.run_interactive())


main()

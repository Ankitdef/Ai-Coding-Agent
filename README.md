<div align="center">

# 🤖 AI Coding Agent

**An AI pair-programmer that runs in your terminal. It reads, writes and searches your code, runs shell commands and browses the web, and asks before it does anything risky.**

Inspired by tools like Claude Code, built from scratch in Python.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![OpenAI SDK](https://img.shields.io/badge/OpenAI--compatible-API-412991?logo=openai&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-supported-orange)
![Rich](https://img.shields.io/badge/UI-Rich%20TUI-green)

</div>

---

## ✨ What it does

You type a task in plain English, such as *"find why the login test fails and fix it"*. The agent then:

1. **Thinks** about the task using a large language model.
2. **Calls tools** to read files, search the code, run commands or look things up online.
3. **Reads the results** and decides on the next step.
4. **Repeats** until the task is done, streaming its progress live in the terminal.

Every file write and shell command goes through a **safety layer**, so nothing destructive happens without your approval.

---

## 🚀 Features

| Area | What you get |
|---|---|
| 🧠 **Agent loop** | Multi-turn reasoning with tool calling, streaming responses, and a configurable turn limit |
| 🛠️ **Built-in tools** | `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`, `shell`, `web_search`, `web_fetch`, `memory`, `todo` |
| 🧩 **Subagents** | Specialised helper agents (codebase investigator, code reviewer) with their own tools and limits |
| 🔌 **MCP support** | Connect any [Model Context Protocol](https://modelcontextprotocol.io) server over stdio or HTTP/SSE and use its tools |
| 🛡️ **Safety & approvals** | Approval modes (`on-request`, `auto`, `never`, `yolo`), dangerous-command blocking (`rm -rf /`, `mkfs`, `shutdown`...), path checks, and secrets removed from the shell environment |
| 📚 **Context management** | Automatic conversation compression near the token limit, tool-output pruning, and token usage tracking |
| 🔁 **Loop detection** | Notices when the agent repeats itself and steers it out of the loop |
| 💾 **Sessions** | Auto-save after every turn, `--continue` to resume, named sessions, and checkpoints you can restore |
| 🪝 **Hooks** | Run your own scripts before/after the agent, before/after each tool call, or on errors |
| 🎨 **Terminal UI** | Rich formatting, live tool-call display, coloured diffs for file edits, and `@file` mentions |

---

## 🏗️ Architecture

```
            ┌──────────────┐
  You  ───▶ │   main.py    │  CLI, slash commands, session handling
            └──────┬───────┘
                   ▼
            ┌──────────────┐     ┌──────────────────┐
            │    Agent     │◀──▶ │    LLM Client    │  OpenAI-compatible API
            │ (agent loop) │     │    (streaming)   │  (OpenRouter, OpenAI, ...)
            └──────┬───────┘     └──────────────────┘
                   │
     ┌─────────────┼──────────────┬───────────────┐
     ▼             ▼              ▼               ▼
┌─────────┐  ┌───────────┐  ┌───────────┐  ┌────────────┐
│ Context │  │   Tool    │  │  Safety / │  │   Hooks    │
│ Manager │  │ Registry  │  │ Approvals │  │   System   │
└─────────┘  └─────┬─────┘  └───────────┘  └────────────┘
                   │
      ┌────────────┼─────────────┐
      ▼            ▼             ▼
  Built-in      MCP tools    Subagents
   tools
```

### Project structure

```
├── main.py              # Entry point: CLI, interactive mode, slash commands
├── agent/               # Agent loop, events, session state, persistence
├── client/              # Streaming LLM client (OpenAI-compatible)
├── config/              # Config models (Pydantic) and TOML loader
├── context/             # Context manager, compression, loop detection
├── prompts/             # System prompt builder
├── safety/              # Approval policies and dangerous-command detection
├── tools/
│   ├── builtin/         # File, search, shell, web, memory and todo tools
│   ├── mcp/             # MCP client and manager
│   ├── subagents.py     # Subagent definitions
│   ├── discovery.py     # Loads custom tools from .ai-agent/tools
│   └── registry.py      # Tool registry
├── hooks/               # Hook system
├── ui/                  # Rich terminal UI and input handling
├── utils/               # Helpers (paths, text, errors, @mentions)
└── .ai-agent/           # Example project config and custom tool
```

---

## ⚙️ Setup

### 1. Prerequisites

- **Python 3.10+**
- An API key for any **OpenAI-compatible** provider, for example [OpenRouter](https://openrouter.ai) (has free models) or OpenAI
- *(Optional)* **Node.js**, only if you want to use MCP servers that run through `npx`

### 2. Clone the repository

```bash
git clone https://github.com/Ankitdef/Ai-Coding-Agent.git
cd Ai-Coding-Agent
```

### 3. Create a virtual environment and install dependencies

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 4. Add your API key

Copy the example file and fill in your key:

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

```env
API_KEY=your_api_key_here
BASE_URL=https://openrouter.ai/api/v1
```

> 🔒 `.env` is listed in `.gitignore`, so your key is never committed.

### 5. Run it

```bash
# Interactive mode
python main.py

# One-off task
python main.py "explain what this project does"

# Work on another folder
python main.py --cwd path/to/your/project

# Resume your last session
python main.py --continue
```

---

## 🧾 Configuration

Settings are read from TOML files and merged in this order (later ones win):

1. **Global:** your user config folder, e.g. `~/.config/ai-agent/config.toml` (Linux) or `%LOCALAPPDATA%\ai-agent\config.toml` (Windows)
2. **Project:** `.ai-agent/config.toml` in the working directory

You can also put an `AGENT.MD` file in the project root to give the agent project-specific instructions.

Example `.ai-agent/config.toml`:

```toml
approval = "on-request"     # on-request | on-failure | auto | never | yolo
max_turns = 100
hooks_enabled = true

[model]
name = "nvidia/nemotron-3-super-120b-a12b:free"
temperature = 0

# Run a script before every tool call
[[hooks]]
name = "log_tools"
trigger = "before_tool"     # before_agent | after_agent | before_tool | after_tool | on_error
command = "python ./scripts/test_tool.py"

# Connect an MCP server
[mcp_servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
startup_timeout_sec = 60
```

### Custom tools

Drop a Python file into `.ai-agent/tools/` and the agent will discover and load it at start-up. See [.ai-agent/tools/test_tool.py](.ai-agent/tools/test_tool.py) for an example.

---

## ⌨️ Commands

Type these inside interactive mode:

| Command | Description |
|---|---|
| `/help` | Show help |
| `/config` | Show current configuration |
| `/model <name>` | Switch the model |
| `/approval <mode>` | Change the approval policy |
| `/tools` | List available tools |
| `/mcp` | Show MCP server status |
| `/stats` | Session statistics (turns, tokens) |
| `/clear` | Clear the conversation |
| `/save` | Save the session |
| `/sessions` | List saved sessions |
| `/resume <id>` | Resume a saved session |
| `/checkpoint` | Create a checkpoint |
| `/checkpoints` | List checkpoints |
| `/restore <id>` | Restore a checkpoint |
| `/exit` | Quit |

Mention a file with `@path/to/file` in your message to add it to the context.

---

## 🛡️ Approval modes

| Mode | Behaviour |
|---|---|
| `on-request` *(default)* | Runs known-safe commands (like `ls`, `git status`) and asks before anything else |
| `auto` / `on-failure` | Runs shell commands without asking |
| `never` | Never asks: known-safe commands run, everything else is rejected |
| `yolo` | Runs everything without asking, including blocked commands (use carefully) |

In every mode except `yolo`, destructive commands such as `rm -rf /`, `mkfs`, `dd if=` or `shutdown` are blocked. File edits outside the working directory always need confirmation. Variables matching `*KEY*`, `*TOKEN*` or `*SECRET*` are removed from the environment the shell tool sees.

---

## 🧰 Tech stack

- **Python** with `asyncio`
- **OpenAI Python SDK** for any OpenAI-compatible endpoint
- **Rich** + **prompt_toolkit** for the terminal UI
- **Pydantic** for config validation
- **FastMCP** for Model Context Protocol support
- **tiktoken** for token counting
- **Click** for the CLI
- **DDGS** + **httpx** for web search and fetching

---

## 🤝 Contributing

Issues and pull requests are welcome. Fork the repo, create a branch, and open a PR.

---

<div align="center">

Made by [Ankitdef](https://github.com/Ankitdef). If you find it useful, please give it a ⭐

</div>

# ttyd Agent Adapter

[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](https://www.python.org/)
[![Skill](https://img.shields.io/badge/agent_skill-SKILL.md-7c3aed.svg)](skills/ttyd-agent-adapter/SKILL.md)
[![ttyd](https://img.shields.io/badge/ttyd-WebSocket-0f766e.svg)](https://github.com/tsl0922/ttyd)

`ttyd-agent-adapter` is a portable agent skill for operating ttyd web terminals from a shell. The repository keeps the actual skill under `skills/ttyd-agent-adapter/`, with concise `SKILL.md` instructions plus a Python helper that connects to ttyd over WebSocket, runs commands, supports interactive sessions, handles common ttyd authentication flows, and transfers small files through the terminal when better transfer channels are unavailable.

## At a Glance

| Capability | What it does |
| --- | --- |
| Command mode | Runs a shell command through ttyd and returns cleaned output. |
| Interactive mode | Opens a browser-like terminal session from the local shell. |
| Auth helpers | Handles Basic auth, ttyd `/token`, cookies, custom headers, and terminal `login:` prompts. |
| File transfer | Uploads and downloads small files through terminal-safe base64 streams. |
| Agent workflow | Keeps protocol details in `skills/ttyd-agent-adapter/SKILL.md` and deterministic behavior in `skills/ttyd-agent-adapter/scripts/ttyd_run.py`. |

## Contents

```text
ttyd-agent-adapter/
├── README.md
├── LICENSE
├── tests/
│   └── test_ttyd_run.py
└── skills/
    └── ttyd-agent-adapter/
        ├── SKILL.md
        ├── requirements.txt
        └── scripts/
            └── ttyd_run.py
```

## Install

Install the Python dependency in the environment that will run the helper:

```bash
python -m pip install -r skills/ttyd-agent-adapter/requirements.txt
```

Install the skill by copying or symlinking the skill directory into your agent's skill search path.

```bash
mkdir -p ~/.agents/skills
ln -s /path/to/ttyd-agent-adapter/skills/ttyd-agent-adapter ~/.agents/skills/ttyd-agent-adapter
```

Agent-specific skill directories vary. For Codex, user skills commonly live under `~/.codex/skills`.

## Usage

Run one command:

```bash
python skills/ttyd-agent-adapter/scripts/ttyd_run.py HOST:PORT 'id'
python skills/ttyd-agent-adapter/scripts/ttyd_run.py http://HOST:PORT 'whoami; uname -a'
```

Run a multi-line script:

```bash
python skills/ttyd-agent-adapter/scripts/ttyd_run.py http://HOST:PORT --command-file commands.sh --timeout 30
```

Use HTTP Basic auth:

```bash
python skills/ttyd-agent-adapter/scripts/ttyd_run.py http://HOST:PORT --basic 'user:password' 'id'
python skills/ttyd-agent-adapter/scripts/ttyd_run.py http://user:password@HOST:PORT 'id'
```

Automate a terminal `login:` prompt:

```bash
python skills/ttyd-agent-adapter/scripts/ttyd_run.py HOST:PORT --login 'user:password' 'id'
```

Transfer small files through the terminal:

```bash
python skills/ttyd-agent-adapter/scripts/ttyd_run.py HOST:PORT --put ./local.txt:/tmp/local.txt
python skills/ttyd-agent-adapter/scripts/ttyd_run.py HOST:PORT --get /tmp/remote.txt:./remote.txt
```

Start an interactive session:

```bash
python skills/ttyd-agent-adapter/scripts/ttyd_run.py http://HOST:PORT
```

Interactive mode requires a POSIX-compatible local terminal. If you run through conda, activate the environment first or use `conda run --no-capture-output`.

## Requirements

Local:

- Python 3.9+
- `websockets`
- POSIX-compatible terminal for interactive mode

Remote command and transfer mode:

- POSIX-like shell
- `base64`
- `mktemp`

## Security

This tool sends credentials, cookies, tokens, terminal input, and command output to the ttyd endpoint you choose. Treat ttyd endpoints as remote shell access and avoid publishing real credentials in examples, logs, screenshots, or issue reports.

Use placeholders such as `HOST:PORT`, `user:password`, and `TOKEN_VALUE` in public reports.

## Test

```bash
python -m unittest discover -s tests
python -m py_compile skills/ttyd-agent-adapter/scripts/ttyd_run.py
```

## Repository Topics

Suggested GitHub topics:

```text
ttyd, websocket, terminal, agent-skill, codex-skill, automation, python
```

## License

MIT

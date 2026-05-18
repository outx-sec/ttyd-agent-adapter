---
name: ttyd-agent-adapter
description: Operate ttyd web terminals from the shell over WebSocket. Use when given a ttyd URL or host:port and you need command execution, interactive terminal access, small file transfer, authentication token handling, or ttyd WebSocket debugging without opening a browser.
---

# ttyd Agent Adapter

Use the bundled `scripts/ttyd_run.py` helper to connect to ttyd terminals through `/ws` with the `tty` WebSocket subprotocol.

Local requirement: Python 3 with `websockets` installed in the environment that runs the script.

```bash
python -m pip install -r /path/to/skill/requirements.txt
```

Remote requirement for command and transfer mode: a POSIX-like shell with `base64` and `mktemp`.

## Quick Start

Prefer command mode for agent work:

```bash
python /path/to/skill/scripts/ttyd_run.py HOST:PORT 'id'
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT 'whoami; uname -a'
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT --command-file commands.sh --timeout 30
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT 'id' --raw
```

Use interactive mode when the task needs a real terminal session or prompt handling:

```bash
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT
python /path/to/skill/scripts/ttyd_run.py HOST:PORT --interactive
```

Interactive mode requires a real local TTY. With conda, activate the environment first or use `conda run --no-capture-output -n ENV python ...`; plain `conda run -n ENV python ...` can capture or detach terminal I/O.

Transfer small files through the terminal when HTTP, SSH, or browser-supported transfer is unavailable:

```bash
python /path/to/skill/scripts/ttyd_run.py HOST:PORT --put ./local.txt:/tmp/local.txt
python /path/to/skill/scripts/ttyd_run.py HOST:PORT --get /tmp/remote.txt:./remote.txt
python /path/to/skill/scripts/ttyd_run.py HOST:PORT --put ./local.bin:/tmp/local.bin --max-transfer-size 20971520
```

## Authentication

For ttyd HTTP Basic auth, pass credentials directly or in the URL:

```bash
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT --basic 'user:password' 'id'
python /path/to/skill/scripts/ttyd_run.py http://user:password@HOST:PORT 'id'
```

With Basic auth, the script automatically fetches `/token` and sends the returned value as `AuthToken` in the ttyd init frame. Use explicit token or headers when a proxy or custom auth layer requires them:

```bash
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT --token 'TOKEN_VALUE' 'id'
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT --cookie 'sid=xxx' --fetch-token 'id'
python /path/to/skill/scripts/ttyd_run.py http://HOST:PORT --header 'X-Auth-Token: TOKEN' 'id'
```

If the terminal itself starts at a `login:` prompt, use `--login user:password` for command and transfer mode:

```bash
python /path/to/skill/scripts/ttyd_run.py HOST:PORT --login 'user:password' 'id'
python /path/to/skill/scripts/ttyd_run.py HOST:PORT --login 'user:password' --put ./local.txt:/tmp/local.txt
```

For `su`, `sudo`, or application-specific prompts, prefer interactive mode unless the user explicitly asks for automation.

## Workflow

1. Normalize the endpoint:
   - `HOST:PORT` -> `ws://HOST:PORT/ws`
   - `http://HOST:PORT` -> `ws://HOST:PORT/ws`
   - `https://HOST:PORT` -> `wss://HOST:PORT/ws`
2. Use command mode first; add `--raw` only when debugging prompt echo, shell startup, or protocol behavior.
3. Use `--command-file` for multi-line shell work instead of trying to type complex commands interactively.
4. Use `--basic`, URL credentials, `--token`, `--cookie`, or `--header` according to the auth layer in front of ttyd.
5. Use `--login` only for terminal login prompts; use interactive mode for other prompts.
6. Use `--put` and `--get` only for small files. The default transfer limit is 10MiB.

## Protocol Notes

The ttyd browser frontend connects to `/ws` using WebSocket subprotocol `tty`.

The first frame is raw JSON init data, not a resize frame:

```json
{"AuthToken":"","columns":180,"rows":60}
```

Terminal input is sent with ttyd message prefix `0` followed by keystrokes:

```text
0id\r
```

Incoming terminal output also uses prefix `0`; strip the first byte before decoding. Other prefixes may carry title, status, or flow-control data.

## Caveats

- If the WebSocket closes immediately, check that the init frame is raw JSON and that `subprotocols=["tty"]` is set.
- Do not assume Basic auth alone is enough for ttyd `-c`; fetch `/token` and send it as `AuthToken`.
- If no credentials are provided and the HTTP endpoint returns 401, add `--basic user:password` or URL credentials.
- Command output is extracted with start/end markers; use `--raw` when marker extraction hides useful PTY noise.
- Strip ANSI CSI and OSC sequences before parsing terminal output.
- Keep command mode non-interactive. Use interactive mode when the task truly needs PTY-style interaction.

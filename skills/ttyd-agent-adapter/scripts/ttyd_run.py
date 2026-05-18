#!/usr/bin/env python3
import argparse
import asyncio
import base64
import inspect
import json
import os
import re
import shlex
import signal
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

try:
    import websockets
except ModuleNotFoundError:
    websockets = None


ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC = re.compile(r"\x1b\].*?\x07")
LOGIN_PROMPT = re.compile(r"(?im)(^|\n|\s)([\w.-]+\s+)?login:\s*$|password:\s*$")
USER_PROMPT = re.compile(r"(?im)(^|\n|\s)([\w.-]+\s+)?login:\s*$")
PASS_PROMPT = re.compile(r"(?im)password:\s*$")


def endpoint_to_url_and_basic(endpoint):
    if "://" not in endpoint:
        endpoint = "http://" + endpoint
    parsed = urlparse(endpoint)
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    path = parsed.path if parsed.path and parsed.path != "/" else "/ws"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    basic = None
    if parsed.username is not None:
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
        basic = f"{username}:{password}"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{host}{path}{query}", basic


def script_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def token_url_from_ws_url(url):
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    path = parsed.path
    if path.endswith("/ws"):
        path = path[:-3] + "/token"
    else:
        path = "/token"
    return f"{scheme}://{parsed.netloc}{path}"


def root_url_from_ws_url(url):
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    base = parsed.path[:-3] if parsed.path.endswith("/ws") else "/"
    return f"{scheme}://{parsed.netloc}{base or '/'}"


def build_headers(args, url_basic):
    headers = []
    basic = args.basic or url_basic
    if basic:
        encoded = base64.b64encode(basic.encode()).decode()
        headers.append(("Authorization", f"Basic {encoded}"))
    if args.cookie:
        headers.append(("Cookie", args.cookie))
    for header in args.header or []:
        if ":" not in header:
            raise ValueError(f"invalid --header value, expected 'Name: value': {header}")
        name, value = header.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return headers


def fetch_token(url, headers, timeout):
    request = urllib.request.Request(token_url_from_ws_url(url))
    for name, value in headers:
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"failed to fetch ttyd token: {exc}") from exc
    try:
        token_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid ttyd token response; expected JSON containing a token field") from exc
    if "token" not in token_payload:
        raise RuntimeError("ttyd token response did not contain a token field")
    return token_payload["token"]


def check_auth_required(url, headers, timeout):
    request = urllib.request.Request(root_url_from_ws_url(url), method="HEAD")
    for name, value in headers:
        request.add_header(name, value)
    try:
        urllib.request.urlopen(request, timeout=timeout).close()
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except urllib.error.URLError:
        return False
    return False


def clean_output(data):
    text = data.decode("utf-8", "ignore")
    text = ANSI_CSI.sub("", text)
    text = ANSI_OSC.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def extract_command_output(text, start_marker, end_marker):
    output, _rc = extract_command_result(text, start_marker, end_marker)
    return output


def extract_command_result(text, start_marker, end_marker):
    start = text.find(start_marker)
    if start < 0:
        return text, None
    start = text.find("\n", start)
    if start < 0:
        return "", None
    start += 1
    end = text.find(end_marker, start)
    if end < 0:
        return text[start:].strip("\n"), None
    output = text[start:end].strip("\n")
    rc = None
    match = re.match(rf"{re.escape(end_marker)}:(\d+)", text[end:])
    if match:
        rc = int(match.group(1))
    return output, rc


def looks_like_login_prompt(data):
    return bool(LOGIN_PROMPT.search(clean_output(data)))


def login_prompt_error():
    return RuntimeError(
        "remote ttyd is waiting at a login/password prompt; "
        "command mode requires an already-authenticated shell, use interactive mode instead"
    )


def split_credentials(value, option_name):
    if ":" not in value:
        raise ValueError(f"{option_name} expects user:password")
    username, password = value.split(":", 1)
    if not username:
        raise ValueError(f"{option_name} expects a non-empty username")
    return username, password


def has_user_prompt(data):
    return bool(USER_PROMPT.search(clean_output(data)))


def has_password_prompt(data):
    return bool(PASS_PROMPT.search(clean_output(data)))


async def recv_tty_data(ws, timeout):
    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
    if isinstance(msg, str):
        msg = msg.encode()
    return msg[1:] if msg else b""


async def automate_login(ws, initial_chunks, username, password, timeout):
    chunks = initial_chunks
    deadline = time.monotonic() + timeout
    sent_user = False
    sent_password = False

    while time.monotonic() < deadline:
        data = b"".join(chunks)
        text = clean_output(data)
        if "Login incorrect" in text or "Authentication failure" in text:
            raise RuntimeError("login failed")
        if not sent_user and has_user_prompt(data):
            await ws.send(("0" + username + "\r").encode())
            sent_user = True
        elif sent_user and not sent_password and has_password_prompt(data):
            await ws.send(("0" + password + "\r").encode())
            sent_password = True
        elif sent_password:
            try:
                chunks.append(await recv_tty_data(ws, 1.0))
                continue
            except asyncio.TimeoutError:
                return chunks

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            chunks.append(await recv_tty_data(ws, min(remaining, 2.0)))
        except asyncio.TimeoutError:
            if sent_password:
                return chunks

    raise RuntimeError("timed out while automating login")


def init_payload(token, columns, rows):
    return json.dumps(
        {"AuthToken": token, "columns": columns, "rows": rows},
        separators=(",", ":"),
    ).encode()


def connect(url, headers):
    if websockets is None:
        raise RuntimeError(
            "missing Python package 'websockets'. Install dependencies in the "
            "environment used to run this script, for example: "
            f"python -m pip install -r {os.path.join(script_dir(), 'requirements.txt')}"
        )
    kwargs = {"subprotocols": ["tty"]}
    params = inspect.signature(websockets.connect).parameters
    if "extra_headers" in params:
        kwargs["extra_headers"] = headers
    elif "additional_headers" in params:
        kwargs["additional_headers"] = headers
    return websockets.connect(url, **kwargs)


def make_markers():
    marker_prefix = "__TTYD_"
    marker_suffix = f"{int(time.time() * 1000)}__"
    start_marker = marker_prefix + "START_" + marker_suffix
    end_marker = marker_prefix + "END_" + marker_suffix
    return marker_prefix, marker_suffix, start_marker, end_marker


def build_command_payload(command, marker_prefix, marker_suffix):
    command_b64 = base64.b64encode(command.encode()).decode()
    return (
        "HISTFILE=/dev/null; history -d $((HISTCMD-1)) 2>/dev/null; set +o history 2>/dev/null; "
        "__ttyd_cmd=$(mktemp); "
        f"printf '%s' '{command_b64}' | base64 -d > \"$__ttyd_cmd\"; "
        f"printf '\\n%s%s%s\\n' '{marker_prefix}' 'START_' '{marker_suffix}'; "
        "${SHELL:-sh} \"$__ttyd_cmd\"; "
        "__ttyd_rc=$?; "
        "rm -f \"$__ttyd_cmd\"; "
        f"printf '\\n%s%s%s:%s\\n' '{marker_prefix}' 'END_' '{marker_suffix}' \"$__ttyd_rc\"\r"
    )


def build_put_stream_start(marker_suffix):
    return (
        "HISTFILE=/dev/null; history -d $((HISTCMD-1)) 2>/dev/null; set +o history 2>/dev/null; "
        "stty -echo 2>/dev/null; "
        "__ttyd_b64=$(mktemp); "
        "cat > \"$__ttyd_b64\"\r"
    )


def build_put_stream_finish(remote_path, marker_prefix, marker_suffix):
    remote_q = shlex.quote(remote_path)
    return (
        "stty echo 2>/dev/null; "
        f"printf '\\n%s%s%s\\n' '{marker_prefix}' 'START_' '{marker_suffix}'; "
        f"base64 -d \"$__ttyd_b64\" > {remote_q}; "
        "__ttyd_rc=$?; "
        "rm -f \"$__ttyd_b64\"; "
        f"printf '\\n%s%s%s:%s\\n' '{marker_prefix}' 'END_' '{marker_suffix}' \"$__ttyd_rc\"\r"
    )


async def run_payload(url, headers, token, payload, timeout, columns, rows, raw, start_marker, end_marker, login, progress=None):
    async with connect(url, headers) as ws:
        await ws.send(init_payload(token, columns, rows))
        initial_chunks = []
        initial_deadline = time.monotonic() + 0.8
        while True:
            remaining = initial_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, str):
                msg = msg.encode()
            if msg:
                initial_chunks.append(msg[1:])
            if looks_like_login_prompt(b"".join(initial_chunks)):
                if not login:
                    raise login_prompt_error()
                username, password = login
                if progress:
                    progress("automating remote login prompt")
                initial_chunks = await automate_login(ws, initial_chunks, username, password, timeout)
                break

        if progress:
            progress("sending terminal payload")
        await ws.send(("0" + payload).encode())

        chunks = initial_chunks[:]
        deadline = time.monotonic() + timeout
        wait_started = time.monotonic()
        next_progress = wait_started + 2.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for ttyd command marker")

            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(msg, str):
                msg = msg.encode()
            if msg:
                chunks.append(msg[1:])

            data = b"".join(chunks)
            if looks_like_login_prompt(data) and end_marker.encode() not in data:
                raise login_prompt_error()
            if end_marker.encode() in data:
                break
            now = time.monotonic()
            if progress and now >= next_progress:
                progress(f"waiting for remote completion ({int(now - wait_started)}s)")
                next_progress = now + 2.0

    text = clean_output(data)
    if raw:
        return text
    output, rc = extract_command_result(text, start_marker, end_marker)
    if rc not in (None, 0):
        raise RuntimeError(f"remote command exited with status {rc}:\n{output}")
    return output


async def prepare_session(ws, token, timeout, login, columns, rows, progress=None):
    await ws.send(init_payload(token, columns, rows))
    initial_chunks = []
    initial_deadline = time.monotonic() + 0.8
    while True:
        remaining = initial_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if isinstance(msg, str):
            msg = msg.encode()
        if msg:
            initial_chunks.append(msg[1:])
        if looks_like_login_prompt(b"".join(initial_chunks)):
            if not login:
                raise login_prompt_error()
            username, password = login
            if progress:
                progress("automating remote login prompt")
            initial_chunks = await automate_login(ws, initial_chunks, username, password, timeout)
            break
    return initial_chunks


async def run_command(url, headers, token, command, timeout, columns, rows, raw, login, progress=None):
    marker_prefix, marker_suffix, start_marker, end_marker = make_markers()
    payload = build_command_payload(command, marker_prefix, marker_suffix)
    return await run_payload(url, headers, token, payload, timeout, columns, rows, raw, start_marker, end_marker, login, progress=progress)


def split_transfer_spec(spec, option_name):
    if ":" not in spec:
        raise ValueError(f"{option_name} expects SRC:DST, got {spec!r}")
    src, dst = spec.split(":", 1)
    if not src or not dst:
        raise ValueError(f"{option_name} expects non-empty SRC:DST, got {spec!r}")
    return src, dst


def wrap_base64(data, line_length=3072):
    encoded = base64.b64encode(data).decode()
    return "\n".join(encoded[i : i + line_length] for i in range(0, len(encoded), line_length)) + "\n"


def stderr_progress(message):
    print(f"[ttyd] {message}", file=sys.stderr, flush=True)


async def put_file(url, headers, token, local_path, remote_path, timeout, columns, rows, raw, max_size, login, progress=None):
    if progress:
        progress(f"reading {local_path}")
    with open(local_path, "rb") as fh:
        data = fh.read()
    if len(data) > max_size:
        raise RuntimeError(f"local file is {len(data)} bytes; exceeds --max-transfer-size {max_size}")
    if progress:
        progress(f"encoding {len(data)} bytes for upload")

    marker_prefix, marker_suffix, start_marker, end_marker = make_markers()
    encoded = wrap_base64(data).encode()
    async with connect(url, headers) as ws:
        initial_chunks = await prepare_session(ws, token, timeout, login, columns, rows, progress=progress)
        if progress:
            progress("starting remote receiver with terminal echo disabled")
        await ws.send(("0" + build_put_stream_start(marker_suffix)).encode())
        await asyncio.sleep(0.3)

        sent = 0
        total = len(encoded)
        chunk_size = 16384
        next_progress = 0
        if progress:
            progress(f"sending {total} base64 bytes")
        for offset in range(0, total, chunk_size):
            chunk = encoded[offset : offset + chunk_size]
            await ws.send(b"0" + chunk)
            sent += len(chunk)
            percent = 100 if total == 0 else int(sent * 100 / total)
            if progress and (percent >= next_progress or sent == total):
                progress(f"upload stream {sent}/{total} bytes ({percent}%)")
                next_progress = percent + 10
            await asyncio.sleep(0)

        if progress:
            progress(f"closing remote receiver and finalizing {remote_path}")
        await ws.send(b"0\x04")
        await asyncio.sleep(0.2)
        await ws.send(("0" + build_put_stream_finish(remote_path, marker_prefix, marker_suffix)).encode())

        chunks = initial_chunks[:]
        deadline = time.monotonic() + timeout
        wait_started = time.monotonic()
        next_wait_progress = wait_started + 2.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for ttyd upload marker")
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(msg, str):
                msg = msg.encode()
            if msg:
                chunks.append(msg[1:])
            data_out = b"".join(chunks)
            if end_marker.encode() in data_out:
                break
            now = time.monotonic()
            if progress and now >= next_wait_progress:
                progress(f"waiting for remote decode ({int(now - wait_started)}s)")
                next_wait_progress = now + 2.0

    text = clean_output(data_out)
    if raw:
        return text
    output, rc = extract_command_result(text, start_marker, end_marker)
    if rc not in (None, 0):
        raise RuntimeError(f"remote upload exited with status {rc}:\n{output}")
    if not raw and not output:
        return f"uploaded {len(data)} bytes to {remote_path}"
    return output


async def get_file(url, headers, token, remote_path, local_path, timeout, columns, rows, raw, max_size, login, progress=None):
    remote_q = shlex.quote(remote_path)
    if progress:
        progress(f"requesting remote file {remote_path}")
    output = await run_command(
        url,
        headers,
        token,
        f"base64 < {remote_q}",
        timeout,
        columns,
        rows,
        raw,
        login,
        progress=progress,
    )
    if raw:
        return output
    if progress:
        progress(f"received {len(output)} base64 characters")
    try:
        data = base64.b64decode("".join(output.split()), validate=True)
    except Exception as exc:
        raise RuntimeError(f"remote output was not valid base64:\n{output}") from exc
    if len(data) > max_size:
        raise RuntimeError(f"remote file is {len(data)} bytes; exceeds --max-transfer-size {max_size}")
    if progress:
        progress(f"writing {len(data)} bytes to {local_path}")
    parent = os.path.dirname(local_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(local_path, "wb") as fh:
        fh.write(data)
    return f"downloaded {len(data)} bytes to {local_path}"


async def interactive(url, headers, token, columns, rows, timeout, max_size):
    if termios is None or tty is None:
        raise RuntimeError("interactive mode requires a POSIX-compatible local terminal")
    async with connect(url, headers) as ws:
        await ws.send(init_payload(token, columns, rows))

        old_term = None
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
        if sys.stdin.isatty():
            old_term = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        capture = None
        local_mode = False
        local_buffer = bytearray()

        def request_stop(*_args):
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except NotImplementedError:
                pass

        async def recv_loop():
            nonlocal capture
            try:
                async for msg in ws:
                    if isinstance(msg, str):
                        msg = msg.encode()
                    if not msg:
                        continue
                    if msg[:1] == b"0":
                        if capture is not None:
                            capture["chunks"].append(msg[1:])
                            data = b"".join(capture["chunks"])
                            if capture["end"].encode() in data and not capture["future"].done():
                                capture["future"].set_result(data)
                        else:
                            os.write(stdout_fd, msg[1:])
            finally:
                stop.set()

        async def run_interactive_payload(payload, start_marker, end_marker):
            nonlocal capture
            future = loop.create_future()
            capture = {"chunks": [], "end": end_marker, "future": future}
            try:
                await ws.send(("0" + payload).encode())
                data = await asyncio.wait_for(future, timeout=timeout)
            finally:
                capture = None
            text = clean_output(data)
            output, rc = extract_command_result(text, start_marker, end_marker)
            if rc not in (None, 0):
                raise RuntimeError(f"remote command exited with status {rc}:\n{output}")
            return output

        async def run_interactive_put(data, remote_path, start_marker, end_marker, marker_prefix, marker_suffix):
            nonlocal capture
            future = loop.create_future()
            capture = {"chunks": [], "end": end_marker, "future": future}
            try:
                await ws.send(("0" + build_put_stream_start(marker_suffix)).encode())
                await asyncio.sleep(0.3)
                encoded = wrap_base64(data).encode()
                total = len(encoded)
                sent = 0
                next_progress = 0
                os.write(stdout_fd, (f"[ttyd] sending {total} base64 bytes\r\n").encode())
                for offset in range(0, total, 16384):
                    chunk = encoded[offset : offset + 16384]
                    await ws.send(b"0" + chunk)
                    sent += len(chunk)
                    percent = 100 if total == 0 else int(sent * 100 / total)
                    if percent >= next_progress or sent == total:
                        os.write(stdout_fd, (f"[ttyd] upload stream {sent}/{total} bytes ({percent}%)\r\n").encode())
                        next_progress = percent + 10
                    await asyncio.sleep(0)
                os.write(stdout_fd, (f"[ttyd] closing remote receiver and finalizing {remote_path}\r\n").encode())
                await ws.send(b"0\x04")
                await asyncio.sleep(0.2)
                await ws.send(("0" + build_put_stream_finish(remote_path, marker_prefix, marker_suffix)).encode())
                raw_data = await asyncio.wait_for(future, timeout=timeout)
            finally:
                capture = None
            text = clean_output(raw_data)
            output, rc = extract_command_result(text, start_marker, end_marker)
            if rc not in (None, 0):
                raise RuntimeError(f"remote upload exited with status {rc}:\n{output}")
            return output

        async def local_put(local_path, remote_path):
            os.write(stdout_fd, (f"\r\n[ttyd] reading {local_path}\r\n").encode())
            with open(local_path, "rb") as fh:
                data = fh.read()
            if len(data) > max_size:
                raise RuntimeError(f"local file is {len(data)} bytes; exceeds --max-transfer-size {max_size}")
            os.write(stdout_fd, (f"[ttyd] uploading {len(data)} bytes to {remote_path}\r\n").encode())
            marker_prefix, marker_suffix, start_marker, end_marker = make_markers()
            output = await run_interactive_put(data, remote_path, start_marker, end_marker, marker_prefix, marker_suffix)
            return output or f"uploaded {len(data)} bytes to {remote_path}"

        async def local_get(remote_path, local_path):
            os.write(stdout_fd, (f"\r\n[ttyd] downloading {remote_path}\r\n").encode())
            marker_prefix, marker_suffix, start_marker, end_marker = make_markers()
            remote_q = shlex.quote(remote_path)
            payload = build_command_payload(f"base64 < {remote_q}", marker_prefix, marker_suffix)
            output = await run_interactive_payload(payload, start_marker, end_marker)
            os.write(stdout_fd, (f"[ttyd] received {len(output)} base64 characters\r\n").encode())
            try:
                data = base64.b64decode("".join(output.split()), validate=True)
            except Exception as exc:
                raise RuntimeError(f"remote output was not valid base64:\n{output}") from exc
            if len(data) > max_size:
                raise RuntimeError(f"remote file is {len(data)} bytes; exceeds --max-transfer-size {max_size}")
            os.write(stdout_fd, (f"[ttyd] writing {len(data)} bytes to {local_path}\r\n").encode())
            parent = os.path.dirname(local_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(local_path, "wb") as fh:
                fh.write(data)
            return f"downloaded {len(data)} bytes to {local_path}"

        async def handle_local_command(line):
            parts = shlex.split(line)
            if not parts:
                return ""
            if parts[0] in ("help", "?"):
                return "local commands: put LOCAL REMOTE | get REMOTE LOCAL | help"
            if parts[0] == "put" and len(parts) == 3:
                return await local_put(parts[1], parts[2])
            if parts[0] == "get" and len(parts) == 3:
                return await local_get(parts[1], parts[2])
            return "unknown local command; use: put LOCAL REMOTE | get REMOTE LOCAL | help"

        async def read_stdin():
            future = loop.create_future()

            def on_readable():
                try:
                    future.set_result(os.read(stdin_fd, 4096))
                except Exception as exc:
                    future.set_exception(exc)

            loop.add_reader(stdin_fd, on_readable)
            try:
                return await future
            finally:
                loop.remove_reader(stdin_fd)

        async def stdin_loop():
            nonlocal local_mode, local_buffer
            while not stop.is_set():
                data = await read_stdin()
                if not data:
                    if not sys.stdin.isatty():
                        stop.set()
                    return
                outgoing = bytearray()
                for byte in data:
                    if local_mode:
                        if byte == 20:
                            local_mode = False
                            local_buffer.clear()
                            os.write(stdout_fd, b"\r\n[ttyd] local mode off\r\n")
                        elif byte in (3, 27):
                            local_mode = False
                            local_buffer.clear()
                            os.write(stdout_fd, b"^C\r\n")
                        elif byte in (10, 13):
                            line = local_buffer.decode("utf-8", "ignore")
                            local_buffer.clear()
                            os.write(stdout_fd, b"\r\n")
                            try:
                                result = await handle_local_command(line)
                                if result:
                                    os.write(stdout_fd, ("\r\n[ttyd] " + result + "\r\n").encode())
                            except Exception as exc:
                                os.write(stdout_fd, ("\r\n[ttyd] ERROR: " + str(exc) + "\r\n").encode())
                            os.write(stdout_fd, b"[ttyd] ")
                        elif byte in (8, 127):
                            if local_buffer:
                                local_buffer.pop()
                                os.write(stdout_fd, b"\b \b")
                        else:
                            local_buffer.append(byte)
                            os.write(stdout_fd, bytes([byte]))
                    elif byte == 20:
                        if outgoing:
                            await ws.send(b"0" + bytes(outgoing))
                            outgoing.clear()
                        local_mode = True
                        local_buffer.clear()
                        os.write(stdout_fd, b"\r\n[ttyd] ")
                    else:
                        outgoing.append(byte)
                if outgoing:
                    await ws.send(b"0" + bytes(outgoing))

        tasks = [asyncio.create_task(recv_loop()), asyncio.create_task(stdin_loop())]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            if old_term is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)


def main() -> int:
    parser = argparse.ArgumentParser(description="Interact with a ttyd WebSocket terminal.")
    parser.add_argument("endpoint", help="ttyd endpoint, e.g. HOST:PORT, http://HOST:PORT, or http://user:pass@HOST:PORT")
    parser.add_argument("command", nargs="?", help="shell command to run")
    parser.add_argument("--command-file", help="read shell commands from a local file")
    parser.add_argument("--put", action="append", help="upload a file as LOCAL:REMOTE using base64 over the terminal")
    parser.add_argument("--get", action="append", help="download a file as REMOTE:LOCAL using base64 over the terminal")
    parser.add_argument("--max-transfer-size", type=int, default=10 * 1024 * 1024, help="maximum bytes for --put/--get, default 10MiB")
    parser.add_argument("--basic", help="HTTP Basic credentials as user:password")
    parser.add_argument("--login", help="automate a ttyd backend login prompt as user:password before command/transfer mode")
    parser.add_argument("--token", default="", help="value to send in ttyd init JSON as AuthToken")
    parser.add_argument("--fetch-token", action="store_true", help="fetch /token before opening the WebSocket")
    parser.add_argument("--no-fetch-token", action="store_true", help="do not auto-fetch /token when Basic auth is used")
    parser.add_argument("--cookie", help="Cookie header value")
    parser.add_argument("--header", action="append", help="extra HTTP header, e.g. 'X-Token: value'")
    parser.add_argument("--interactive", "-i", action="store_true", help="force interactive mode")
    parser.add_argument("--raw", action="store_true", help="show raw cleaned PTY output in command mode")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--columns", type=int, default=180)
    parser.add_argument("--rows", type=int, default=60)
    args = parser.parse_args()

    url, url_basic = endpoint_to_url_and_basic(args.endpoint)
    try:
        headers = build_headers(args, url_basic)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        login = split_credentials(args.login, "--login") if args.login else None
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    token = args.token
    should_fetch_token = args.fetch_token or args.basic or url_basic
    if not token and not args.no_fetch_token and should_fetch_token:
        try:
            token = fetch_token(url, headers, min(args.timeout, 10.0))
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    elif not headers and check_auth_required(url, headers, min(args.timeout, 5.0)):
        print(
            "ERROR: ttyd requires HTTP Basic authentication. "
            "Pass `--basic user:password` or use `http://user:password@HOST:PORT`.",
            file=sys.stderr,
        )
        return 2

    command = args.command
    if args.command_file:
        with open(args.command_file, "r", encoding="utf-8") as fh:
            command = fh.read()
    if command and (args.put or args.get):
        print("ERROR: command cannot be combined with --put or --get", file=sys.stderr)
        return 2

    try:
        if args.interactive or command is None:
            if args.put or args.get:
                for spec in args.put or []:
                    local_path, remote_path = split_transfer_spec(spec, "--put")
                    print(
                        asyncio.run(
                            put_file(
                                url,
                                headers,
                                token,
                                local_path,
                                remote_path,
                                args.timeout,
                                args.columns,
                                args.rows,
                                args.raw,
                                args.max_transfer_size,
                                login,
                                progress=stderr_progress,
                            )
                        )
                    )
                for spec in args.get or []:
                    remote_path, local_path = split_transfer_spec(spec, "--get")
                    print(
                        asyncio.run(
                            get_file(
                                url,
                                headers,
                                token,
                                remote_path,
                                local_path,
                                args.timeout,
                                args.columns,
                                args.rows,
                                args.raw,
                                args.max_transfer_size,
                                login,
                                progress=stderr_progress,
                            )
                        )
                    )
                return 0
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                print(
                    "ERROR: interactive mode requires a real terminal TTY.\n"
                    "Activate the Python environment that has dependencies installed, then run "
                    "this script with `python`; for conda run, use "
                    "`conda run --no-capture-output -n ENV python ...`.",
                    file=sys.stderr,
                )
                return 2
            asyncio.run(interactive(url, headers, token, args.columns, args.rows, args.timeout, args.max_transfer_size))
            return 0
        output = asyncio.run(run_command(url, headers, token, command, args.timeout, args.columns, args.rows, args.raw, login))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

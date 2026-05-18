import importlib.util
import pathlib
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills" / "ttyd-agent-adapter" / "scripts" / "ttyd_run.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ttyd_run", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TtydRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_endpoint_to_url_and_basic(self):
        cases = {
            "HOST:7681": ("ws://host:7681/ws", None),
            "http://HOST:7681": ("ws://host:7681/ws", None),
            "https://HOST:7681": ("wss://host:7681/ws", None),
            "ws://HOST:7681/ws": ("ws://host:7681/ws", None),
            "wss://HOST:7681/ws": ("wss://host:7681/ws", None),
            "http://user:pass@HOST:7681": ("ws://host:7681/ws", "user:pass"),
            "http://[::1]:7681": ("ws://[::1]:7681/ws", None),
            "https://HOST:7681/proxy/ws?x=1": ("wss://host:7681/proxy/ws?x=1", None),
        }
        for endpoint, expected in cases.items():
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.mod.endpoint_to_url_and_basic(endpoint), expected)

    def test_token_url_from_ws_url(self):
        cases = {
            "ws://host:7681/ws": "http://host:7681/token",
            "wss://host:7681/ws": "https://host:7681/token",
            "wss://host:7681/proxy/ws": "https://host:7681/proxy/token",
            "ws://host:7681/custom": "http://host:7681/token",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self.mod.token_url_from_ws_url(url), expected)

    def test_build_headers(self):
        args = types.SimpleNamespace(
            basic="user:pass",
            cookie="sid=placeholder",
            header=["X-Test: value"],
        )
        self.assertEqual(
            self.mod.build_headers(args, None),
            [
                ("Authorization", "Basic dXNlcjpwYXNz"),
                ("Cookie", "sid=placeholder"),
                ("X-Test", "value"),
            ],
        )

    def test_build_headers_rejects_invalid_header(self):
        args = types.SimpleNamespace(basic=None, cookie=None, header=["invalid"])
        with self.assertRaises(ValueError):
            self.mod.build_headers(args, None)

    def test_clean_output_strips_ansi_and_normalizes_lines(self):
        data = b"\x1b[31mred\x1b[0m\r\nline\rnext\x1b]0;title\x07"
        self.assertEqual(self.mod.clean_output(data), "red\nline\nnext")

    def test_extract_command_result(self):
        text = "noise\nSTART\npayload\nEND:7\nprompt"
        self.assertEqual(self.mod.extract_command_result(text, "START", "END"), ("payload", 7))

    def test_split_credentials(self):
        self.assertEqual(self.mod.split_credentials("user:p:a:s:s", "--login"), ("user", "p:a:s:s"))

    def test_split_credentials_rejects_missing_separator(self):
        with self.assertRaises(ValueError):
            self.mod.split_credentials("user", "--login")

    def test_split_transfer_spec(self):
        self.assertEqual(self.mod.split_transfer_spec("a:b:c", "--put"), ("a", "b:c"))


if __name__ == "__main__":
    unittest.main()

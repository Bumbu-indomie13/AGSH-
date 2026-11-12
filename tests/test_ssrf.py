import json
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import agsh


class SSRFRegressionTests(unittest.TestCase):
    def start_server(self, respond):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def _handle_request(self):
                body = b""
                if self.command not in ("GET", "HEAD"):
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append({
                    "path": self.path,
                    "method": self.command,
                    "body": body,
                    "host": self.headers.get("Host"),
                    "authorization": self.headers.get("Authorization"),
                    "api_key": self.headers.get("X-API-Key"),
                    "accept": self.headers.get("Accept"),
                })
                status, headers, body = respond(self)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._handle_request()

            def do_POST(self):
                self._handle_request()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, requests

    def test_non_global_and_ipv4_mapped_destinations_are_blocked(self):
        addresses = (
            "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.2",
            "169.254.169.254", "0.0.0.0", "::1", "fe80::1", "fc00::1",
            "::ffff:127.0.0.1", "64:ff9b::7f00:1",
        )
        for ip in addresses:
            with self.subTest(ip=ip):
                runtime = agsh.Runtime()
                with patch.object(runtime, "_resolve_ips", return_value={ip}):
                    with self.assertRaises(agsh.AGSHError):
                        runtime._pin_destination(f"http://[{ip}]/", "fetch", {}) if ":" in ip else runtime._pin_destination(f"http://{ip}/", "fetch", {})

    def test_nat64_well_known_prefix_allows_only_public_embedded_ipv4(self):
        runtime = agsh.Runtime()
        with patch.object(runtime, "_resolve_ips", return_value={"64:ff9b::808:808"}):
            parsed = runtime._pin_destination("https://nat64.example/", "fetch", {})
        self.assertEqual(parsed["host"], "nat64.example")

    def test_every_dns_answer_must_be_global(self):
        runtime = agsh.Runtime()
        with patch.object(runtime, "_resolve_ips", return_value={"8.8.8.8", "169.254.169.254"}):
            with self.assertRaisesRegex(agsh.AGSHError, "private/metadata"):
                runtime._pin_destination("https://mixed.example/", "fetch", {})

    def test_malformed_and_userinfo_urls_are_rejected_before_dns(self):
        runtime = agsh.Runtime()
        with patch.object(runtime, "_resolve_ips") as resolve:
            for url in (
                "ftp://example.com/file",
                "http://user:pass@example.com/",
                "http://example.com:99999/",
                "http:///missing-host",
                "http://example.com\\@127.0.0.1/",
            ):
                with self.subTest(url=url), self.assertRaises(agsh.AGSHError):
                    runtime._pin_destination(url, "fetch", {})
            resolve.assert_not_called()
        with self.assertRaisesRegex(agsh.AGSHError, "Host header"):
            runtime._resolve_extra_headers("fetch", {"header": ["Host:127.0.0.1"]})

    def test_socket_connects_to_validated_ip_not_hostname_resolution(self):
        server, requests = self.start_server(
            lambda _handler: (200, {"Content-Type": "application/json"}, b'{"ok":true}')
        )
        runtime = agsh.Runtime(allow_private_net=True)
        url = f"http://synthetic.invalid:{server.server_port}/pinned"
        with patch.object(runtime, "_resolve_ips", return_value={"127.0.0.1"}) as resolve:
            req = agsh.urllib.request.Request(url)
            with runtime._safe_urlopen(req, timeout=3, verb="fetch") as response:
                self.assertEqual(json.loads(response.read()), {"ok": True})
        resolve.assert_called_once_with("synthetic.invalid")
        self.assertEqual(requests[0]["path"], "/pinned")
        self.assertEqual(requests[0]["host"], f"synthetic.invalid:{server.server_port}")

    def test_https_handler_keeps_default_certificate_verification(self):
        handler = agsh._PinnedHTTPSHandler({"synthetic.invalid": "203.0.113.8"})
        req = agsh.urllib.request.Request("https://synthetic.invalid/")
        with patch.object(handler, "do_open", return_value=object()) as do_open:
            handler.https_open(req)
        context = do_open.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_https_socket_uses_pin_but_keeps_original_tls_hostname(self):
        context = MagicMock()
        raw_socket, tls_socket = object(), object()
        context.wrap_socket.return_value = tls_socket
        conn = agsh._PinnedHTTPSConnection(
            "synthetic.invalid:443", pinned_ip="203.0.113.8",
            timeout=4, context=context)
        with patch.object(agsh.socket, "create_connection", return_value=raw_socket) as connect:
            conn.connect()
        connect.assert_called_once_with(("203.0.113.8", 443), 4, None)
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="synthetic.invalid")
        self.assertIs(conn.sock, tls_socket)

    def test_each_redirect_hop_is_revalidated_before_connection(self):
        runtime = agsh.Runtime(allow_private_net=True)
        target = "metadata.invalid"

        def respond(handler):
            if handler.path == "/start":
                # The first local test hop is explicitly allowed; make the following
                # redirect use the normal deny-private policy before it is evaluated.
                runtime.allow_private_net = False
                return 302, {"Location": f"http://{target}:{server.server_port}/latest/meta-data/"}, b""
            return 200, {}, b"unexpected"

        server, requests = self.start_server(respond)

        def resolve(host):
            if host == "start.invalid":
                return {"127.0.0.1"}
            if host == target:
                return {"169.254.169.254"}
            raise AssertionError(f"unexpected hostname resolution: {host}")

        url = f"http://start.invalid:{server.server_port}/start"
        with patch.object(runtime, "_resolve_ips", side_effect=resolve) as resolver:
            with self.assertRaisesRegex(agsh.AGSHError, "private/metadata"):
                runtime._safe_urlopen(agsh.urllib.request.Request(url), timeout=3, verb="fetch")
        self.assertEqual([request["path"] for request in requests], ["/start"])
        self.assertEqual(resolver.call_args_list[0].args, ("start.invalid",))
        self.assertEqual(resolver.call_args_list[1].args, (target,))

    def test_cross_origin_redirect_drops_caller_credentials_and_custom_headers(self):
        runtime = agsh.Runtime(allow_private_net=True)

        def respond(handler):
            if handler.path == "/start":
                return 302, {"Location": f"http://target.invalid:{server.server_port}/final"}, b""
            return 200, {"Content-Type": "application/json"}, b'{"ok":true}'

        server, requests = self.start_server(respond)
        url = f"http://start.invalid:{server.server_port}/start"
        with patch.object(runtime, "_resolve_ips", return_value={"127.0.0.1"}):
            req = agsh.urllib.request.Request(url, headers={
                "Authorization": "Bearer secret",
                "X-API-Key": "secret",
                "Accept": "application/json",
                "User-Agent": "ssrf-regression-test",
            })
            with runtime._safe_urlopen(req, timeout=3, verb="fetch") as response:
                self.assertEqual(json.loads(response.read()), {"ok": True})
        self.assertEqual([request["path"] for request in requests], ["/start", "/final"])
        self.assertIsNone(requests[1]["authorization"])
        self.assertIsNone(requests[1]["api_key"])
        self.assertEqual(requests[1]["accept"], "application/json")

    def test_cross_origin_307_does_not_replay_post_body(self):
        runtime = agsh.Runtime(allow_private_net=True)

        def respond(handler):
            if handler.path == "/start":
                return 307, {"Location": f"http://target.invalid:{server.server_port}/final"}, b""
            return 200, {}, b"unexpected"

        server, requests = self.start_server(respond)
        url = f"http://start.invalid:{server.server_port}/start"
        with patch.object(runtime, "_resolve_ips", return_value={"127.0.0.1"}):
            req = agsh.urllib.request.Request(url, data=b"sensitive body", method="POST")
            with self.assertRaises(agsh.urllib.error.HTTPError):
                runtime._safe_urlopen(req, timeout=3, verb="post")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["method"], "POST")
        self.assertEqual(requests[0]["body"], b"sensitive body")


if __name__ == "__main__":
    unittest.main()

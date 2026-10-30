"""Public web boundaries; mocked sockets only, no network, model or QQ calls."""
import inspect
import io
import socket
import threading
import unittest
from unittest.mock import Mock, patch

import public_web as web


def ipv4(*octets):
    return ".".join(map(str, octets))


PUBLIC_IP = ipv4(93, 184, 216, 34)


def dns_row(ip=PUBLIC_IP, port=443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)


class Response:
    def __init__(self, body=b"hello", status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "text/plain", **(headers or {})}
        self.body = io.BytesIO(body)

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        return self.body.read(size)


class PublicWebTests(unittest.TestCase):
    def test_rejects_private_urls_credentials_protocols_and_ports(self):
        invalid = [
            "file:///etc/passwd", "ftp://example.com/a", "https://localhost/",
            "https://a.internal/", "https://127.0.0.1/", "https://[::1]/",
            "https://example.com:8443/", "https://user:password@example.com/",
            "https://example.com/\nInjected", "https://example.com\\@localhost/",
            "https://example.com/?access_token=synthetic", "https://example.com/?api%5fkey=synthetic",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(web.WebError):
                web.validate_url(value)

    def test_normalizes_idna_and_strips_fragment(self):
        target = web.validate_url("https://EXAMPLE.com:443/a?q=test#not-transmitted")
        self.assertEqual(target.url, "https://example.com/a?q=test")
        self.assertEqual(target.path, "/a?q=test")
        self.assertEqual(web.validate_url("https://例子.com/").host, "xn--fsqu00a.com")

    def test_percent_encoded_secrets_are_rejected(self):
        # These are synthetic tokens and credential-shaped strings only.
        for value in [
            "https://example.com/?q=sk%2Dabcdefghijklmnop",
            "https://example.com/path/password%3Dsynthetic",
            "https://example.com/?q=Bearer%20abcdefghijklmnop",
        ]:
            with self.subTest(value=value), self.assertRaises(web.WebError):
                web.validate_url(value)

    def test_private_dns_and_tunnel_results_are_rejected(self):
        for ip in ["127.0.0.1", ipv4(10, 0, 0, 1), ipv4(172, 16, 1, 2), ipv4(192, 168, 1, 1), ipv4(169, 254, 169, 254),
                   ipv4(100, 64, 0, 1), "0.0.0.0", ipv4(224, 0, 0, 1), "::1", "fd00::1", "fe80::1", "fec0::1",
                   "::ffff:127.0.0.1", "2002:7f00:1::"]:
            with self.subTest(ip=ip), patch.object(web.socket, "getaddrinfo", return_value=[dns_row(ip)]):
                with self.assertRaisesRegex(web.WebError, "private_address_blocked"):
                    web.public_addresses("example.com", 443)

    def test_mixed_dns_rejects_entire_answer_even_after_four_public_results(self):
        records = [dns_row(ipv4(93, 184, 216, i)) for i in range(30, 35)] + [dns_row(ipv4(10, 0, 0, 1))]
        with patch.object(web.socket, "getaddrinfo", return_value=records):
            with self.assertRaisesRegex(web.WebError, "private_address_blocked"):
                web.public_addresses("example.com", 443)

    def test_dns_failure_has_stable_error(self):
        with patch.object(web.socket, "getaddrinfo", side_effect=socket.gaierror("private resolver detail")):
            with self.assertRaisesRegex(web.WebError, "^dns_unavailable$"):
                web.public_addresses("example.com", 443)

    def test_connects_to_pinned_ip_once_with_original_tls_hostname_and_fixed_get(self):
        connection = Mock()
        connection.getresponse.return_value = Response()
        raw_socket, tls_socket = Mock(), Mock()
        tls_context = Mock()
        tls_context.wrap_socket.return_value = tls_socket
        # A hypothetical second resolution would rebind to a private address.
        with patch.object(web.socket, "getaddrinfo", side_effect=[[dns_row()], [dns_row("127.0.0.1")]]) as dns, \
             patch.object(web.socket, "socket", return_value=raw_socket), \
             patch.object(web.ssl, "create_default_context", return_value=tls_context), \
             patch.object(web.http.client, "HTTPConnection", return_value=connection):
            web._open(web.validate_url("https://example.com/page?q=test"), web.time.monotonic() + 5)
        self.assertEqual(dns.call_count, 1)
        raw_socket.connect.assert_called_once_with((PUBLIC_IP, 443))
        tls_context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.com", do_handshake_on_connect=False)
        tls_socket.do_handshake.assert_called_once()
        self.assertIs(connection.sock, tls_socket)
        args, kwargs = connection.request.call_args
        connection._web_deadline_timer.cancel()
        self.assertEqual(args, ("GET", "/page?q=test"))
        self.assertNotIn("body", kwargs)
        self.assertFalse({"cookie", "authorization", "proxy-authorization"} & {key.lower() for key in kwargs["headers"]})

    def test_redirect_resolves_again_and_blocks_private_target_before_connect(self):
        connection = Mock()
        connection.getresponse.return_value = Response(status=302, headers={"Location": "http://redirect.example/a"})
        with patch.object(web.socket, "getaddrinfo", side_effect=[[dns_row(port=80)], [dns_row(ipv4(10, 0, 0, 1), 80)]]) as dns, \
             patch.object(web.socket, "socket") as create_socket, \
             patch.object(web.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(web.WebError, "private_address_blocked"):
                web.fetch_bytes("http://example.com/start")
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(create_socket.call_count, 1)
        connection.close.assert_called_once()

    def test_redirect_url_is_validated_before_next_request(self):
        connection = Mock()
        response = Response(status=302, headers={"Location": "http://127.0.0.1/secret"})
        with patch.object(web, "_open", return_value=(connection, response)) as opened:
            with self.assertRaises(web.WebError):
                web.fetch_bytes("https://example.com/start")
        self.assertEqual(opened.call_count, 1)
        connection.close.assert_called_once()

    def test_redirect_limit_closes_all_connections(self):
        pairs = [(Mock(), Response(status=302, headers={"Location": "/next"})) for _ in range(5)]
        with patch.object(web, "_open", side_effect=pairs) as opened:
            with self.assertRaisesRegex(web.WebError, "redirect_limit"):
                web.fetch_bytes("https://example.com/start")
        self.assertEqual(opened.call_count, 5)
        for connection, _ in pairs:
            connection.close.assert_called_once()

    def test_size_cap_with_and_without_content_length(self):
        for response in [Response(headers={"Content-Length": "9"}), Response(body=b"a" * 9)]:
            connection = Mock()
            with self.subTest(response=response.headers), patch.object(web, "MAX_BYTES", 8), \
                 patch.object(web, "_open", return_value=(connection, response)):
                with self.assertRaisesRegex(web.WebError, "page_too_large"):
                    web.fetch_bytes("https://example.com/")
            connection.close.assert_called_once()

    def test_exact_size_boundary_succeeds(self):
        connection = Mock()
        with patch.object(web, "MAX_BYTES", 8), patch.object(web, "_open", return_value=(connection, Response(body=b"a" * 8))):
            self.assertEqual(web.fetch_bytes("https://example.com/")[2], b"a" * 8)
        connection.close.assert_called_once()

    def test_compressed_or_nontext_body_rejected(self):
        for headers, code in [({"Content-Encoding": "gzip"}, "compressed_response_not_supported"),
                              ({"Content-Type": "application/octet-stream"}, "unsupported_content_type")]:
            with self.subTest(headers=headers), patch.object(web, "_open", return_value=(Mock(), Response(headers=headers))):
                with self.assertRaisesRegex(web.WebError, code):
                    web.fetch_bytes("https://example.com/")

    def test_methods_headers_and_credentials_not_exposed_as_parameters(self):
        self.assertEqual(list(inspect.signature(web.read_public_web).parameters), ["url"])
        self.assertEqual(list(inspect.signature(web.search_public_web).parameters), ["query", "max_results"])

    def test_sensitive_query_rejected_without_fetch(self):
        with patch.object(web, "fetch_bytes") as fetch:
            for query in ["sk-abcdefghijklmnop", "password=synthetic", "api_key:synthetic", "",
                          "sk%2Dabcdefghijklmnop", "password%253Dsynthetic", "Bearer+abcdefghijklmnop",
                          "password&#61;synthetic", "sk%252Dabcdefghijklmnop"]:
                with self.subTest(query=query):
                    self.assertFalse(web.search_public_web(query)["ok"])
            fetch.assert_not_called()

    def test_public_error_does_not_echo_private_url(self):
        result = web.read_public_web("https://user:synthetic-password@example.com/")
        self.assertFalse(result["ok"])
        self.assertNotIn("synthetic", str(result))

    def test_page_output_is_bounded_and_script_text_removed(self):
        body = b"<title>Title</title><script>secret-script</script><p>Visible page</p>"
        with patch.object(web, "fetch_bytes", return_value=("https://example.com/", "text/html", body)), \
             patch.object(web, "MAX_TEXT", 10):
            result = web.read_public_web("https://example.com/")
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Title")
        self.assertLessEqual(len(result["text"]), 10)
        self.assertTrue(result["truncated"])
        self.assertNotIn("secret-script", str(result))

    def test_stuck_dns_has_bounded_wait_and_at_most_four_outstanding_threads(self):
        gate = threading.Event()
        slots = threading.BoundedSemaphore(4)
        threads = []
        original_thread = threading.Thread

        def resolver(*args, **kwargs):
            gate.wait(2)
            return [dns_row()]

        def record_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch.object(web, "_DNS_SLOTS", slots), \
             patch.object(web.socket, "getaddrinfo", side_effect=resolver) as dns, \
             patch.object(web.threading, "Thread", side_effect=record_thread):
            try:
                for _ in range(4):
                    with self.assertRaisesRegex(web.WebError, "dns_timeout"):
                        web.public_addresses("example.com", 443, web.time.monotonic() + 0.03)
                with self.assertRaisesRegex(web.WebError, "dns_busy"):
                    web.public_addresses("example.com", 443, web.time.monotonic() + 0.03)
                self.assertEqual(len(threads), 4)
                self.assertEqual(dns.call_count, 4)
                self.assertTrue(all(thread.daemon for thread in threads))
            finally:
                gate.set()
                for thread in threads:
                    thread.join(2)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(web.public_addresses("example.com", 443)), 1)
            threads[-1].join(2)

    def test_expired_request_deadline_rejected(self):
        with self.assertRaisesRegex(web.WebError, "request_timeout"):
            web._remaining(web.time.monotonic() - 1)

    def test_expired_dns_request_does_not_start_a_worker(self):
        slots = threading.BoundedSemaphore(4)
        with patch.object(web, "_DNS_SLOTS", slots), patch.object(web.threading, "Thread") as worker:
            with self.assertRaisesRegex(web.WebError, "request_timeout"):
                web.public_addresses("example.com", 443, web.time.monotonic() - 1)
            worker.assert_not_called()

    def test_dns_worker_start_failure_releases_reserved_slot(self):
        slots = threading.BoundedSemaphore(4)
        with patch.object(web, "_DNS_SLOTS", slots), patch.object(web.threading, "Thread") as worker:
            worker.return_value.start.side_effect = RuntimeError("cannot start new thread")
            with self.assertRaises(web.WebError):
                web.public_addresses("example.com", 443, web.time.monotonic() + 1)
            acquired = [slots.acquire(blocking=False) for _ in range(4)]
            self.assertEqual(acquired, [True] * 4)
            for owned in acquired:
                if owned:
                    slots.release()

    def test_deadline_between_dns_and_connect_does_not_leak_socket(self):
        raw_socket = Mock()
        with patch.object(web, "public_addresses", return_value=[dns_row()[:3] + (dns_row()[4],)]), \
             patch.object(web.socket, "socket", return_value=raw_socket) as created, \
             patch.object(web, "_remaining", side_effect=web.WebError("request_timeout")):
            with self.assertRaises(web.WebError):
                web._open(web.validate_url("https://example.com/"), web.time.monotonic() + 1)
        if created.called:
            raw_socket.close.assert_called_once()

    def test_http_deadline_watchdog_closes_connection(self):
        connection = Mock()
        raw_socket = Mock()
        timers = []

        def timer_factory(interval, callback):
            timer = Mock()
            timer.expire = callback
            timers.append(timer)
            return timer

        with patch.object(web, "public_addresses", return_value=[dns_row(port=80)[:3] + (dns_row(port=80)[4],)]), \
             patch.object(web.socket, "socket", return_value=raw_socket), \
             patch.object(web.http.client, "HTTPConnection", return_value=connection), \
             patch.object(web.threading, "Timer", side_effect=timer_factory):
            web._open(web.validate_url("http://example.com/"), web.time.monotonic() + 5)
        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].daemon)
        timers[0].start.assert_called_once()
        timers[0].expire()
        raw_socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        raw_socket.close.assert_called_once()

    def test_http_deadline_watchdog_is_cancelled_on_success(self):
        connection = Mock()
        with patch.object(web, "_open", return_value=(connection, Response())):
            web.fetch_bytes("https://example.com/")
        connection._web_deadline_timer.cancel.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

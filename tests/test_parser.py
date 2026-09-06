import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import parser  # noqa: E402
import subscription  # noqa: E402


UUID = "123e4567-e89b-42d3-a456-426614174000"
PBK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def vless(
    host="example.com",
    port=443,
    query="encryption=none&security=tls&type=ws&host=cdn.example.com&path=%2Fws",
    fragment="node",
):
    suffix = f"?{query}" if query else ""
    remark = f"#{fragment}" if fragment else ""
    return f"vless://{UUID}@{host}:{port}{suffix}{remark}"


class VlessValidationTests(unittest.TestCase):
    def assertValid(self, link):
        valid, reason = parser.is_valid_vless(link)
        self.assertTrue(valid, reason)

    def assertInvalid(self, link):
        valid, _ = parser.is_valid_vless(link)
        self.assertFalse(valid)

    def test_valid_domain_ipv4_and_ipv6(self):
        self.assertValid(vless())
        self.assertValid(vless(host="8.8.8.8", query="encryption=none&type=tcp"))
        self.assertValid(vless(host="[2606:4700:4700::1111]", query="type=grpc&security=tls&sni=cloudflare.com"))

    def test_valid_reality(self):
        self.assertValid(
            vless(
                query=(
                    "encryption=none&security=reality&type=tcp&flow=xtls-rprx-vision"
                    f"&sni=www.microsoft.com&fp=chrome&pbk={PBK}&sid=0123abcd"
                )
            )
        )
        # Canonical 128-bit IDs with reserved UUID version bits are accepted by
        # VLESS/Xray and occur in the requested provider feeds.
        self.assertValid(
            vless().replace(UUID, "342ab7e4-5a89-0001-8809-304120d4aa83")
        )
        self.assertValid(vless(query="security=&type=tcp"))

    def test_reality_requires_sni_and_public_key(self):
        self.assertInvalid(vless(query="security=reality&type=tcp&sni=example.com"))
        self.assertInvalid(vless(query=f"security=reality&type=tcp&pbk={PBK}"))
        self.assertInvalid(vless(query=f"security=reality&type=tcp&sni=example.com&pbk=bad"))
        self.assertInvalid(vless(query=f"security=reality&type=tcp&sni=example.com&pbk={PBK}&sid=123"))

    def test_rejects_bad_authority_uuid_and_port(self):
        self.assertInvalid(f"vless://not-a-uuid@example.com:443?type=tcp")
        self.assertInvalid(f"vless://{UUID}:password@example.com:443?type=tcp")
        self.assertInvalid(f"vless://{UUID}@example.com?type=tcp")
        self.assertInvalid(f"vless://{UUID}@example.com:0?type=tcp")
        self.assertInvalid(f"vless://{UUID}@example.com:70000?type=tcp")
        self.assertInvalid(f"vless://{UUID}@example.com:nope?type=tcp")

    def test_rejects_non_public_or_malformed_hosts(self):
        for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.1.1", "999.1.1.1", "localhost", "bad_host.example"):
            with self.subTest(host=host):
                self.assertInvalid(vless(host=host, query="type=tcp"))

    def test_rejects_bad_query_values_and_escaping(self):
        self.assertInvalid(vless(query="encryption=aes-128-gcm&type=tcp"))
        self.assertInvalid(vless(query="security=unknown&type=tcp"))
        self.assertInvalid(vless(query="security=tls&type=unknown"))
        self.assertInvalid(vless(query="type=tcp&type=ws"))
        self.assertInvalid(vless(query="type=ws&path=%ZZ"))
        self.assertInvalid(vless(query="type=tcp&allowInsecure=maybe"))
        self.assertInvalid(vless(query="type=ws&host=http%3A%2F%2Fexample.com"))

    def test_host_header_may_have_a_numeric_port(self):
        self.assertValid(vless(query="type=ws&security=tls&host=cdn.example.com%3A8443"))
        self.assertInvalid(vless(query="type=ws&security=tls&host=cdn.example.com%3Anot-a-port"))

    def test_rejects_unescaped_path_whitespace_and_bad_remark_encoding(self):
        self.assertInvalid(f"vless://{UUID}@example.com:443/not-allowed?type=ws")
        self.assertInvalid(vless(fragment="bad remark"))
        self.assertInvalid(vless(fragment="bad%FFremark"))

    def test_parse_vless_info(self):
        info = parser.parse_vless_info(vless(fragment="My%20Node"))
        self.assertEqual(info["host"], "example.com")
        self.assertEqual(info["port"], "443")
        self.assertEqual(info["remark"], "My Node")
        self.assertEqual(info["type"], "ws")
        self.assertEqual(info["security"], "tls")


class ExtractionTests(unittest.TestCase):
    def test_plain_markdown_html_and_json_escaped(self):
        first = vless(fragment="one")
        second = vless(host="1.1.1.1", query="type=tcp&encryption=none", fragment="two")
        escaped = second.replace("/", "\\/").replace("&", "\\u0026")
        payload = f'<a href="{first.replace("&", "&amp;")}">x</a>\n[{first}]\n{{"url":"{escaped}"}}'
        self.assertEqual(parser.extract_configs(payload), [first, second])

    def test_json_string_with_escaped_newline(self):
        one = vless(fragment="one")
        two = vless(host="8.8.4.4", query="type=tcp", fragment="two")
        payload = '{"subscription": ' + repr(one + "\n" + two).replace("'", '"') + "}"
        self.assertEqual(parser.extract_configs(payload), [one, two])

    def test_whole_payload_base64_standard_and_urlsafe(self):
        link = vless()
        standard = base64.b64encode(link.encode()).decode()
        urlsafe = base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")
        self.assertEqual(parser.extract_configs(standard), [link])
        self.assertEqual(parser.extract_configs(urlsafe), [link])

    def test_line_by_line_base64_and_vless_only(self):
        one = vless(fragment="one")
        two = vless(host="8.8.4.4", query="type=tcp", fragment="two")
        payload = "\n".join(
            [
                "# comment",
                base64.b64encode(one.encode()).decode(),
                base64.b64encode(two.encode()).decode(),
                "trojan://password@example.com:443",
            ]
        )
        self.assertEqual(parser.extract_configs(payload), [one, two])

    def test_semantic_dedup_ignores_fragment_and_query_order(self):
        first = vless(query="type=ws&security=tls&encryption=none", fragment="one")
        second = vless(query="encryption=none&security=tls&type=ws", fragment="two")
        self.assertEqual(parser.deduplicate_configs([first, second, first]), [first])


class AsyncValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_checks_each_unique_endpoint_once(self):
        one = vless(fragment="one")
        duplicate_endpoint = vless(
            query="type=tcp&encryption=none",
            fragment="different-config",
        )
        unreachable = vless(
            host="example.net",
            query="type=tcp&encryption=none",
            fragment="down",
        )

        async def fake_check(host, port, timeout=3.0):
            return host == "example.com" and port == 443

        with patch.object(parser, "check_tcp", AsyncMock(side_effect=fake_check)) as check:
            result = await parser.validate_configs([one, duplicate_endpoint, unreachable], mode="tcp")
        self.assertEqual(result, [one, duplicate_endpoint])
        self.assertEqual(check.await_count, 2)

    async def test_fetch_all_tcp_uses_one_global_endpoint_check(self):
        shared_one = vless(fragment="source-one")
        shared_two = vless(fragment="source-two")
        other = vless(host="example.net", fragment="other")

        async def fake_category(session, key, mode="syntax"):
            links = [shared_one, other] if key == "one" else [shared_two]
            return {
                "key": key,
                "name": key,
                "configs": links,
                "raw_total": len(links),
                "filtered_total": len(links),
                "removed": 0,
                "errors": [],
            }

        with patch.object(parser, "fetch_category", AsyncMock(side_effect=fake_category)):
            with patch.object(parser, "check_tcp", AsyncMock(return_value=True)) as check:
                result = await parser.fetch_all(mode="tcp", categories=["one", "two"])
        self.assertEqual(check.await_count, 2)
        self.assertEqual(result["one"]["validation_mode"], "tcp")
        self.assertEqual(result["two"]["configs"], [shared_two])

    async def test_first_available_uses_valid_mirror(self):
        source = {
            "name": "test",
            "description": "test",
            "url_strategy": "first_available",
            "urls": ["https://bad.invalid", "https://good.invalid", "https://unused.invalid"],
        }
        good_link = vless()
        responses = ["", good_link, vless(host="example.net")]
        with patch.dict(parser.SOURCES, {"test": source}, clear=False):
            with patch.object(parser, "fetch_text", AsyncMock(side_effect=responses)) as fetch:
                result = await parser.fetch_category(None, "test", mode="syntax")
        self.assertEqual(result["configs"], [good_link])
        self.assertEqual(result["used_urls"], ["https://good.invalid"])
        self.assertEqual(fetch.await_count, 2)


class SubscriptionTests(unittest.TestCase):
    def test_smaller_build_deletes_stale_numbered_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "FULL_99.txt"
            stale.write_text("stale", encoding="utf-8")
            links = [vless(fragment="one"), vless(host="example.net", fragment="two")]
            subscription.save_aggregated_chunks(
                directory, "FULL.txt", "test", links, chunk_size=1
            )
            self.assertFalse(stale.exists())
            self.assertTrue((Path(directory) / "FULL_1.txt").exists())
            self.assertTrue((Path(directory) / "FULL_2.txt").exists())


class SourceRegistryTests(unittest.TestCase):
    def test_all_requested_providers_are_registered_and_aggregated(self):
        self.assertEqual(len(config.REQUIRED_PROVIDER_KEYS), 11)
        self.assertTrue(set(config.REQUIRED_PROVIDER_KEYS).issubset(config.SOURCES))
        self.assertTrue(
            set(config.REQUIRED_PROVIDER_KEYS).issubset(
                config.AGGREGATED_SUBS["WHITE_FULL"]["source_keys"]
            )
        )
        self.assertTrue(
            set(config.REQUIRED_PROVIDER_KEYS).issubset(
                config.AGGREGATED_SUBS["FULL"]["source_keys"]
            )
        )

    def test_admin_label_is_exact(self):
        bot_source = (ROOT / "src" / "bot.py").read_text(encoding="utf-8")
        self.assertIn('InlineKeyboardButton("«Проверка и очистка»"', bot_source)
        self.assertNotIn("Проверку и очистку", bot_source)


if __name__ == "__main__":
    unittest.main()

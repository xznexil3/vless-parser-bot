import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import bot  # noqa: E402
import config  # noqa: E402
import github_sync  # noqa: E402
import health  # noqa: E402
import parser  # noqa: E402
import subscription  # noqa: E402
from telegram.constants import KeyboardButtonStyle  # noqa: E402


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

    def test_discovery_extracts_only_likely_github_feed_urls(self):
        vless_feed = "https://raw.githubusercontent.com/owner/repo/main/vless.txt"
        config_page = "https://github.com/owner/repo/raw/main/configs.txt"
        config_feed = "https://raw.githubusercontent.com/owner/repo/main/configs.txt"
        payload = "\n".join(
            [
                vless_feed,
                config_page,
                "https://raw.githubusercontent.com/owner/repo/main/README.md",
                "https://example.com/vless.txt",
                "https://raw.githubusercontent.com/owner/repo/main/logo.png",
            ]
        )
        self.assertEqual(
            parser.extract_discovery_urls(payload),
            [vless_feed, config_feed],
        )


class AsyncValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_text_reads_every_stream_chunk(self):
        class Content:
            async def iter_chunked(self, _size):
                yield b"vless://first"
                yield b"-second"

        class Response:
            status = 200
            charset = "utf-8"
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        session = SimpleNamespace(get=lambda *args, **kwargs: Response())
        self.assertEqual(
            await parser.fetch_text(session, "https://example.invalid/feed"),
            "vless://first-second",
        )

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

    async def test_discovery_downloads_valid_vless_from_found_feeds(self):
        first_url = "https://raw.githubusercontent.com/new/feed/main/vless.txt"
        second_url = "https://raw.githubusercontent.com/new/feed/main/empty.txt"
        links = [
            vless(host=f"node{index}.example.com", port=4000 + index, fragment=str(index))
            for index in range(1, 7)
        ]

        async def fake_fetch(_session, url):
            if url == first_url:
                return "\n".join([*links, "vmess://ignored"])
            return "not a subscription"

        source = {"name": "discovery", "description": "test", "index_urls": []}
        with patch.object(
            parser,
            "discover_github_feed_urls",
            AsyncMock(return_value=([first_url, second_url], [])),
        ):
            with patch.object(parser, "fetch_text", AsyncMock(side_effect=fake_fetch)):
                result = await parser.fetch_discovered_category(
                    None,
                    "internet_discovery",
                    source,
                    "syntax",
                )
        self.assertEqual(result["configs"], links)
        self.assertEqual(result["used_urls"], [first_url])
        self.assertEqual(result["raw_total"], len(links))


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_works_but_domain_subscription_routes_do_not_exist(self):
        client = TestClient(TestServer(health.create_health_app()))
        await client.start_server()
        try:
            health_response = await client.get("/health")
            subscription_response = await client.get("/sub/BLACK_FULL_6.txt")
            self.assertEqual(health_response.status, 200)
            self.assertEqual(await health_response.text(), "OK - VLESS parser bot is running")
            self.assertEqual(subscription_response.status, 404)
        finally:
            await client.close()


class CommittedAggregateTests(unittest.TestCase):
    @staticmethod
    def _links(path):
        return [
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def test_numbered_chunks_exactly_reconstruct_each_aggregate(self):
        for key in ("BLACK_FULL", "WHITE_FULL", "FULL"):
            base = config.AGGREGATED_SUBS[key]["filename"]
            stem = base.removesuffix(".txt")
            files = sorted(
                ROOT.glob(f"{stem}_[0-9]*.txt"),
                key=lambda path: int(path.stem.rsplit("_", 1)[1]),
            )
            self.assertEqual(
                [int(path.stem.rsplit("_", 1)[1]) for path in files],
                list(range(1, len(files) + 1)),
            )
            reconstructed = []
            for path in files:
                part = self._links(path)
                self.assertLessEqual(len(part), subscription.CHUNK_SIZE)
                reconstructed.extend(part)
            self.assertEqual(reconstructed, self._links(ROOT / base))


class GithubSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_uses_one_ref_update_and_deletes_stale_chunks(self):
        class FakeResponse:
            def __init__(self, payload, status=200):
                self.payload = payload
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                return self.payload

            async def text(self):
                return str(self.payload)

        class FakeSession:
            def __init__(self):
                self.calls = []
                self.tree_payload = None
                self.blobs = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, **kwargs):
                self.calls.append(("GET", url))
                if "/git/ref/heads/" in url:
                    return FakeResponse({"object": {"sha": "old-commit"}})
                if url.endswith("/git/commits/old-commit"):
                    return FakeResponse({"tree": {"sha": "old-tree"}})
                if url.endswith("/git/trees/old-tree"):
                    return FakeResponse(
                        {
                            "tree": [
                                {"path": "BLACK_FULL.txt", "type": "blob", "sha": "old-main"},
                                {"path": "BLACK_FULL_6.txt", "type": "blob", "sha": "stale"},
                                {"path": "README.md", "type": "blob", "sha": "keep"},
                            ],
                            "truncated": False,
                        }
                    )
                raise AssertionError(url)

            def post(self, url, **kwargs):
                self.calls.append(("POST", url))
                if url.endswith("/git/blobs"):
                    self.blobs += 1
                    return FakeResponse({"sha": f"blob-{self.blobs}"}, 201)
                if url.endswith("/git/trees"):
                    self.tree_payload = kwargs["json"]
                    return FakeResponse({"sha": "new-tree"}, 201)
                if url.endswith("/git/commits"):
                    return FakeResponse({"sha": "new-commit"}, 201)
                raise AssertionError(url)

            def patch(self, url, **kwargs):
                self.calls.append(("PATCH", url))
                self.ref_payload = kwargs["json"]
                return FakeResponse({}, 200)

        session = FakeSession()
        with patch.object(github_sync.aiohttp, "ClientSession", return_value=session):
            urls = await github_sync.push_aggregated_subscriptions(
                {
                    "BLACK_FULL.txt": "main",
                    "BLACK_FULL_1.txt": "chunk",
                },
                "owner/repo",
                "secret",
            )

        self.assertEqual(len([call for call in session.calls if call[0] == "PATCH"]), 1)
        self.assertEqual(session.ref_payload, {"sha": "new-commit", "force": False})
        entries = session.tree_payload["tree"]
        self.assertIn(
            {"path": "BLACK_FULL_6.txt", "mode": "100644", "type": "blob", "sha": None},
            entries,
        )
        self.assertIn("BLACK_FULL_1.txt", urls)
        self.assertNotIn("README.md", {entry["path"] for entry in entries})


class SubscriptionTests(unittest.TestCase):
    def test_stale_chunks_remain_until_new_map_is_ready_then_are_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "FULL_99.txt"
            stale.write_text("stale", encoding="utf-8")
            links = [vless(fragment="one"), vless(host="example.net", fragment="two")]
            *_, chunks = subscription.save_aggregated_chunks(
                directory, "FULL.txt", "test", links, chunk_size=1
            )
            # Old keyboard callbacks remain valid during generation.
            self.assertTrue(stale.exists())
            active = {"FULL.txt", *(item[0] for item in chunks)}
            removed = subscription.cleanup_stale_aggregate_chunks(
                directory, ["FULL.txt"], active
            )
            self.assertEqual(removed, ["FULL_99.txt"])
            self.assertFalse(stale.exists())
            self.assertTrue((Path(directory) / "FULL_1.txt").exists())
            self.assertTrue((Path(directory) / "FULL_2.txt").exists())

    def test_activation_switches_complete_map_then_removes_stale_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "BLACK_FULL.txt").write_text("main", encoding="utf-8")
            (data_dir / "BLACK_FULL_1.txt").write_text("chunk", encoding="utf-8")
            stale = data_dir / "BLACK_FULL_6.txt"
            stale.write_text("stale", encoding="utf-8")
            results = {
                "BLACK_FULL.txt": {"count": 1, "content": "main"},
                "BLACK_FULL_1.txt": {"count": 1, "content": "chunk"},
            }
            chunk_map = {"BLACK_FULL.txt": [("BLACK_FULL_1.txt", "one", 1)]}
            with (
                patch.object(bot, "DATA_DIR", data_dir),
                patch.object(bot, "AGGREGATED_CACHE", {}),
                patch.object(bot, "AGGREGATED_CHUNKS", {}),
                patch.object(bot, "AGGREGATED_PROTO_COUNTS", {}),
            ):
                raw_url = (
                    "https://raw.githubusercontent.com/owner/repo/"
                    "main/BLACK_FULL_1.txt"
                )
                bot.activate_aggregated_configs(
                    results,
                    chunk_map,
                    {"BLACK_FULL.txt": {"vless": 1}},
                    {"BLACK_FULL_1.txt": raw_url},
                )
                self.assertEqual(bot.AGGREGATED_CHUNKS, chunk_map)
                self.assertEqual(
                    bot.AGGREGATED_CACHE["BLACK_FULL_1.txt"]["raw_url"],
                    raw_url,
                )
                self.assertFalse(stale.exists())

    def test_github_sync_recognizes_obsolete_numbered_chunks(self):
        groups = github_sync._managed_groups(
            {"BLACK_FULL.txt", "BLACK_FULL_1.txt", "FULL.txt", "FULL_1.txt"}
        )
        self.assertTrue(github_sync._is_managed_path("BLACK_FULL_6.txt", groups))
        self.assertTrue(github_sync._is_managed_path("FULL_99.txt", groups))
        self.assertFalse(github_sync._is_managed_path("collection.txt", groups))


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

    def test_aetris_is_fetched_into_black_and_full_aggregates(self):
        self.assertEqual(
            config.SOURCES["aetris_vpn"]["urls"],
            ["https://raw.githubusercontent.com/flaafix/AetrisVPN-black-list/main/configs.txt"],
        )
        self.assertIn("aetris_vpn", config.BLACK_SOURCE_KEYS)
        self.assertIn("aetris_vpn", config.AGGREGATED_SUBS["BLACK_FULL"]["source_keys"])
        self.assertIn("aetris_vpn", config.AGGREGATED_SUBS["FULL"]["source_keys"])
        self.assertNotIn("aetris_vpn", config.AGGREGATED_SUBS["WHITE_FULL"]["source_keys"])

    def test_bounded_internet_discovery_is_enabled_in_black_and_full(self):
        self.assertTrue(config.AUTO_DISCOVERY)
        self.assertTrue(config.SOURCES["internet_discovery"]["discovery"])
        self.assertIn("internet_discovery", config.BLACK_SOURCE_KEYS)
        self.assertIn(
            "internet_discovery",
            config.AGGREGATED_SUBS["FULL"]["source_keys"],
        )

    def test_admin_label_is_exact(self):
        bot_source = (ROOT / "src" / "bot.py").read_text(encoding="utf-8")
        self.assertIn('"«Проверка и очистка»"', bot_source)
        self.assertNotIn("Проверку и очистку", bot_source)

    def test_main_and_navigation_button_styles(self):
        main_buttons = {
            button.text: button.style
            for row in bot.main_keyboard(config.ADMIN_ID).inline_keyboard
            for button in row
        }
        self.assertEqual(main_buttons["«Профиль»"], KeyboardButtonStyle.PRIMARY)
        self.assertEqual(main_buttons["«Белые списки»"], KeyboardButtonStyle.SUCCESS)
        self.assertEqual(main_buttons["«Черные списки»"], KeyboardButtonStyle.SUCCESS)
        self.assertEqual(main_buttons["«Полный список»"], KeyboardButtonStyle.SUCCESS)
        self.assertEqual(main_buttons["«Помощь»"], KeyboardButtonStyle.DANGER)
        self.assertEqual(main_buttons["«Админ панель»"], KeyboardButtonStyle.DANGER)

        back = bot.back_keyboard("admin_panel").inline_keyboard[0][0]
        self.assertEqual(back.text, "«Назад»")
        self.assertEqual(back.style, KeyboardButtonStyle.PRIMARY)

    def test_only_published_github_txt_url_is_returned(self):
        raw_url = (
            "https://raw.githubusercontent.com/xznexil3/vless-parser-bot/"
            "main/BLACK_FULL_6.txt"
        )
        with patch.object(
            bot,
            "AGGREGATED_CACHE",
            {"BLACK_FULL_6.txt": {"raw_url": raw_url}},
        ):
            self.assertEqual(bot.get_raw_url("BLACK_FULL_6.txt"), raw_url)

    def test_unpublished_chunk_does_not_get_an_invented_github_url(self):
        with patch.object(
            bot,
            "AGGREGATED_CACHE",
            {"BLACK_FULL_6.txt": {"raw_url": ""}},
        ):
            self.assertEqual(bot.get_raw_url("BLACK_FULL_6.txt"), "")

    def test_obsolete_chunk_callback_resolves_to_current_aggregate(self):
        key, aggregate = bot.aggregate_for_filename("BLACK_FULL_6.txt")
        self.assertEqual(key, "BLACK_FULL")
        self.assertEqual(aggregate["filename"], "BLACK_FULL.txt")
        self.assertEqual(bot.aggregate_back_callback("BLACK_FULL_6.txt"), "black")
        with patch.object(bot, "AGGREGATED_CACHE", {"BLACK_FULL.txt": {}}):
            self.assertIsNone(bot.local_subscription_path("BLACK_FULL_6.txt"))

    def test_custom_subscription_builder_is_fully_removed(self):
        checked_files = [
            ROOT / "src" / "bot.py",
            ROOT / "src" / "config.py",
        ]
        self.assertFalse((ROOT / "src" / "server.py").exists())
        content = "\n".join(path.read_text(encoding="utf-8") for path in checked_files)
        for obsolete in (
            "CUSTOM_100",
            "TEMP_100",
            "handle_build_subscription",
            "build_subscription",
            "Собрать еще раз",
            "TEMP_FILES",
        ):
            self.assertNotIn(obsolete, content)


if __name__ == "__main__":
    unittest.main()

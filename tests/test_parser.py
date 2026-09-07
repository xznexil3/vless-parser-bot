import asyncio
import base64
import json
import sys
import tempfile
import unittest
from datetime import datetime
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
import paid_subscriptions  # noqa: E402
import provider_registry  # noqa: E402
import subscription  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402


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

    def test_discovery_requires_strict_github_vless_filters(self):
        accepted = (
            "https://raw.githubusercontent.com/owner/vpn-list/"
            "main/blacklist_vless_config.txt"
        )
        self.assertGreater(parser._discovery_url_score(accepted), 0)
        self.assertGreater(
            parser._discovery_url_score(
                "https://raw.githubusercontent.com/owner/public-vpn/main/general_configs.txt"
            ),
            0,
        )
        self.assertGreater(
            parser._discovery_url_score(
                "https://raw.githubusercontent.com/owner/xray-proxy/main/white-list.txt"
            ),
            0,
        )
        self.assertLess(
            parser._discovery_url_score(
                "https://raw.githubusercontent.com/owner/repo/main/configs.txt"
            ),
            0,
        )
        self.assertLess(
            parser._discovery_url_score("https://example.com/vless_config.txt"),
            0,
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

    async def test_tcp_latency_is_measured_in_milliseconds(self):
        moments = iter([10.0, 10.045])
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        fake_loop = SimpleNamespace(time=lambda: next(moments))
        with (
            patch.object(parser.asyncio, "get_running_loop", return_value=fake_loop),
            patch.object(
                parser.asyncio,
                "open_connection",
                AsyncMock(return_value=(None, writer)),
            ),
        ):
            latency = await parser.measure_tcp_latency("example.com", 443)
        self.assertEqual(latency, 45)
        writer.wait_closed.assert_awaited_once()

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

    async def test_discovery_only_returns_valid_candidates_for_admin_approval(self):
        first_url = "https://raw.githubusercontent.com/new/feed/main/vless.txt"
        second_url = "https://raw.githubusercontent.com/new/feed/main/empty.txt"
        links = [
            vless(host=f"node{index}.example.com", port=4000 + index, fragment=str(index))
            for index in range(1, 13)
        ]

        async def fake_fetch(_session, url, request_headers=None):
            del request_headers
            if url == first_url:
                return "\n".join([*links, "vmess://ignored"])
            return "not a subscription"

        with (
            patch.object(
                parser,
                "discover_github_feed_urls",
                AsyncMock(return_value=([first_url, second_url], [])),
            ),
            patch.object(parser, "fetch_text", AsyncMock(side_effect=fake_fetch)),
            patch.object(parser, "DISCOVERY_MAX_CONFIGS", 7),
        ):
            candidates, errors = await parser.find_public_github_candidates()
        self.assertFalse(errors)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], first_url)
        self.assertEqual(candidates[0]["valid_count"], 7)

    async def test_discovery_uses_all_query_groups_and_multiple_repo_files(self):
        source = {
            "search_queries": ["vpn subscription", "vpn whitelist"],
        }
        seen_searches = []

        async def fake_json(_session, url):
            if "/search/repositories?" in url:
                seen_searches.append(url)
                repo = "owner/vpn-one" if "subscription" in url else "owner/vpn-two"
                return {
                    "items": [
                        {
                            "full_name": repo,
                            "default_branch": "main",
                            "archived": False,
                            "disabled": False,
                            "private": False,
                        }
                    ]
                }
            if "/git/trees/" in url:
                return {
                    "tree": [
                        {"type": "blob", "path": "configs.txt", "size": 100},
                        {"type": "blob", "path": "white-list.txt", "size": 100},
                    ]
                }
            raise AssertionError(url)

        with (
            patch.object(parser, "_fetch_json", AsyncMock(side_effect=fake_json)),
            patch.object(parser, "DISCOVERY_MAX_REPOS", 2),
            patch.object(parser, "DISCOVERY_MAX_FEEDS", 4),
            patch.object(parser, "DISCOVERY_MAX_FILES_PER_REPO", 2),
        ):
            urls, errors = await parser.discover_github_feed_urls(None, source)
        self.assertEqual(len(seen_searches), 2)
        self.assertEqual(len(urls), 4)
        self.assertFalse(errors)
        self.assertTrue(any("vpn-one" in url for url in urls))
        self.assertTrue(any("vpn-two" in url for url in urls))


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

    async def test_provider_registry_file_is_created_without_force_push(self):
        class FakeResponse:
            def __init__(self, payload, status):
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
                self.put_payload = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, _url, **_kwargs):
                return FakeResponse({}, 404)

            def put(self, _url, **kwargs):
                self.put_payload = kwargs["json"]
                return FakeResponse({"content": {"sha": "new"}}, 201)

        session = FakeSession()
        with patch.object(github_sync.aiohttp, "ClientSession", return_value=session):
            url = await github_sync.push_text_file(
                "providers.json",
                '{"version": 1}\n',
                "owner/repo",
                "secret",
            )
        self.assertEqual(
            url,
            "https://raw.githubusercontent.com/owner/repo/main/providers.json",
        )
        self.assertEqual(session.put_payload["branch"], "main")
        self.assertNotIn("force", session.put_payload)
        self.assertNotIn("sha", session.put_payload)


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
            config_link = vless()
            results = {
                "BLACK_FULL.txt": {
                    "count": 1,
                    "content": "main",
                    "configs": [config_link],
                },
                "BLACK_FULL_1.txt": {"count": 1, "content": "chunk"},
            }
            chunk_map = {"BLACK_FULL.txt": [("BLACK_FULL_1.txt", "one", 1)]}
            with (
                patch.object(bot, "DATA_DIR", data_dir),
                patch.object(bot, "AGGREGATED_CACHE", {}),
                patch.object(bot, "AGGREGATED_CHUNKS", {}),
                patch.object(bot, "AGGREGATED_PROTO_COUNTS", {}),
            ):
                bot.activate_aggregated_configs(
                    results,
                    chunk_map,
                    {"BLACK_FULL.txt": {"vless": 1}},
                )
                self.assertEqual(bot.AGGREGATED_CHUNKS, chunk_map)
                self.assertEqual(
                    bot.AGGREGATED_CACHE["BLACK_FULL_1.txt"]["count"],
                    1,
                )
                self.assertEqual(
                    bot.AGGREGATED_CACHE["BLACK_FULL.txt"]["configs"],
                    [config_link],
                )
                self.assertNotIn(
                    "raw_url",
                    bot.AGGREGATED_CACHE["BLACK_FULL_1.txt"],
                )
                self.assertFalse(stale.exists())

    def test_github_sync_recognizes_obsolete_numbered_chunks(self):
        groups = github_sync._managed_groups(
            {"BLACK_FULL.txt", "BLACK_FULL_1.txt", "FULL.txt", "FULL_1.txt"}
        )
        self.assertTrue(github_sync._is_managed_path("BLACK_FULL_6.txt", groups))
        self.assertTrue(github_sync._is_managed_path("FULL_99.txt", groups))
        self.assertFalse(github_sync._is_managed_path("collection.txt", groups))


class ConfigConnectivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_result_edits_existing_caption_without_reuploading_banner(self):
        message = SimpleNamespace(
            photo=[SimpleNamespace()],
            edit_caption=AsyncMock(),
            edit_text=AsyncMock(),
        )
        query = SimpleNamespace(message=message)
        with patch.dict(config.CUSTOM_EMOJI_IDS, {}, clear=True):
            await bot.edit_config_message_content(query, "📶 Проверка", None)
        message.edit_caption.assert_awaited_once_with(
            caption="📶 Проверка",
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )
        message.edit_text.assert_not_awaited()

    async def test_ping_callback_edits_same_config_message_before_and_after_check(self):
        link = vless(fragment="Ping-Node")
        token = bot.config_token(link)
        query = SimpleNamespace(
            data=f"cfgping:FULL:{token}:0",
            from_user=SimpleNamespace(id=123, username="", first_name="User"),
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(bot=SimpleNamespace(), user_data={})
        with (
            patch.object(bot, "is_user_subscribed", AsyncMock(return_value=True)),
            patch.object(bot, "get_or_create_user"),
            patch.object(
                bot,
                "show_config_detail",
                AsyncMock(side_effect=[link, link]),
            ) as show,
            patch.object(bot, "measure_tcp_latency", AsyncMock(return_value=23)) as ping,
        ):
            await bot.callback_handler(update, context)
        self.assertEqual(show.await_count, 2)
        self.assertEqual(show.await_args_list[0].kwargs["ping_status"], "checking")
        self.assertEqual(show.await_args_list[1].kwargs["ping_status"], 23)
        ping.assert_awaited_once_with("example.com", 443, timeout=3.0)


class AdminCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_report_uses_unique_full_counts(self):
        query = SimpleNamespace(
            message=SimpleNamespace(
                edit_caption=AsyncMock(),
                edit_text=AsyncMock(),
            ),
            get_bot=lambda: SimpleNamespace(),
        )
        with (
            patch.object(bot, "current_vless_count", side_effect=[200, 177]),
            patch.object(bot, "update_cache", AsyncMock(return_value={})),
            patch.object(bot, "edit_message_with_banner", AsyncMock()) as edit,
        ):
            await bot.handle_admin_clean(query)
        report = edit.await_args.args[2]
        self.assertIn("Уникальных VLESS было: <b>200</b>", report)
        self.assertIn("Уникальных VLESS стало: <b>177</b>", report)
        self.assertNotIn("github_discovery", report)


class NotificationAndSupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_notice_replaces_previous_message_with_exact_format(self):
        fake_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
            delete_message=AsyncMock(),
        )
        fixed_datetime = SimpleNamespace(
            now=lambda tz: datetime(2026, 9, 7, 20, 28, tzinfo=tz)
        )
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings = {
                "update_notifications": True,
                "last_update_notification_id": 55,
            }
            with (
                patch.object(bot, "SETTINGS_FILE", settings_path),
                patch.object(bot, "SETTINGS", settings),
                patch.object(bot, "datetime", fixed_datetime),
                patch.dict(config.CUSTOM_EMOJI_IDS, {}, clear=True),
            ):
                await bot.notify_channel_update(fake_bot, 100, 200)

            fake_bot.send_message.assert_awaited_once_with(
                chat_id=config.CHANNEL_ID,
                text="✅  • Списки обновлены (+100)\n\n🕔07.09.2026 20:28 МСК",
                parse_mode=ParseMode.HTML,
            )
            fake_bot.delete_message.assert_awaited_once_with(
                chat_id=config.CHANNEL_ID,
                message_id=55,
            )
            self.assertEqual(settings["last_update_notification_id"], 77)
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8")),
                settings,
            )

    async def test_update_notice_shows_negative_difference(self):
        fake_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
            delete_message=AsyncMock(),
        )
        fixed_datetime = SimpleNamespace(
            now=lambda tz: datetime(2026, 9, 7, 20, 28, tzinfo=tz)
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(bot, "SETTINGS_FILE", Path(directory) / "settings.json"),
                patch.object(
                    bot,
                    "SETTINGS",
                    {"update_notifications": True, "last_update_notification_id": None},
                ),
                patch.object(bot, "datetime", fixed_datetime),
                patch.dict(config.CUSTOM_EMOJI_IDS, {}, clear=True),
            ):
                await bot.notify_channel_update(fake_bot, 200, 177)
        self.assertEqual(
            fake_bot.send_message.await_args.kwargs["text"],
            "✅  • Списки обновлены (-23)\n\n🕔07.09.2026 20:28 МСК",
        )

    async def test_disabled_update_notices_send_nothing(self):
        fake_bot = SimpleNamespace(
            send_message=AsyncMock(),
            delete_message=AsyncMock(),
        )
        with patch.object(
            bot,
            "SETTINGS",
            {"update_notifications": False, "last_update_notification_id": 55},
        ):
            await bot.notify_channel_update(fake_bot, 100, 200)
        fake_bot.send_message.assert_not_awaited()
        fake_bot.delete_message.assert_not_awaited()

    async def test_user_support_message_is_relayed_to_admin(self):
        message = SimpleNamespace(
            chat_id=123,
            message_id=456,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(
                id=123,
                username="crimson_user",
                full_name="Crimson Nick",
            ),
        )
        fake_bot = SimpleNamespace(
            send_message=AsyncMock(),
            copy_message=AsyncMock(),
        )
        context = SimpleNamespace(user_data={"support_mode": True}, bot=fake_bot)
        with patch.object(config, "ADMIN_IDS", {999}):
            handled = await bot.relay_support_message(update, context)

        self.assertTrue(handled)
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.await_args.kwargs["chat_id"], 999)
        self.assertEqual(
            fake_bot.send_message.await_args.kwargs["text"],
            "💬 <b>Новое обращение в поддержку</b>\n\n"
            "🆔 ID пользователя: <code>123</code>\n\n"
            "🔗 Юзернейм: <b>@crimson_user</b>\n\n"
            "👤 Ник: <b>Crimson Nick</b>",
        )
        fake_bot.copy_message.assert_awaited_once()
        copy_kwargs = fake_bot.copy_message.await_args.kwargs
        self.assertEqual(copy_kwargs["chat_id"], 999)
        self.assertEqual(copy_kwargs["from_chat_id"], 123)
        self.assertEqual(copy_kwargs["message_id"], 456)
        reply_button = copy_kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(reply_button.callback_data, "support_reply:123")
        message.reply_text.assert_awaited_once()

    async def test_admin_support_reply_is_delivered_anonymously(self):
        message = SimpleNamespace(
            chat_id=999,
            message_id=654,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=999),
        )
        fake_bot = SimpleNamespace(
            send_message=AsyncMock(),
            copy_message=AsyncMock(),
        )
        context = SimpleNamespace(user_data={"support_reply_to": 123}, bot=fake_bot)
        with (
            patch.object(config, "ADMIN_IDS", {999}),
            patch.dict(config.CUSTOM_EMOJI_IDS, {}, clear=True),
        ):
            handled = await bot.relay_support_message(update, context)

        self.assertTrue(handled)
        fake_bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="💬 <b>Ответ поддержки</b>",
            parse_mode=ParseMode.HTML,
        )
        fake_bot.copy_message.assert_awaited_once_with(
            chat_id=123,
            from_chat_id=999,
            message_id=654,
        )
        message.reply_text.assert_awaited_once()


class PaidSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def plan(**overrides):
        values = {
            "name": "White Premium",
            "category": "white",
            "stars_price": 125,
            "description": "Stable VLESS list",
            "created_at": "2026-09-07T21:00:00+03:00",
            "delivery_url": "https://example.com/paid/subscription",
        }
        values.update(overrides)
        return paid_subscriptions.build_paid_plan(**values)

    async def test_paid_registry_catalog_and_admin_controls(self):
        plan = self.plan()
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "paid_subscriptions.json"
            paid_subscriptions.save_paid_registry([plan], registry)
            with (
                patch.object(bot, "PAID_REGISTRY_FILE", registry),
                patch.dict(config.CUSTOM_EMOJI_IDS, {}, clear=True),
            ):
                catalog = bot.paid_catalog_text()
                buttons = [
                    button
                    for row in bot.paid_catalog_keyboard().inline_keyboard
                    for button in row
                ]
                detail = bot.paid_plan_text(plan)
                admin_buttons = [
                    button
                    for row in bot.admin_paid_plan_keyboard(plan).inline_keyboard
                    for button in row
                ]
        self.assertIn("Белые списки: <b>1</b>", catalog)
        self.assertTrue(any(button.callback_data == "paidcat:white" for button in buttons))
        self.assertIn("125 XTR", detail)
        self.assertNotIn(plan["delivery_url"], detail)
        self.assertTrue(any(button.callback_data.startswith("paidadminfile:") for button in admin_buttons))
        self.assertTrue(any(button.callback_data.startswith("paidadminlink:") for button in admin_buttons))
        self.assertTrue(all(button.style is None for button in [*buttons, *admin_buttons]))

    async def test_paid_plan_callback_creates_real_stars_invoice(self):
        plan = self.plan()
        query = SimpleNamespace(
            data=f"paystars:{plan['id']}",
            from_user=SimpleNamespace(id=123, username="buyer", first_name="Buyer"),
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        fake_bot = SimpleNamespace(send_invoice=AsyncMock())
        context = SimpleNamespace(bot=fake_bot, user_data={})
        update = SimpleNamespace(callback_query=query)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bot, "is_user_subscribed", AsyncMock(return_value=True)),
            patch.object(bot, "get_or_create_user"),
        ):
            registry = Path(directory) / "paid_subscriptions.json"
            paid_subscriptions.save_paid_registry([plan], registry)
            with patch.object(bot, "PAID_REGISTRY_FILE", registry):
                await bot.callback_handler(update, context)
        kwargs = fake_bot.send_invoice.await_args.kwargs
        self.assertEqual(kwargs["currency"], "XTR")
        self.assertEqual(kwargs["provider_token"], "")
        self.assertEqual(kwargs["payload"], f"paid:{plan['id']}:123")
        self.assertEqual(kwargs["prices"][0].amount, 125)

    async def test_stars_precheckout_validates_user_plan_and_exact_price(self):
        plan = self.plan()
        precheckout = SimpleNamespace(
            invoice_payload=f"paid:{plan['id']}:123",
            from_user=SimpleNamespace(id=123),
            currency="XTR",
            total_amount=125,
            answer=AsyncMock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "paid_subscriptions.json"
            paid_subscriptions.save_paid_registry([plan], registry)
            with patch.object(bot, "PAID_REGISTRY_FILE", registry):
                await bot.paid_precheckout_handler(
                    SimpleNamespace(pre_checkout_query=precheckout),
                    SimpleNamespace(),
                )
                precheckout.answer.assert_awaited_once_with(ok=True)
                precheckout.total_amount = 124
                precheckout.answer.reset_mock()
                await bot.paid_precheckout_handler(
                    SimpleNamespace(pre_checkout_query=precheckout),
                    SimpleNamespace(),
                )
        self.assertFalse(precheckout.answer.await_args.kwargs["ok"])

    async def test_successful_stars_payment_delivers_once_and_records_order(self):
        plan = self.plan()
        payment = SimpleNamespace(
            invoice_payload=f"paid:{plan['id']}:123",
            telegram_payment_charge_id="charge-123",
            currency="XTR",
            total_amount=125,
        )
        message = SimpleNamespace(successful_payment=payment, reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        context = SimpleNamespace(bot=SimpleNamespace())
        saved = {}
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "paid_subscriptions.json"
            paid_subscriptions.save_paid_registry([plan], registry)
            with (
                patch.object(bot, "PAID_REGISTRY_FILE", registry),
                patch.object(bot, "load_paid_orders", return_value={}),
                patch.object(bot, "save_paid_orders", side_effect=lambda value: saved.update(value)),
                patch.object(bot, "deliver_paid_plan", AsyncMock(return_value=True)) as deliver,
            ):
                await bot.paid_successful_payment_handler(update, context)
        deliver.assert_awaited_once_with(context.bot, 123, plan)
        self.assertTrue(saved["stars:charge-123"]["delivered"])
        self.assertEqual(saved["stars:charge-123"]["plan_id"], plan["id"])

    async def test_paid_txt_upload_keeps_only_valid_vless(self):
        good = vless(fragment="Paid-Node")
        telegram_file = SimpleNamespace(
            download_as_bytearray=AsyncMock(
                return_value=bytearray(f"{good}\nvmess://ignored\nvless://broken".encode())
            )
        )
        document = SimpleNamespace(
            file_name="premium.txt",
            file_size=100,
            get_file=AsyncMock(return_value=telegram_file),
        )
        content = await bot.read_paid_document(document)
        self.assertIn(good, content)
        self.assertNotIn("vmess://", content)
        self.assertNotIn("vless://broken", content)
        self.assertIn("# Количество: 1", content)

    def test_paid_registry_rejects_credential_urls(self):
        with self.assertRaises(ValueError):
            self.plan(delivery_url="https://user:secret@example.com/sub")


class DynamicProviderRegistryTests(unittest.TestCase):
    def test_github_file_links_are_normalized_and_external_urls_rejected(self):
        expected = (
            "https://raw.githubusercontent.com/owner/vpn-repo/main/"
            "vless_configs.txt"
        )
        self.assertEqual(
            provider_registry.normalize_github_raw_url(
                "https://github.com/owner/vpn-repo/blob/main/vless_configs.txt"
            ),
            expected,
        )
        self.assertEqual(
            provider_registry.normalize_github_raw_url(expected),
            expected,
        )
        for unsafe in (
            "http://github.com/owner/repo/blob/main/vless.txt",
            "https://example.com/owner/repo/vless.txt",
            "https://github.com/owner/repo/blob/main/vless.txt?token=secret",
            "https://user:pass@github.com/owner/repo/blob/main/vless.txt",
        ):
            with self.subTest(url=unsafe):
                with self.assertRaises(ValueError):
                    provider_registry.normalize_github_raw_url(unsafe)

    def test_exact_public_repository_links_are_supported(self):
        self.assertEqual(
            provider_registry.parse_github_repository_url(
                "https://github.com/owner/vpn-repo"
            ),
            ("owner", "vpn-repo"),
        )
        self.assertIsNone(
            provider_registry.parse_github_repository_url(
                "https://github.com/owner/vpn-repo/issues"
            )
        )

    def test_registry_round_trip_and_runtime_category_application(self):
        url = (
            "https://raw.githubusercontent.com/owner/vpn-repo/main/"
            "vless_configs.txt"
        )
        record = provider_registry.build_provider_record(
            url,
            "black",
            added_at="2026-09-07T20:00:00+03:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            provider_registry.save_provider_registry([record], path)
            self.assertEqual(
                provider_registry.load_provider_registry(path),
                [record],
            )

        original = list(config.DYNAMIC_PROVIDER_RECORDS)
        try:
            config.apply_dynamic_providers([record])
            self.assertIn(record["id"], config.SOURCES)
            self.assertIn(record["id"], config.BLACK_SOURCE_KEYS)
            self.assertNotIn(record["id"], config.WHITE_SOURCE_KEYS)
            self.assertIn(
                record["id"],
                config.AGGREGATED_SUBS["BLACK_FULL"]["source_keys"],
            )
            self.assertIn(
                record["id"],
                config.AGGREGATED_SUBS["FULL"]["source_keys"],
            )
            disabled = dict(record, enabled=False)
            config.apply_dynamic_providers([disabled])
            self.assertNotIn(record["id"], config.SOURCES)
            self.assertIn(disabled, config.DYNAMIC_PROVIDER_RECORDS)
        finally:
            config.apply_dynamic_providers(original)

    def test_registry_has_a_bounded_provider_count(self):
        records = [
            provider_registry.build_provider_record(
                f"https://raw.githubusercontent.com/owner/vpn-repo/main/vless_{index}.txt",
                "black",
                added_at="2026-09-07T20:00:00+03:00",
            )
            for index in range(provider_registry.MAX_DYNAMIC_PROVIDERS + 5)
        ]
        self.assertEqual(
            len(provider_registry.normalize_registry(records)),
            provider_registry.MAX_DYNAMIC_PROVIDERS,
        )

    def test_provider_candidate_buttons_require_explicit_category_approval(self):
        keyboard = bot.provider_candidate_keyboard("abcd1234")
        buttons = {
            button.callback_data: button
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertIsNone(buttons["provider_accept:abcd1234:white"].style)
        self.assertIsNone(buttons["provider_accept:abcd1234:black"].style)
        self.assertIsNone(buttons["provider_skip:abcd1234"].style)

    def test_repository_candidate_ranking_remains_vless_vpn_bounded(self):
        self.assertGreater(
            parser._repository_file_score(
                "owner/vpn-repo", "feeds/vless_configs.txt"
            ),
            0,
        )
        self.assertLess(
            parser._repository_file_score("owner/unrelated", "README.md"),
            0,
        )
        self.assertLess(
            parser._repository_file_score("owner/unrelated", "image.png"),
            0,
        )


class DynamicProviderAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_inspection_accepts_only_valid_vless(self):
        url = (
            "https://raw.githubusercontent.com/owner/vpn-repo/main/"
            "vless_configs.txt"
        )
        valid = vless()
        with patch.object(
            parser,
            "fetch_text",
            AsyncMock(return_value=f"{valid}\ntrojan://ignored@example.com:443"),
        ):
            result = await parser._inspect_github_feed_urls(
                None,
                [url],
                min_valid=1,
                max_results=2,
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], url)
        self.assertEqual(result[0]["valid_count"], 1)

    async def test_registry_works_locally_without_github_token_and_warns(self):
        record = provider_registry.build_provider_record(
            "https://raw.githubusercontent.com/owner/vpn-repo/main/vless.txt",
            "white",
            added_at="2026-09-07T20:00:00+03:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "providers.json"
            with (
                patch.object(bot, "REGISTRY_FILE", registry_path),
                patch.object(config, "GITHUB_TOKEN", ""),
                patch.object(config, "apply_dynamic_providers") as apply,
            ):
                ok, detail = await bot.persist_dynamic_providers(
                    [record],
                    "test provider",
                )
            self.assertTrue(ok)
            self.assertTrue(detail.startswith("local-only:"))
            self.assertEqual(
                provider_registry.load_provider_registry(registry_path),
                [record],
            )
            apply.assert_called_once_with([record])


class SourceRegistryTests(unittest.TestCase):
    def test_only_selected_github_sources_are_registered(self):
        self.assertNotIn("collection", config.SOURCES)
        self.assertNotIn("internet_discovery", config.SOURCES)
        self.assertEqual(
            set(config.WHITE_SOURCE_KEYS),
            {"zieng2", "igareck", "cid_vpn", "byewhitelists2", "ghost_vpn"},
        )
        for key, source in config.SOURCES.items():
            if source.get("discovery"):
                continue
            with self.subTest(source=key):
                self.assertTrue(source["urls"])
                self.assertTrue(
                    all(
                        url.startswith("https://raw.githubusercontent.com/")
                        for url in source["urls"]
                    )
                )
        self.assertEqual(
            set(config.FULL_SOURCE_KEYS),
            set(config.WHITE_SOURCE_KEYS) | set(config.BLACK_SOURCE_KEYS),
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

    def test_github_search_is_manual_approval_only_and_bounded(self):
        self.assertNotIn("github_discovery", config.SOURCES)
        self.assertNotIn("github_discovery", config.WHITE_SOURCE_KEYS)
        self.assertNotIn("github_discovery", config.BLACK_SOURCE_KEYS)
        self.assertNotIn("github_discovery", config.FULL_SOURCE_KEYS)
        self.assertGreaterEqual(len(config.PUBLIC_GITHUB_SEARCH["search_queries"]), 8)
        self.assertLessEqual(config.DISCOVERY_MAX_FEEDS, 16)
        self.assertLessEqual(config.DISCOVERY_MAX_CONFIGS, 3000)

    def test_admin_label_is_exact(self):
        bot_source = (ROOT / "src" / "bot.py").read_text(encoding="utf-8")
        self.assertIn('"«Проверка и очистка»"', bot_source)
        self.assertNotIn("Проверку и очистку", bot_source)

    def test_inline_buttons_are_neutral_and_keep_unicode_icons(self):
        main_buttons = {
            button.callback_data: button
            for row in bot.main_keyboard(config.ADMIN_ID).inline_keyboard
            for button in row
        }
        expected = {
            "profile": "👤",
            "white": "⬜",
            "black": "⬛",
            "full": "📚",
            "paid": "💎",
            "help": "❔",
            "support": "💬",
            "admin_panel": "⚙️",
        }
        for callback_data, icon in expected.items():
            with self.subTest(callback_data=callback_data):
                self.assertTrue(main_buttons[callback_data].text.startswith(icon))
                self.assertIsNone(main_buttons[callback_data].style)
                self.assertIsNone(main_buttons[callback_data].api_kwargs.get("icon_custom_emoji_id"))

        back = bot.back_keyboard("admin_panel").inline_keyboard[0][0]
        self.assertTrue(back.text.startswith("◀️"))
        self.assertIsNone(back.style)
        self.assertIsNone(back.api_kwargs.get("icon_custom_emoji_id"))

    def test_configured_custom_emoji_id_is_sent_to_button_and_html(self):
        with patch.dict(bot.config.CUSTOM_EMOJI_IDS, {"profile": "custom-profile-id"}, clear=True):
            profile = next(
                button
                for row in bot.main_keyboard(config.ADMIN_ID).inline_keyboard
                for button in row
                if button.callback_data == "profile"
            )
            self.assertEqual(profile.text, "«Профиль»")
            self.assertEqual(profile.icon_custom_emoji_id, "custom-profile-id")
            self.assertEqual(
                bot.render_html("👤 <b>Профиль</b>", bot.config.CUSTOM_EMOJI_IDS),
                '<tg-emoji emoji-id="custom-profile-id">👤</tg-emoji> <b>Профиль</b>',
            )

    def test_custom_emoji_ids_are_read_from_entities_captions_and_stickers(self):
        custom_type = bot.MessageEntityType.CUSTOM_EMOJI
        message = SimpleNamespace(
            sticker=SimpleNamespace(custom_emoji_id="sticker-id"),
            entities=[SimpleNamespace(type=custom_type, custom_emoji_id="text-id")],
            caption_entities=[
                SimpleNamespace(type=custom_type, custom_emoji_id="caption-id"),
                SimpleNamespace(type=custom_type, custom_emoji_id="sticker-id"),
            ],
        )
        self.assertEqual(
            bot.extract_custom_emoji_ids(message),
            ["sticker-id", "text-id", "caption-id"],
        )

    def test_config_list_is_paginated_and_keeps_txt_delivery(self):
        links = [
            vless(host=f"node{index}.example.com", fragment=f"Node-{index}")
            for index in range(10)
        ]
        with (
            patch.object(
                bot,
                "AGGREGATED_CACHE",
                {
                    "FULL.txt": {
                        "count": len(links),
                        "content": "",
                        "configs": links,
                    }
                },
            ),
            patch.dict(config.CUSTOM_EMOJI_IDS, {}, clear=True),
        ):
            keyboard = bot.config_list_keyboard("FULL", 0)
            text = bot.config_list_text("FULL", 0)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        config_buttons = [
            button for button in buttons if (button.callback_data or "").startswith("cfgdetail:")
        ]
        self.assertEqual(len(config_buttons), bot.CONFIGS_PER_PAGE)
        self.assertTrue(any((button.callback_data or "").startswith("cfglist:FULL:1") for button in buttons))
        self.assertTrue(any(button.callback_data == "packages:FULL" for button in buttons))
        self.assertIn("Страница: <b>1/2</b>", text)
        self.assertNotIn("vless://", " ".join(button.text for button in buttons))
        self.assertTrue(all(button.style is None for button in buttons))

    def test_config_detail_has_in_message_tcp_ping_control(self):
        link = vless(fragment="Fast-Node")
        with (
            patch.object(
                bot,
                "AGGREGATED_CACHE",
                {"FULL.txt": {"count": 1, "content": "", "configs": [link]}},
            ),
            patch.dict(config.CUSTOM_EMOJI_IDS, {}, clear=True),
        ):
            text = bot.config_detail_text("FULL", link, 0, ping_status=42)
            keyboard = bot.config_detail_keyboard("FULL", link, 0)
        self.assertIn("TCP-соединение установлено", text)
        self.assertIn("42 мс", text)
        self.assertNotIn("vless://", text)
        ping_button = keyboard.inline_keyboard[0][0]
        self.assertTrue(ping_button.callback_data.startswith("cfgping:FULL:"))
        self.assertIsNone(ping_button.style)

    def test_admin_panel_displays_current_unique_vless_count(self):
        with patch.object(
            bot,
            "AGGREGATED_CACHE",
            {"FULL.txt": {"count": 321, "content": "", "configs": []}},
        ):
            self.assertIn(
                "Текущее количество VLESS-конфигов: <b>321</b>",
                bot.admin_panel_text(),
            )

    def test_interface_offers_files_without_subscription_links(self):
        bot_source = (ROOT / "src" / "bot.py").read_text(encoding="utf-8")
        self.assertNotIn("get_raw_url", bot_source)
        self.assertNotIn("«Копировать", bot_source)
        self.assertNotIn("generate_qr_bytes", bot_source)
        self.assertIn("«Скачать .txt»", bot_source)
        self.assertIn("config.FILE_USAGE_TEXT", bot_source)
        self.assertIn(config.FILE_USAGE_TEXT, config.HELP_TEXT)
        self.assertIn("всё содержимое файла целиком", config.FILE_USAGE_TEXT)
        self.assertIn("весь скопированный текст сразу", config.FILE_USAGE_TEXT)
        self.assertNotIn("скопируй нужную строку", config.FILE_USAGE_TEXT)

    def test_obsolete_chunk_callback_resolves_to_current_aggregate(self):
        key, aggregate = bot.aggregate_for_filename("BLACK_FULL_6.txt")
        self.assertEqual(key, "BLACK_FULL")
        self.assertEqual(aggregate["filename"], "BLACK_FULL.txt")
        self.assertEqual(bot.aggregate_back_callback("BLACK_FULL_6.txt"), "black")
        with patch.object(bot, "AGGREGATED_CACHE", {"BLACK_FULL.txt": {}}):
            self.assertIsNone(bot.local_subscription_path("BLACK_FULL_6.txt"))

    def test_banner_uses_key_and_owner_username_is_not_exposed(self):
        self.assertIn("🔑 Free VPN • Crimson", config.WELCOME_TEXT)
        self.assertNotIn("🛰", config.WELCOME_TEXT)
        self.assertEqual(bot.icon_text("network", "Main"), "🔑 Main")
        bot_text = (ROOT / "src" / "bot.py").read_text(encoding="utf-8")
        config_text = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("unnervin", bot_text.lower())
        self.assertNotIn("unnervin", config_text.lower())
        self.assertIn("«Поддержка»", config.HELP_TEXT)

    def test_admin_notification_toggle_button_reflects_setting(self):
        with patch.object(
            bot,
            "SETTINGS",
            {"update_notifications": True, "last_update_notification_id": None},
        ):
            enabled = {
                button.callback_data: button
                for row in bot.admin_keyboard().inline_keyboard
                for button in row
            }
        self.assertIsNone(enabled["admin_notifications"].style)
        self.assertIn("Уведомления: ВКЛ", enabled["admin_notifications"].text)
        self.assertIsNone(enabled["admin_providers"].style)
        self.assertIsNone(enabled["admin_discovery"].style)
        self.assertIsNone(enabled["admin_paid"].style)

        with patch.object(
            bot,
            "SETTINGS",
            {"update_notifications": False, "last_update_notification_id": None},
        ):
            disabled = {
                button.callback_data: button
                for row in bot.admin_keyboard().inline_keyboard
                for button in row
            }
        self.assertIsNone(disabled["admin_notifications"].style)
        self.assertIn("Уведомления: ВЫКЛ", disabled["admin_notifications"].text)

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

"""Atomic publication of generated VLESS subscriptions to GitHub."""

import asyncio
import base64
import hashlib
import logging
import re
from pathlib import PurePosixPath
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)
API_ROOT = "https://api.github.com"
MANAGED_FILE_RE = re.compile(r"^(.+?)(?:_(\d+))?\.txt$")


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "VLESS-Parser-Bot",
    }


async def _response_json(response, operation: str, expected=(200,)):
    if response.status not in expected:
        body = await response.text()
        raise RuntimeError(f"GitHub {operation} -> {response.status}: {body[:300]}")
    return await response.json()


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _managed_groups(paths: set) -> set:
    """Return (directory, aggregate base name) pairs from the outgoing files."""
    groups = set()
    for path in paths:
        item = PurePosixPath(path)
        match = MANAGED_FILE_RE.fullmatch(item.name)
        if not match:
            continue
        groups.add((str(item.parent) if str(item.parent) != "." else "", match.group(1)))
    return groups


def _is_managed_path(path: str, groups: set) -> bool:
    item = PurePosixPath(path)
    directory = str(item.parent) if str(item.parent) != "." else ""
    match = MANAGED_FILE_RE.fullmatch(item.name)
    return bool(match and (directory, match.group(1)) in groups)


async def push_aggregated_subscriptions(
    aggregated_files: dict,
    repo: str,
    token: str,
    branch: str = "main",
    retries: int = 2,
):
    """Publish every aggregate/chunk and delete stale chunks in one commit.

    A single ref update prevents Railway from redeploying halfway through a
    multi-file refresh, which previously allowed keyboards to reference files
    (for example ``BLACK_FULL_6.txt``) that had not been uploaded yet.
    """
    if not token:
        logger.warning("GITHUB_TOKEN не задан, пропуск публикации")
        return {}
    if not repo or "/" not in repo or not aggregated_files:
        logger.warning("Неверные параметры публикации GitHub")
        return {}

    outgoing = {
        str(PurePosixPath(path)): content.encode("utf-8")
        for path, content in aggregated_files.items()
    }
    outgoing_paths = set(outgoing)
    managed_groups = _managed_groups(outgoing_paths)
    encoded_branch = quote(branch, safe="")
    headers = _headers(token)
    timeout = aiohttp.ClientTimeout(total=120, connect=15, sock_read=60)

    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                ref_url = f"{API_ROOT}/repos/{repo}/git/ref/heads/{encoded_branch}"
                async with session.get(ref_url) as response:
                    ref = await _response_json(response, "get ref")
                parent_sha = ref["object"]["sha"]

                commit_url = f"{API_ROOT}/repos/{repo}/git/commits/{parent_sha}"
                async with session.get(commit_url) as response:
                    parent_commit = await _response_json(response, "get commit")
                base_tree_sha = parent_commit["tree"]["sha"]

                tree_url = f"{API_ROOT}/repos/{repo}/git/trees/{base_tree_sha}"
                async with session.get(tree_url, params={"recursive": "1"}) as response:
                    current_tree = await _response_json(response, "get tree")
                current_blobs = {
                    item["path"]: item["sha"]
                    for item in current_tree.get("tree", [])
                    if item.get("type") == "blob"
                }

                tree_entries = []
                for path, content in outgoing.items():
                    expected_sha = _git_blob_sha(content)
                    if current_blobs.get(path) == expected_sha:
                        continue
                    blob_payload = {
                        "content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64",
                    }
                    async with session.post(
                        f"{API_ROOT}/repos/{repo}/git/blobs",
                        json=blob_payload,
                    ) as response:
                        blob = await _response_json(response, f"create blob {path}", expected=(201,))
                    tree_entries.append(
                        {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
                    )

                stale_paths = sorted(
                    path
                    for path in current_blobs
                    if _is_managed_path(path, managed_groups) and path not in outgoing_paths
                )
                tree_entries.extend(
                    {"path": path, "mode": "100644", "type": "blob", "sha": None}
                    for path in stale_paths
                )

                if tree_entries:
                    async with session.post(
                        f"{API_ROOT}/repos/{repo}/git/trees",
                        json={"base_tree": base_tree_sha, "tree": tree_entries},
                    ) as response:
                        new_tree = await _response_json(response, "create tree", expected=(201,))

                    message = (
                        f"update VLESS subscriptions — {len(outgoing_paths)} files"
                        + (f", remove {len(stale_paths)} stale chunks" if stale_paths else "")
                    )
                    async with session.post(
                        f"{API_ROOT}/repos/{repo}/git/commits",
                        json={
                            "message": message,
                            "tree": new_tree["sha"],
                            "parents": [parent_sha],
                        },
                    ) as response:
                        new_commit = await _response_json(response, "create commit", expected=(201,))

                    async with session.patch(
                        f"{API_ROOT}/repos/{repo}/git/refs/heads/{encoded_branch}",
                        json={"sha": new_commit["sha"], "force": False},
                    ) as response:
                        if response.status == 422 and attempt < retries:
                            logger.warning("GitHub branch changed during sync, retrying")
                            await asyncio.sleep(1)
                            continue
                        await _response_json(response, "update ref")
                    logger.info(
                        "GitHub atomic sync: %s files, %s stale chunks removed",
                        len(outgoing_paths),
                        len(stale_paths),
                    )

                return {
                    path: (
                        f"https://raw.githubusercontent.com/{repo}/{branch}/"
                        f"{quote(path, safe='/')}"
                    )
                    for path in outgoing_paths
                }
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, KeyError) as exc:
            if attempt < retries:
                logger.warning("GitHub atomic sync failed, retrying: %s", exc)
                await asyncio.sleep(1)
                continue
            logger.error("GitHub atomic sync failed: %s", exc)
            return {}

    return {}

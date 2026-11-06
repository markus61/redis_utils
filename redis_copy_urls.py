#!/usr/bin/env python3
"""
copy redis data from redis url "source" to redis url "target"
both urls are command line parameters.
the script is called like this:
    redis_copy_urls.py <source_url> <target_url>

the script then walks through all keys in the source Redis and copies them to the target Redis.
any errors encountered during the copy process will be logged.

requirements:
    - Python >= 3.10, redis-py (tested with 8.1)
    - Redis server >= 3.0 on both sides
    - target Redis version >= source Redis version (DUMP payloads are not
      readable by older servers)
"""

import sys

MINIMUM_PYTHON_VERSION = (3, 10)
if sys.version_info < MINIMUM_PYTHON_VERSION:
    sys.exit(
        "redis_copy_urls.py requires Python %d.%d or newer (running %s)."
        % (*MINIMUM_PYTHON_VERSION, sys.version.split()[0])
    )

import argparse  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402

import redis.asyncio as redis  # noqa: E402
from redis.exceptions import ConnectionError, RedisError, ResponseError, TimeoutError  # noqa: E402

logger = logging.getLogger(__name__)

KEY_MISSING_TTL = -2
NO_EXPIRY_TTL = -1
SCAN_BATCH_SIZE = 1000
INCOMPATIBLE_PAYLOAD_MESSAGE = "DUMP payload version"

EXIT_SUCCESS = 0
EXIT_SOME_KEYS_FAILED = 1
EXIT_CONNECTION_FAILED = 2
EXIT_INCOMPATIBLE_VERSIONS = 3

REQUIREMENTS_HELP = """\
requirements:
  Python >= 3.10
  Redis server >= 3.0 on source and target
  target Redis version must be >= source Redis version
    Keys are copied with DUMP/RESTORE. The DUMP format is tied to the
    source server's RDB version; an older target cannot read a payload
    from a newer source and rejects it with
    "DUMP payload version or checksum are wrong".
    Example: 5.0 -> 8.0 works, 8.0 -> 5.0 does not.

exit codes:
  0  all keys copied
  1  some keys failed (see log)
  2  could not connect to source or target
  3  target Redis is too old for the source's DUMP format
"""

VERSION_MISMATCH_HINT = (
    "The target Redis (%s) cannot read DUMP payloads from the source Redis (%s). "
    "DUMP/RESTORE only works from an older or equal server version to a newer "
    "or equal one. Upgrade the target Redis, or copy into a target running "
    "Redis >= %s."
)


class IncompatibleVersionError(Exception):
    """Raised when the target Redis rejects the source's DUMP format."""


def parse_arguments(argument_list: list[str] | None = None) -> argparse.Namespace:
    """Parse the source and target Redis URLs from the command line."""
    parser = argparse.ArgumentParser(
        description="Copy all keys from a source Redis to a target Redis.",
        epilog=REQUIREMENTS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source_url", help="Redis URL to copy from, e.g. redis://host:6379/0")
    parser.add_argument("target_url", help="Redis URL to copy to, e.g. redis://host:6379/1")
    return parser.parse_args(argument_list)


async def get_server_version(client: redis.Redis) -> str:
    """Return the Redis server version string, e.g. '7.4.1'."""
    server_info = await client.info("server")
    version = server_info["redis_version"]
    return version.decode() if isinstance(version, bytes) else str(version)


def version_tuple(version: str) -> tuple[int, ...]:
    """Turn '7.4.1' into (7, 4, 1) for comparison."""
    return tuple(int(part) for part in version.split(".") if part.isdigit())


async def check_server_versions(source_client: redis.Redis, target_client: redis.Redis) -> tuple[str, str]:
    """Log both server versions and warn if the target is older than the source."""
    source_version = await get_server_version(source_client)
    target_version = await get_server_version(target_client)
    logger.info("Source Redis %s -> target Redis %s", source_version, target_version)
    if version_tuple(target_version) < version_tuple(source_version):
        logger.warning(
            "Target Redis (%s) is older than source Redis (%s); the copy will "
            "likely fail. Run with --help for version requirements.",
            target_version,
            source_version,
        )
    return source_version, target_version


async def copy_key(
    source_client: redis.Redis, target_client: redis.Redis, key_name: bytes
) -> bool:
    """Copy a single key (any type, including its TTL) via DUMP/RESTORE.

    Returns True if the key was copied, False if it vanished before it could be read.
    """
    serialized_value = await source_client.dump(key_name)
    remaining_ttl_ms = await source_client.pttl(key_name)
    if serialized_value is None or remaining_ttl_ms == KEY_MISSING_TTL:
        return False
    restore_ttl_ms = 0 if remaining_ttl_ms == NO_EXPIRY_TTL else remaining_ttl_ms
    await target_client.restore(key_name, restore_ttl_ms, serialized_value, replace=True)
    return True


async def copy_all_keys(source_url: str, target_url: str) -> int:
    """Walk all keys of the source Redis and copy them to the target Redis.

    Returns the number of keys that failed to copy.
    Raises IncompatibleVersionError if the target cannot read the source's DUMP format.
    """
    copied_count = 0
    skipped_count = 0
    failed_count = 0

    # decode_responses must stay False: DUMP payloads are binary.
    # protocol=2 (RESP2): redis-py >= 8 defaults to RESP3, which sends HELLO 3
    # and fails on Redis servers older than 6.0.
    async with (
        redis.Redis.from_url(source_url, decode_responses=False, protocol=2) as source_client,
        redis.Redis.from_url(target_url, decode_responses=False, protocol=2) as target_client,
    ):
        source_version, target_version = await check_server_versions(source_client, target_client)

        async for key_name in source_client.scan_iter(count=SCAN_BATCH_SIZE):
            try:
                if await copy_key(source_client, target_client, key_name):
                    copied_count += 1
                else:
                    skipped_count += 1
                    logger.debug("Key %r disappeared before copy; skipped", key_name)
            except (ConnectionError, TimeoutError):
                raise
            except ResponseError as error:
                # Every key shares the same DUMP format, so all would fail: stop now.
                if INCOMPATIBLE_PAYLOAD_MESSAGE in str(error):
                    raise IncompatibleVersionError(
                        VERSION_MISMATCH_HINT % (target_version, source_version, source_version)
                    ) from error
                failed_count += 1
                logger.exception("Failed to copy key %r", key_name)
            except RedisError:
                failed_count += 1
                logger.exception("Failed to copy key %r", key_name)

    logger.info(
        "Done: %d copied, %d skipped (expired during copy), %d failed",
        copied_count,
        skipped_count,
        failed_count,
    )
    return failed_count


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    arguments = parse_arguments()
    try:
        failed_count = asyncio.run(copy_all_keys(arguments.source_url, arguments.target_url))
    except IncompatibleVersionError as error:
        logger.error("%s Nothing was copied. Run with --help for details.", error)
        return EXIT_INCOMPATIBLE_VERSIONS
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("Connection to Redis failed; aborting")
        return EXIT_CONNECTION_FAILED
    return EXIT_SOME_KEYS_FAILED if failed_count else EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())

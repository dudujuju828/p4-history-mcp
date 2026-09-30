"""Shared resource limits, enforced before expensive reads or writes."""

MAX_FILE = 64 * 1024 * 1024
MAX_SNAPSHOT = 256 * 1024 * 1024
MAX_FILES = 2000
MAX_METADATA = 8 * 1024 * 1024
MAX_DIFF_FILE = 64 * 1024
MAX_DIFF_LINES = 4096
MAX_DIFF_OUTPUT = 100_000

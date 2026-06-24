"""Shared pytest fixtures: ephemeral bare git repo + config.

Every test module that needs a real repository receives the ``repo``
fixture, which spins up an in-process bare git repo (no disk state
survives between tests) and commits the FILES tree to
``refs/heads/master``.

The ``cfg`` fixture wraps it in a minimal AgentCacheConfig.

FILES is also importable as a module-level constant so test modules
that need to reference file content directly can do:

    from tests.conftest import FILES
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict

import pygit2
import pytest

from agentcache.config import AgentCacheConfig

# ---------------------------------------------------------------------------
# Fixture file tree.
#
# Chosen to satisfy all test_symbols.py assertions:
#   - "TokenRefresher" class in src/app.py
#   - "make_refresher" function in src/app.py
#   - "str_len" symbol (defined in src/util.c as a C function)
#
# test_bundle_and_coldstart.py also asserts:
#   assert "TokenRefresher" in content   (content of src/app.py blob)
# ---------------------------------------------------------------------------
FILES: Dict[str, str] = {
    "src/app.py": """\
\"\"\"Token refresh helpers.\"\"\"
from __future__ import annotations


class TokenRefresher:
    \"\"\"Handles token refresh for API clients.\"\"\"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str = ""

    def refresh(self) -> str:
        \"\"\"Refresh and return the bearer token.\"\"\"
        self._token = "refreshed"
        return self._token


def make_refresher(client_id: str, client_secret: str) -> \"TokenRefresher\":
    \"\"\"Factory: build and return a TokenRefresher.\"\"\"
    return TokenRefresher(client_id, client_secret)
""",
    "src/util.c": """\
#include <string.h>

/* Return the byte length of a NUL-terminated string. */
size_t str_len(const char *s) {
    return strlen(s);
}
""",
    "README.md": "# agentcache fixture repo\n",
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BLOB_MODE = 0o100644  # GIT_FILEMODE_BLOB
_TREE_MODE = 0o040000  # GIT_FILEMODE_TREE


def _build_tree_oid(repo: pygit2.Repository, files: Dict[str, str]) -> pygit2.Oid:
    """Recursively build a git tree from a ``{relative-path: content}`` dict.

    Paths use forward-slash separators.  Content values are strings;
    they are encoded to UTF-8 before storage.
    """
    root_files: Dict[str, str] = {}
    subdirs: Dict[str, Dict[str, str]] = defaultdict(dict)

    for path, content in files.items():
        head, _, tail = path.partition("/")
        if tail:
            subdirs[head][tail] = content
        else:
            root_files[head] = content

    builder = repo.TreeBuilder()

    for name in sorted(root_files):
        data = root_files[name].encode()
        blob_oid = repo.create_blob(data)
        builder.insert(name, blob_oid, _BLOB_MODE)

    for dir_name in sorted(subdirs):
        sub_oid = _build_tree_oid(repo, subdirs[dir_name])
        builder.insert(dir_name, sub_oid, _TREE_MODE)

    return builder.write()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    """Yield ``(pygit2.Repository, commit_hex)`` for an ephemeral bare repo.

    The repo has one commit on ``refs/heads/master`` containing FILES.
    """
    bare_path = str(tmp_path / "test.git")
    r = pygit2.init_repository(bare_path, bare=True)

    tree_oid = _build_tree_oid(r, FILES)
    sig = pygit2.Signature("Test Author", "test@example.com")
    commit_oid = r.create_commit(
        "refs/heads/master",
        sig,
        sig,
        "Initial commit\n",
        tree_oid,
        [],  # no parents — first commit
    )
    yield r, str(commit_oid)


@pytest.fixture
def cfg(repo):
    """Return an :class:`AgentCacheConfig` pointing at the test repo."""
    r, _ = repo
    return AgentCacheConfig(repo_dir=r.path)

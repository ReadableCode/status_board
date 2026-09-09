#!/usr/bin/env bash
# Step functions for status_board commands (darwin + linux). Sourced by cmdr,
# never executed directly; <step>_check is the read-only probe.

status_board() {
    (cd "$CMDR_REPO_DIR" && uv run python src/status_board.py)
}

status_board_check() {
    # One fetch of every panel, printed: a panel that fails to fetch is drift.
    (cd "$CMDR_REPO_DIR" && uv run python src/status_board.py --once)
}

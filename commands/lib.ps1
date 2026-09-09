# Step functions for status_board commands (windows). Dot-sourced by cmdr.

function status_board {
    Set-Location $env:CMDR_REPO_DIR
    uv run python src/status_board.py
    exit $LASTEXITCODE
}

function status_board_check {
    Set-Location $env:CMDR_REPO_DIR
    uv run python src/status_board.py --once
    exit $LASTEXITCODE
}

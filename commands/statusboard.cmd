# The status board TUI as a cmdr command: terminal step, so cmdr's TUI
# hands the screen over. The check is one static render of every panel.
description: status board - the homelab panels, live
order: 250
platforms: darwin linux windows
steps:
  status_board requires=uv terminal

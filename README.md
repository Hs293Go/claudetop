# claudetop

A terminal UI for inspecting and cleaning up Claude Code sessions on disk
(`~/.claude`, or `$CLAUDE_CONFIG_DIR`).

- **Sessions**: every transcript, with its size, token use, and how full its
  context window got
- **Projects**: sessions rolled up per working directory, flagging directories
  that no longer exist
- **Traffic**: tokens per day, split by cache behaviour and by model
- **Prune**: reclaimable data, such as superseded transcripts and per-session
  data left behind

Deleting, rehoming and pruning ask for a typed confirmation and move files into
`~/.claude/.claudetop-trash/` instead of unlinking them. Sessions that are
currently running are never touched.

## LLM Disclaimer

This tool is **almost completely** written by a large language model.

## Install

Needs Python 3.12 or newer. Tested on Linux.

```sh
uv tool install git+https://github.com/Hs293Go/claudetop
# or
pipx install git+https://github.com/Hs293Go/claudetop
```

From a checkout, `uv run claudetop` runs it without installing.

## Usage

```sh
claudetop                 # the TUI
claudetop --report        # plain-text summary
claudetop --json          # the whole scan as JSON
claudetop --trash         # list trash batches
claudetop --restore latest
claudetop --hard          # delete permanently instead of using the trash
```

Navigation is vim-style: `j`/`k` down/up · `gg`/`G` top/bottom ·
`ctrl+d`/`ctrl+u` half page · `ctrl+f`/`ctrl+b` full page · `h`/`l` scroll
sideways · `tab`/`shift+tab` or `gt`/`gT` next/previous tab (or `1`–`4`) · `/`
filter (the only way into it; `enter` or `tab` leaves it, `esc` clears it) ·
`enter` details, `q` or `esc` back

Actions: `s` sort (or click a column header) · `d` delete · `m` rehome · `x`
prune · `O` include sessions whose project dir is gone · `M` include their
auto-memory · `r` rescan · `q` quit

## Development

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
uv run ty check
```

## License

MIT. See [LICENSE](LICENSE).

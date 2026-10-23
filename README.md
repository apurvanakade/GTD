# GTD

A small, single-user Getting Things Done web app: Flask + Jinja + stdlib `sqlite3`. No build step, no JS bundler.

## Install

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer (uv can fetch Python for you).

```bash
git clone https://github.com/apurvanakade/GTD.git
cd GTD
uv sync          # creates .venv and installs Flask (plus pytest for development)
```

Your data lives in `gtd.db` (SQLite, created on first run, gitignored).

## Run

```bash
uv run app.py
```

Then open <http://127.0.0.1:5005>.

### Optional: the `GTD` command (macOS, zsh)

`gtd.sh` starts the server in the background if it isn't running, restarts it when code or templates have changed, and opens the browser. To install it globally:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/gtd.sh" ~/.local/bin/GTD
GTD
```

Make sure `~/.local/bin` is on your `PATH`. Server output goes to `server.log`.

## Configuration

Environment variables, all optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GTD_HOST` | `127.0.0.1` | Address to listen on |
| `GTD_PORT` | `5005` | Port to listen on |
| `GTD_DB` | `~/Dropbox/Apps/GTD/gtd.db` if that folder exists, else `./gtd.db` | Path to the SQLite file |
| `GTD_SECRET` | random on each start | Flask session key (used for flash messages) |
| `GTD_DEBUG` | off | Set to `1` for Flask debug mode and auto-reload |

The app has no login. Keep it on `127.0.0.1` unless you put something in front of it.

## Tests

```bash
uv run pytest
```

To refuse commits while tests fail, enable the pre-commit hook once per clone:

```bash
git config core.hooksPath .githooks
```

See [CLAUDE.md](CLAUDE.md) for the architecture and the GTD rules built into the code.

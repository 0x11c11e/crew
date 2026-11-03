# crew

One command that brings up the architect / worker workflow on any project:
**Claude Code** as the architect, **Codex** (or Claude) headless sessions as workers, and a live
terminal **dashboard** of every message between them.

```
   you ──prompt──▶ fable (Claude Code, architect)
                      │  plan · dispatch · verify
                      ├──task──▶ worker-1 (Codex)   ──result──▶ fable
                      └──task──▶ worker-2 (Codex)   ──result──▶ fable
                                      │
                 every event ──▶ <project>/.crew/events.jsonl ──▶ dashboard
```

A project gains exactly two things: `crew.toml` at its root and a `.crew/` state folder that
ignores itself in git. crew never edits the project's `CLAUDE.md`, `.claude/`, or git config: the
architect gets its protocol and hooks through `claude --settings` and
`claude --append-system-prompt-file`, both pointing into `.crew/`.

## What you need

| Piece | Install | Check |
|---|---|---|
| Python 3.11+ with `rich` | `pip install rich` (or your distro's `python3-rich`) | `python3 -c "import rich"` |
| Claude Code, signed in | https://code.claude.com/docs, then `claude` | `claude --version` |
| Codex CLI, signed in (for codex workers) | `npm i -g @openai/codex`, then `codex login` | `codex --version` |
| tmux (optional, recommended) | your package manager | `tmux -V` |

No accounts are needed to watch the recorded run: `bin/crew replay samples/todo-demo.jsonl`.

## Run it without installing

Everything runs straight from a clone through `bin/crew`:

```bash
git clone https://github.com/0x11c11e/crew.git ~/crew
~/crew/bin/crew ~/Code/myproject      # asks to generate crew.toml if there is none, then launches
```

Put `bin/` on your PATH (or alias it) to type just `crew`:

```bash
export PATH="$HOME/crew/bin:$PATH"
```

Optional, if you prefer an installed command: `pip install -e .` inside a virtualenv. Nothing in
crew depends on being installed.

## A session

```bash
cd ~/Code/myproject
crew init            # questions with defaults in brackets; --yes takes them all
crew up              # tmux: Claude Code on the left, the dashboard (or status strip and Codex) beside it
```

`crew` with no subcommand does both. Run inside a folder that has a `crew.toml`, it launches
straight away. Anywhere else it asks for the project directory (default: here), offers to write
`crew.toml` if the project has none, prints it, and asks `Launch now?`. When stdin is not a
terminal every question takes its default.

The first time Claude Code starts in a project it asks you to trust the folder. Say yes, or the
hooks that put the architect's side on the dashboard stay off.

Then ask Claude Code for something. It follows the protocol in `.crew/ARCHITECT.md`: posts a plan
(`crew say`), dispatches workers in the background (`crew run worker-1 --task-file …`), is woken up
when each worker exits, verifies for itself, and posts a verdict (`crew verify worker-1 pass …`).
`--continue` resumes a worker's own thread, so a follow-up does not re-explain the task.

Two layouts, set by `layout` under `[dashboard]` (`crew init` defaults to one worker and writes `strip`
for it, `wide` for more):

- **wide**: Claude Code left, the full dashboard right, with a card per agent and the whole conversation.
- **strip**: three panes. Claude Code left, a narrow status strip in the middle, and the worker's own
  interactive Codex right, where you watch it work. The strip shows only what passes between them and
  their states: each task, "working", each result, "waiting for you", the verdict. A Claude worker
  cannot be attached yet, so a project whose worker is Claude keeps the wide layout.

`crew up` starts each session with an empty bus (the old one is archived under `.crew/archive/`).
`crew up --fresh` also archives the workers' threads so they start over. `crew stop` closes it.

## `crew.toml`

```toml
name = "deskfish"            # shown on the dashboard; default: directory name
version = 1

[architect]
name = "fable"               # its name on the bus and the dashboard (default "fable"); not a model
model = "fable"              # passed as `claude --model`; "" = your Claude Code default
notes = """
Run `npm test` before any verdict.
"""                          # optional, appended verbatim to the architect's protocol

[[workers]]
name = "worker-1"            # unique; used as `crew run worker-1`
kind = "codex"               # "codex" | "claude"
model = ""                   # "" = the agent's own default
effort = "medium"            # codex only: model_reasoning_effort
sandbox = "workspace-write"  # codex: read-only | workspace-write | danger-full-access
cwd = "."                    # relative to the project root
timeout = 900                # seconds

[[workers]]
name = "worker-2"
kind = "claude"              # newer than the codex path, see below
model = "opus"
permission_mode = "acceptEdits"
allowed_tools = "Bash Edit Write MultiEdit Read Glob Grep"
cwd = "."
timeout = 900

[dashboard]
split = 55                   # percent of the width given to the dashboard pane
raw = false                  # add a bottom-right pane tailing the newest raw worker log
```

Missing keys take the defaults shown. Unknown keys are ignored with a warning. Duplicate worker
names, an unknown kind, or a `cwd` that does not exist print one line and exit 2.

## Commands

| Command | Does |
|---|---|
| `crew [DIR]` | bring the workflow up, generating `crew.toml` if needed |
| `crew init [DIR] [--yes] [--workers N] [--kind codex\|claude] [--model M] [--worker-model M] [--force]` | write `crew.toml` and `.crew/`; `--model` is the architect's |
| `crew up [DIR] [--fresh] [--detached] [--claude-args "..."] [--dry-run]` | render `.crew/`, reset the bus, start tmux; `--dry-run` prints the pane commands and starts nothing |
| `crew run NAME (--task TEXT \| --task-file PATH) [--continue] [--read-only] [--timeout S]` | run one worker on one task; `--read-only` lets it look but not write; a busy worker exits 1 |
| `crew say TEXT [--to NAME]` | the architect says something on the bus |
| `crew verify NAME pass\|fail TEXT` | the architect's verdict on a worker |
| `crew post --from A [--to B] --kind K --text T [--data JSON]` | append any event |
| `crew tail [-n N] [-f]` | print the bus |
| `crew status` | each worker: kind, model, thread id, tasks done, last result file |
| `crew dashboard [--events FILE] [--layout wide\|strip]` | the live dashboard |
| `crew replay FILE [--speed N]` | play a recording, e.g. `crew replay samples/todo-demo.jsonl --speed 2` |
| `crew snapshot OUT.svg [--events FILE \| --replay FILE] [--focus NAME] [--width W] [--height H] [--layout L]` | render one dashboard frame |
| `crew hook [EVENT]` | Claude Code hook entry (payload on stdin, always exit 0) |
| `crew attach NAME` | run inside a tmux pane with an interactive Codex: `crew run NAME` types into it |
| `crew stop [DIR]` | close the project's tmux session |

Commands that need a project take `--project DIR`, then `$CREW_PROJECT`, then the nearest parent
directory with a `crew.toml`. The architect runs inside the project, so `crew run …` needs no flags.

## The dashboard

- **Header**: the project, then one line of *now*: each agent's state, and the totals so far
  (prompts, tasks, results, pass/fail, tokens, cost).
- **Agents** (left): a card per agent. Line one is its live state (thinking, running `$ command`,
  waiting on a worker, done, failed) and how long it has been in it; line two is the task the
  worker is on (the brief's `# title`) or the last thing the architect said; line three is tasks
  done, tokens, the thread id, and the architect's verdict as a PASS or FAIL badge. The bottom
  edge names what runs the agent and its model. You, the human, are not a card: your prompts
  are in the conversation.
- **Conversation** (right): your prompts (`you ▶ fable`), the plan, each task brief, every shell
  command a worker runs as one line with its exit code and the tail of its output, each result,
  and the verdicts. Claude Code waking the architect after a worker finished shows as a dim
  `woken up: …` line, not as something you said.

The **strip** layout (`crew dashboard --layout strip`, 44 to 56 columns) is a one-line header (project,
elapsed, tokens), a compact card for the architect and the worker (the worker's shows `$ command` while it
runs one), the TASKS ledger, and the exchange, newest at the bottom: tasks, results, verdicts, errors and
the state changes, at most three lines each. Commands, edits and the architect's messages to you are
left out: the panes beside it show them. Only `q` and scrolling work there.

| Key or mouse | Does |
|---|---|
| `1`–`9` or a click on a card | focus that agent (again: back to everyone) |
| `0`, `Esc` | everyone |
| `Tab`, `Shift-Tab` | next / previous agent |
| `↑` `↓`, `PgUp` `PgDn`, mouse wheel | scroll back through the feed |
| `End` | jump back to the newest events |
| `q` | quit the dashboard |

## What lives in `.crew/`

```
.crew/                          mode 700: private to your user (raw logs hold tool output)
  .gitignore                    "*": git never sees any of this
  events.jsonl                  the bus (append-only JSONL, one event per line)
  archive/                      earlier buses, and worker threads archived by --fresh
  ARCHITECT.md                  the architect's protocol, rendered by every `crew up`
  claude-settings.json          hooks + permission allowlist, rendered by every `crew up`
  tmux.conf                     the crew tmux server's look
  workers/<name>/state.json     {thread_id, tasks, last_result, last_exit, kind}
  workers/<name>/task-NN.md     the brief as sent
  workers/<name>/task-NN.result.md   the worker's final message
  workers/<name>/task-NN.raw.jsonl   the agent's raw event stream
```

## Workers

- **Codex** (`kind = "codex"`): `codex exec --json`, resumed with `codex exec … resume <thread>`.
  Every Codex event becomes a bus event: messages, each command when it starts and ends (with exit
  code and output tail), file changes, token usage, the final answer.
- **Claude** (`kind = "claude"`): `claude -p --output-format stream-json --verbose`, resumed
  with `--resume <session>`. Newer than the Codex path: the event mapping is tested against a real
  tool-using recording (`tests/fixtures/claude-stream.jsonl`), and a live `crew run` plus a
  `--continue` on the same session were verified on 2026-11-02 with a trivial task. Larger tasks
  have not been exercised yet. Workers run with `CREW_WORKER=1`, so a Claude worker's own hooks
  never post as the architect.
- **`--read-only`**: for a question, a review or a scan. A Codex worker runs in the `read-only`
  sandbox; a Claude worker gets only `Read Glob Grep`. The architect's protocol tells it to use
  this whenever the request is to look, not to change.

### Attached workers

With the strip layout, `crew up` starts the worker as an interactive Codex in the right pane
(`codex -s <sandbox> -a never -C <cwd> -c 'notify=["crew","codex-notify"]'`) and records the pane in
`.crew/workers/<name>/pane`. To use a Codex you opened yourself in tmux, run `crew attach <name>` in its
pane first. `crew run <name>` then types one line into that pane, "Read the brief in <path> and do it…",
and waits for the reply. When Codex finishes its turn, its notify hook runs `crew codex-notify`, which
writes `task-NN.result.md` and posts the result, so the architect is woken up as with a headless run.
Turns with no task open (you typing to Codex yourself) are left alone. `--read-only` is refused for an
attached worker: its sandbox is set when the pane starts. If the pane is gone, `crew run` says so and
runs headless. The strip layout currently applies only when the first worker is Codex; with a Claude
first worker `crew up` says so and uses wide.

## Tests

```bash
python3 -m unittest discover -s tests
```

Offline: no Claude, Codex or tmux is started. Adapters are tested against the real recordings in
`tests/fixtures/`; the worker runner against fake `codex`/`claude` scripts that replay them.

## Troubleshooting

- **Nothing from the architect on the dashboard**: you have not trusted the folder yet, or Claude
  Code was not started by `crew up`. Check `.crew/claude-settings.json` exists and that the left pane's
  command (see `crew up --dry-run`) includes `--settings .crew/claude-settings.json`.
- **`crew: command not found` inside Claude Code**: `crew up` prepends `bin/` to PATH for the
  architect. If your shell startup files replace PATH outright, add `bin/` to PATH in them.
- **Claude Code asks permission for every `crew …` command**: the allowlist in
  `.crew/claude-settings.json` matches commands that start with `crew say`, `crew run`, …; a command
  prefixed by `cd x &&` or a full path does not match.
- **`codex: command not found`**: install the CLI and run `codex login`. `crew run` posts the error to
  the dashboard and exits 127.
- **A worker prints a warning about unrecognized configuration settings**: a stale key in
  `~/.codex/config.toml`; harmless, and hidden from the dashboard.
- **The card says `fable` but Claude Code shows another model**: `name` is a label, `model` is the
  model. With `model = ""` the architect runs on whatever your Claude Code default is; the card's
  bottom edge shows the model it really used once it has answered. Set `model = "fable"` (or any
  `claude --model` value) to pin it.
- **`crew up` says the session is already running**: it attaches to it. `crew stop` first to restart.
  Edits to `crew.toml`, and new versions of crew, take effect at the next `crew up`.
- **`NAME is already running (task N)`**: a second `crew run` on a busy worker exits 1; nothing is
  queued. Wait for the running task to finish, or give the new one to another worker.
- **`crew stop` does not find a session started by an older crew**: sessions are now named
  `crew-<name>-<6 hex of the project path>`. Close an old one with `tmux -L crew kill-session -t crew-<name>`.
- **No tmux**: `crew up` prints the two commands to run in two terminals.
- **The dashboard looks cramped**: widen it to 120+ columns; it also works at 100.
- **Clicks do nothing**: your terminal does not forward mouse events. The number keys always work.
  Wheeling over the Claude Code pane drops tmux into copy mode; `q` leaves it.

## Origin

crew grew out of a single-project demo of the same workflow, generalised so it can be pointed at
any project.

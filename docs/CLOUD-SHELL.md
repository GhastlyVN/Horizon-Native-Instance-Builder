# Cloud Shell

`cloud-shell.yml` turns a GitHub-hosted runner into a disposable work machine you
can drive from the command line. It is the fallback when a Codespace cannot be
started (quota exhausted) or when the local machine is too slow for a build.

## Run something

```bash
gh workflow run cloud-shell.yml \
  -f script='python -m pytest -q' \
  -f timeout_minutes=90

gh run list --workflow=cloud-shell.yml --limit 1
gh run watch
```

Multi-line scripts work as well:

```bash
gh workflow run cloud-shell.yml -f script='
sudo apt-get update -qq
sudo apt-get install -y -qq jq
jq --version
'
```

## Interactive session

Add `-f interactive=true` to get an SSH and web shell that stays alive after the
script finishes. The session is limited to the account that dispatched the run,
and it ends when the job timeout expires.

```bash
gh workflow run cloud-shell.yml -f script='echo ready' -f interactive=true
gh run watch   # the tmate connection details are printed in the log
```

## Limits

| Item | Value |
| --- | --- |
| Job timeout | 350 minutes maximum (GitHub hard limit: 6 hours) |
| Runner image | `ubuntu-22.04` by default, `runner` input overrides it |
| Cost | None. The repository is public, so standard runner minutes are unmetered |
| Log | Uploaded as the `cloud-shell-log` artifact (14 day retention) |
| Access | Only accounts with write access can dispatch the workflow |

## When not to use it

Anything that needs to survive between runs. Runners are wiped after every job,
so commit or upload the results you need to keep. For persistent work, export
the state as artifacts or push it to a branch.

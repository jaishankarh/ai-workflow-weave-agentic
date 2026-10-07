# Coding standards: sri-aurobindo-works-chat

Product-wide rules for every Repo of this Product. Each one can be judged from a diff alone.
Kept short on purpose; a Repo's own rules files may add more.

## Secrets and config come from env or settings, never hard-coded

API keys, tokens, passwords, URLs of external services, ports and paths that differ per machine
are read from environment variables or the Repo's settings/config module.
Judge: flag any added literal that is a credential or a per-deployment value (a key-like string,
`http(s)://` host, absolute local path) outside a settings module, `.env.example` or tests.

## No hard-coded model names

Which LLM or embedding model is used is configuration, so it can change without a code change.
Judge: flag an added model identifier string (e.g. `"claude-…"`, `"gpt-…"`, `"text-embedding-…"`)
anywhere but a settings/config default or a test.

## No swallowed errors

An error is handled, re-raised, or logged with enough context to act on; never silently dropped.
Judge: flag an added `except`/`catch` whose body is empty, only `pass`/`continue`, or returns a
default without logging or re-raising; likewise `.catch(() => {})` and ignored promise rejections.

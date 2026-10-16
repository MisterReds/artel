# Project instruction files (AGENTS.md)

Artel reads the de-facto standard instruction files used by Codex, Claude Code, Gemini CLI and Cline and puts
them into agent prompts. Discovery is adapted from Codex (openai/codex, Apache-2.0).

## Discovery order
Starting at the project work directory (`shared/`) and walking up to the repository root (the directory holding
`.git`; with no repository only the work directory itself is checked), the **nearest** directory containing one
wins. In a directory the priority is `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.clinerules` (file, or a directory
of rule files). Only one file is used.

## Size cap
The content is capped at 16 KiB; longer files are cut and end with a truncation marker.

## Runtime behavior
The text is injected, framed as untrusted project data, into the planner, worker, reviewer and auditor system
prompts, but only for roles running on the `native` runtime. CLI runtimes (`claude_code`, `codex`, ...) read these
files themselves, so nothing is duplicated for them.

## Dashboard
The project page shows the chosen file path (and a note if it was truncated). It is hidden when no file exists.
The value is stored in project meta as `instructions_file` / `instructions_truncated`.

"""The environment an agent's shell and the agent CLIs start with: an allowlist, never all of ``os.environ``.

A prompt-injected agent can run ``env``; Artel's own secrets (ARTEL_TELEGRAM_TOKEN, GITHUB_TOKEN, cloud keys ...)
must not be in there. Only what the child needs is passed, plus the runtime's own auth variables and the names a
person lists in config ``agent_env: {pass: [...]}``.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil

COMMON = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR", "TEMP", "TMP",
          "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "PROGRAMDATA",
          "HOMEDRIVE", "HOMEPATH", "SYSTEMDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
          "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "COMPUTERNAME", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
          "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "VIRTUAL_ENV",
          "PYTHONIOENCODING", "ARTEL_SHARED", "ARTEL_WORKSPACE",
          "ARTEL_HOME",  # the policy hook and Artel's MCP proxy find policy.yaml through it (a path, not a secret)
          "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy"}
# package-manager settings MCP presets (npx, uvx) need behind a private registry or CA; never their auth tokens
COMMON_RE = re.compile(r"^(LC_.*|XDG_.*|(?i:npm_config_(registry|proxy|https_proxy|cafile|strict_ssl|cache|prefix|userconfig))"
                       r"|UV_(INDEX_URL|EXTRA_INDEX_URL|CACHE_DIR|PYTHON)|PLAYWRIGHT_BROWSERS_PATH)$")
RUNTIME = {
    "claude_code": {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"},
    "codex": {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME"},
    "native": set(),
}
RUNTIME_RE = {"claude_code": re.compile(r"^ANTHROPIC_DEFAULT_[A-Z0-9_]*_MODEL$")}
# no SSH_AUTH_SOCK, DISPLAY: agents must not use the owner's ssh agent or screen
_CLOUD_FLAGS = {"CLAUDE_CODE_USE_FOUNDRY": re.compile(r"^ANTHROPIC_FOUNDRY_"), "CLAUDE_CODE_USE_BEDROCK": re.compile(r"^AWS_"), "CLAUDE_CODE_USE_VERTEX": re.compile(r"^GOOGLE_|^CLOUD_ML_")}
_TRUE = {"1", "true", "yes", "on"}


def agent_env(runtime: str, extra: dict | None = None, config: dict | None = None) -> dict:
    src = os.environ
    allowed = set(COMMON) | RUNTIME.get(runtime, set())
    allowed |= {str(n) for n in ((config or {}).get("agent_env") or {}).get("pass") or []}
    rt_re = RUNTIME_RE.get(runtime)
    env = {k: v for k, v in src.items() if k in allowed or COMMON_RE.match(k) or (rt_re and rt_re.match(k))}
    if runtime == "claude_code":
        for flag, pat in _CLOUD_FLAGS.items():
            if src.get(flag, "").strip().lower() in _TRUE:
                env.update({k: v for k, v in src.items() if pat.match(k)})
    if os.name == "nt":  # Windows names are case-insensitive: keep what the allowlist matches in any case
        up = {a.upper() for a in allowed}
        env.update({k: v for k, v in src.items() if k.upper() in up})
    env.update(extra or {})
    env = with_python_shim(with_loopback_bypass(env))
    if env.get("PATH"):  # PATH order: Artel's tools/bin, the python shim, the rest, its conda environment last
        from .tools import with_tool_dirs

        env["PATH"] = with_tool_dirs(env["PATH"])
    return env


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def with_loopback_bypass(env: dict) -> dict:
    """A proxy from the owner's environment must never carry loopback traffic: an agent's own test server on
    127.0.0.1 would be sent to the proxy and look dead. When any proxy is set, NO_PROXY/no_proxy get loopback."""
    if not any(env.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")):
        return env
    for key in ("NO_PROXY", "no_proxy"):
        have = [h.strip() for h in (env.get(key) or env.get(key.swapcase()) or "").split(",") if h.strip()]
        env[key] = ",".join(have + [h for h in LOOPBACK if h not in have])
    return env


def _path_key(env: dict) -> str | None:
    return next((k for k in env if k.upper() == "PATH"), None)


def python_shim_dir() -> str | None:
    """ARTEL_HOME/shims with a ``python`` that runs ``python3`` (a machine with only python3: Debian, Ubuntu, macOS).
    Created once, idempotent; None when it cannot be written."""
    from .project import artel_home

    d = artel_home() / "shims"
    name, body = ("python.cmd", "@python3 %*\r\n") if os.name == "nt" else ("python", '#!/bin/sh\nexec python3 "$@"\n')
    f = d / name
    try:
        if not f.exists() or f.read_text() != body:
            d.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(body)
            tmp.chmod(0o755)
            os.replace(tmp, f)
    except OSError:
        return None
    return str(d)


def with_python_shim(env: dict) -> dict:
    """Agents' shells, task checks and both CLIs: ``python`` resolves to the user's python3 when the machine has no
    ``python``. A real ``python`` on PATH is never shadowed."""
    key = _path_key(env)
    path = env.get(key) if key else None
    if path is None or shutil.which("python", path=path) or not shutil.which("python3", path=path):
        return env
    shim = python_shim_dir()
    if shim:
        env[key] = shim + os.pathsep + path
    return env


TOOLS = ("python3", "python", "node", "npm", "git", "grep", "rg", "jq", "curl", "make", "docker", "sed", "awk",
         "find", "tar", "unzip", "pip", "pip3", "uv", "cargo", "go", "java", "gcc", "sqlite3")
_inventory: dict = {}


def tool_inventory(config: dict | None = None) -> tuple[list, list]:
    """(available, missing) among the tools tasks usually need, probed once per PATH on the agents' PATH (shims
    included) and cached."""
    env = agent_env("native", config=config)
    path = env.get(_path_key(env) or "PATH") or ""
    if path not in _inventory:
        have = [t for t in TOOLS if shutil.which(t, path=path)]
        _inventory[path] = (have, [t for t in TOOLS if t not in have])
    return _inventory[path]


_NOT_PROGRAMS = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "in", "!",
                 "{", "}", "(", ")", "cd", "set", "export", "unset", "source", ".", ":", "[[", "exit", "return", "eval",
                 "exec", "alias", "type", "ulimit", "umask", "wait", "trap", "read", "shift", "true", "false", "echo",
                 "printf", "test", "[", "pwd", "command", "builtin", "local", "declare"}
_OPERATORS = "();<>|&`\n"
_PREFIX_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"}  # a command follows them
_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _drop_heredoc_bodies(check: str) -> str:
    """Remove the data lines of here-documents; the line that opens one stays (its command still runs)."""
    out: list = []
    end = None
    for line in check.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = _HEREDOC.search(line)
        if m:
            end = m.group(2)
    return "\n".join(out)


def _tokens(check: str) -> list:
    """Shell tokens of ``check``: quoted text is one word, operators are tokens of their own."""
    lex = shlex.shlex(_drop_heredoc_bodies(check.replace("\\\n", " ")), posix=True, punctuation_chars=_OPERATORS)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    out: list = []
    try:
        out.extend(lex)
    except ValueError:  # unbalanced quote: the words read so far still name the program
        pass
    return out


def _substituted(word: str) -> list:
    """Commands run by ``$(...)`` and backtick substitutions inside one word."""
    return [c for m in _SUBSTITUTION.finditer(word) for c in _command_words(m.group(1) or m.group(2) or "")]


def _command_words(check: str) -> list:
    """Words of every simple command in a shell line, in order. Quoting is honoured, so code passed as a quoted
    argument (``python3 -c "a; b | c"``) stays one word instead of being split at its own operators; command
    substitutions inside words (``"$(cmd)"``) are commands of their own."""
    commands: list = [[]]
    nested: list = []
    target = False  # the word after a redirect (``> out``, ``2>&1``, ``<<EOF``) is a file or tag, not a command
    for tok in _tokens(check):
        if set(tok) <= set(_OPERATORS):
            target = ("<" in tok or ">" in tok) and "(" not in tok
            if not target:
                commands.append([])
        elif target:
            target = False
        else:
            commands[-1].append(tok)
            nested.extend(_substituted(tok))
    return [c for c in commands if c] + nested


def _program(words: list) -> str | None:
    """The program a simple command runs, or None for builtins, keywords, paths and variables."""
    while words and (words[0] in _PREFIX_WORDS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])):
        words = words[1:]
    if not words or words[0] in _NOT_PROGRAMS or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", words[0]):
        return None
    return words[0]


def missing_programs(checks: list, config: dict | None = None) -> list:
    """First program of every command in the checks that cannot be found (``which`` on the agents' PATH). Only
    plain names count: paths, variables and shell builtins are not looked up."""
    env = agent_env("native", config=config)
    path = env.get(_path_key(env) or "PATH") or ""
    out: list = []
    for check in checks or []:
        for prog in filter(None, map(_program, _command_words(str(check)))):
            if not shutil.which(prog, path=path) and prog not in out:
                out.append(prog)
    return out

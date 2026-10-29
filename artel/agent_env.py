"""The environment an agent's shell and the agent CLIs start with: an allowlist, never all of ``os.environ``.

A prompt-injected agent can run ``env``; Artel's own secrets (ARTEL_TELEGRAM_TOKEN, GITHUB_TOKEN, cloud keys ...)
must not be in there. Only what the child needs is passed, plus the runtime's own auth variables and the names a
person lists in config ``agent_env: {pass: [...]}``.
"""
from __future__ import annotations

import os
import re
from typing import Optional

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


def agent_env(runtime: str, extra: Optional[dict] = None, config: Optional[dict] = None) -> dict:
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
    if env.get("PATH"):  # what Artel installed for the agents (ARTEL_HOME/tools) must be on their shells' PATH
        from .tools import with_tool_dirs

        env["PATH"] = with_tool_dirs(env["PATH"])
    return with_loopback_bypass(env)


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

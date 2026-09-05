"""Provider-agnostic LLM client seam for the headless driver.

The framework never talks to a model provider directly. Every call goes
through :class:`LLMClient` — a one-method protocol, ``complete`` — so the
reasoning provider is a swappable factor in the experiment rather than a
dependency baked into the loop. Three implementations ship here:

- :class:`ClaudeCLIClient` shells out to the ``claude`` command-line tool in
  headless (``-p``) mode with tools disabled and no session persistence. The
  CLI owns authentication; this process never sees a key. It is the first
  provider the study uses.
- :class:`AnthropicAPIClient` is the SDK seam. Constructing it without the
  ``anthropic`` package raises an ``ImportError`` that says how to install it;
  the call itself is a thin wrapper over the Messages API with a JSON-schema
  output format.
- :class:`FakeLLMClient` returns canned replies for tests and dry runs, and
  records every call it received.

:func:`make_client` resolves a client from a short name (``"claude-cli"``,
``"fake"``, ``"anthropic"``) so a command-line flag maps to an implementation.

Nothing here knows about optimization models, run records, or skills — that is
:mod:`framework.agent_driver`'s job. This module is deliberately the only place
that knows how a provider is invoked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import deque
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

Reply = Union[Dict[str, Any], str]


class LLMError(RuntimeError):
    """A provider call failed or returned something unusable.

    ``stderr`` / ``stdout`` / ``returncode`` carry whatever the provider emitted
    so a failure is diagnosable from the audit log without re-running it.
    """

    def __init__(
        self,
        message: str,
        *,
        stderr: str = "",
        stdout: str = "",
        returncode: Optional[int] = None,
    ):
        super().__init__(message)
        self.stderr = stderr
        self.stdout = stdout
        self.returncode = returncode


@runtime_checkable
class LLMClient(Protocol):
    """One completion: system prompt + user prompt (+ optional JSON schema).

    With ``schema`` the reply must be a ``dict`` conforming to it; without a
    schema the reply is the model's text. ``model`` overrides the client's
    default for this call only. Implementations should raise :class:`LLMError`
    on any failure and expose a ``name`` attribute for audit logs.
    """

    def complete(
        self,
        system: str,
        user: str,
        schema: Optional[Dict[str, Any]] = None,
        *,
        model: Optional[str] = None,
    ) -> Reply: ...


# ---------------------------------------------------------------------------
# helpers shared by the clients
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_ROLE_RE = re.compile(r"^\s*\[role:\s*([A-Za-z0-9_\-]+)\]", re.MULTILINE)


def extract_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort: pull the first JSON *object* out of free text.

    Handles bare JSON, ```json fences, and prose wrapped around an object.
    Returns ``None`` when nothing parses to a dict.
    """
    if not text:
        return None
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def role_of(system: str) -> Optional[str]:
    """Read the ``[role: <name>]`` marker the driver puts on a system prompt's
    first line. Lets a fake client dispatch canned replies by role."""
    m = _ROLE_RE.search(system or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# claude CLI
# ---------------------------------------------------------------------------

#: Environment variables that make a nested ``claude`` refuse to start when the
#: driver itself is launched from inside an interactive Claude Code session.
#: They are dropped from the child's environment by default.
NESTING_ENV_VARS: Sequence[str] = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


class ClaudeCLIClient:
    """Headless completions through the ``claude`` CLI.

    Each call runs::

        claude -p --output-format json --no-session-persistence --tools "" \\
               --max-turns 1 --system-prompt <system> [--json-schema <schema>] \\
               [--model <model>] [--effort <e>] [--max-budget-usd <usd>] <extra_args>

    with the user prompt on stdin (default) or as the positional argument
    (``prompt_via="arg"``). stdin is the default because run records and log
    tails can exceed the per-argument length limit on some platforms.

    The JSON envelope the CLI prints is parsed for ``result`` (the text) and,
    when a schema was supplied, ``structured_output``; if the latter is absent
    the first JSON object in ``result`` is used instead. Cost and usage fields
    from the envelope are kept in :attr:`last_meta` for the audit log.

    ``--bare`` is intentionally never passed: it disables the OAuth login the
    CLI relies on.
    """

    name = "claude-cli"

    def __init__(
        self,
        model: Optional[str] = "sonnet",
        claude_bin: str = "claude",
        extra_args: Sequence[str] = (),
        *,
        max_turns: int = 1,
        timeout: Optional[float] = 900.0,
        prompt_via: str = "stdin",
        effort: Optional[str] = None,
        max_budget_usd: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        drop_env: Sequence[str] = NESTING_ENV_VARS,
    ):
        if prompt_via not in ("stdin", "arg"):
            raise ValueError("prompt_via must be 'stdin' or 'arg'")
        self.model = model
        self.claude_bin = claude_bin
        self.extra_args = tuple(extra_args)
        self.max_turns = int(max_turns)
        self.timeout = timeout
        self.prompt_via = prompt_via
        self.effort = effort
        self.max_budget_usd = max_budget_usd
        self.env = env
        self.drop_env = tuple(drop_env)
        self.last_meta: Dict[str, Any] = {}

    # -- argv -------------------------------------------------------------

    def build_argv(
        self,
        system: str,
        schema: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        user: Optional[str] = None,
    ) -> list:
        argv = [
            self.claude_bin,
            "-p",
            "--output-format", "json",
            "--no-session-persistence",
            "--tools", "",
            "--max-turns", str(self.max_turns),
            "--system-prompt", system,
        ]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]
        chosen = model or self.model
        if chosen:
            argv += ["--model", chosen]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        argv += list(self.extra_args)
        if user is not None and self.prompt_via == "arg":
            argv.append(user)
        return argv

    def _child_env(self) -> Dict[str, str]:
        env = dict(os.environ if self.env is None else self.env)
        for key in self.drop_env:
            env.pop(key, None)
        return env

    # -- the call ---------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        schema: Optional[Dict[str, Any]] = None,
        *,
        model: Optional[str] = None,
    ) -> Reply:
        argv = self.build_argv(system, schema, model, user)
        # With the prompt on argv the child gets /dev/null on stdin: it must
        # never inherit (and block on, or eat) the driver's own stdin.
        stdin_kwargs: Dict[str, Any] = (
            {"input": user} if self.prompt_via == "stdin" else {"stdin": subprocess.DEVNULL}
        )
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._child_env(),
                **stdin_kwargs,
            )
        except FileNotFoundError as e:
            raise LLMError(f"claude binary not found: {self.claude_bin!r} ({e})") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(
                f"claude timed out after {self.timeout}s",
                stdout=_as_text(e.stdout),
                stderr=_as_text(e.stderr),
            ) from e

        if proc.returncode != 0:
            raise LLMError(
                f"claude exited with status {proc.returncode}: {proc.stderr.strip()[:2000]}",
                stderr=proc.stderr,
                stdout=proc.stdout,
                returncode=proc.returncode,
            )

        envelope = self.parse_envelope(proc.stdout)
        self.last_meta = {
            k: envelope.get(k)
            for k in (
                "subtype", "num_turns", "duration_ms", "duration_api_ms",
                "total_cost_usd", "usage", "session_id",
            )
            if k in envelope
        }
        return self.extract_reply(envelope, schema)

    # -- envelope handling (pure, testable) ------------------------------

    @staticmethod
    def parse_envelope(stdout: str) -> Dict[str, Any]:
        """Turn the CLI's stdout into the result envelope dict."""
        text = (stdout or "").strip()
        data: Any
        try:
            data = json.loads(text)
        except ValueError:
            data = extract_json_object(text)
            if data is None:
                raise LLMError("claude printed no JSON envelope", stdout=stdout)
        if isinstance(data, list):
            # stream-json style: the terminal "result" event carries the answer.
            results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
            data = results[-1] if results else (data[-1] if data else None)
        if not isinstance(data, dict):
            raise LLMError("claude envelope was not a JSON object", stdout=stdout)
        return data

    @staticmethod
    def extract_reply(envelope: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> Reply:
        """Pull the answer out of an envelope: structured output when a schema
        was requested, otherwise the result text."""
        if envelope.get("is_error"):
            raise LLMError(
                f"claude reported an error: {envelope.get('result')!r}",
                stdout=json.dumps(envelope)[:4000],
            )
        result = envelope.get("result")
        if schema is None:
            if result is None:
                raise LLMError(
                    "claude returned an envelope with no result text",
                    stdout=json.dumps(envelope)[:4000],
                )
            return result if isinstance(result, str) else json.dumps(result)

        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            return structured
        if isinstance(structured, str):
            obj = extract_json_object(structured)
            if obj is not None:
                return obj
        if isinstance(result, dict):
            return result
        obj = extract_json_object(result) if isinstance(result, str) else None
        if obj is None:
            raise LLMError(
                "claude returned neither structured_output nor a JSON object in result",
                stdout=json.dumps(envelope)[:4000],
            )
        return obj


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ---------------------------------------------------------------------------
# fake client for tests / dry runs
# ---------------------------------------------------------------------------

ResponseFn = Callable[[str, str, Optional[Dict[str, Any]]], Reply]


class FakeLLMClient:
    """Canned replies, three ways:

    - a **sequence** of replies, consumed in call order;
    - a **mapping keyed by role** (``"log-analyzer"``, ``"refiner"``, ...)
      whose values are a list (consumed in order), a single reply (reused), or
      a callable ``(system, user, schema) -> reply``; the role is read from the
      ``[role: ...]`` marker the driver puts on the system prompt;
    - a **callable** ``(system, user, schema) -> reply`` for full control.

    Every call is appended to :attr:`calls` so tests can assert on prompts.
    Running out of replies raises :class:`LLMError` rather than inventing one.
    """

    name = "fake"

    def __init__(
        self,
        responses: Union[None, Iterable[Reply], Mapping[str, Any], ResponseFn] = None,
        *,
        model: str = "fake",
    ):
        self.model = model
        self.calls: list = []
        self.last_meta: Dict[str, Any] = {}
        self._fn: Optional[ResponseFn] = None
        self._by_role: Dict[str, Any] = {}
        self._queue: Deque[Reply] = deque()
        if responses is None:
            return
        if callable(responses):
            self._fn = responses
        elif isinstance(responses, Mapping):
            self._by_role = {
                role: (deque(v) if isinstance(v, (list, tuple)) else v)
                for role, v in responses.items()
            }
        else:
            self._queue = deque(responses)

    def complete(
        self,
        system: str,
        user: str,
        schema: Optional[Dict[str, Any]] = None,
        *,
        model: Optional[str] = None,
    ) -> Reply:
        role = role_of(system)
        self.calls.append({
            "role": role, "system": system, "user": user,
            "schema": schema, "model": model or self.model,
        })
        if self._fn is not None:
            return self._fn(system, user, schema)
        if self._by_role:
            src = self._by_role.get(role)
            if src is None:
                raise LLMError(f"FakeLLMClient: no canned reply for role {role!r}")
            if isinstance(src, deque):
                if not src:
                    raise LLMError(f"FakeLLMClient: replies for role {role!r} exhausted")
                return src.popleft()
            if callable(src):
                return src(system, user, schema)
            return src
        if not self._queue:
            raise LLMError("FakeLLMClient: canned replies exhausted")
        return self._queue.popleft()


# ---------------------------------------------------------------------------
# Anthropic SDK seam
# ---------------------------------------------------------------------------

class AnthropicAPIClient:
    """Direct Messages-API client (the SDK seam).

    Requires the ``anthropic`` package; constructing without it raises an
    ``ImportError`` that says how to install it. When a schema is given the
    request uses ``output_config.format`` (``json_schema``) so the reply text
    is guaranteed JSON; ``effort`` (``low``..``max``) rides in the same
    ``output_config``. Not exercised by the test-suite (no SDK in the study
    environments) — treat as a seam, not a verified provider.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5",
        api_key: Optional[str] = None,
        *,
        max_tokens: int = 16000,
        effort: Optional[str] = None,
        timeout: float = 600.0,
    ):
        try:
            import anthropic  # type: ignore
        except ImportError as e:  # pragma: no cover - exercised via sys.modules patch
            raise ImportError(
                "AnthropicAPIClient needs the 'anthropic' SDK: run `pip install anthropic` "
                "(or install this package with the 'anthropic' extra). To go through the "
                "claude CLI instead, use make_client('claude-cli')."
            ) from e
        self._anthropic = anthropic
        kwargs: Dict[str, Any] = {"timeout": timeout}
        if api_key:
            kwargs["api_key"] = api_key
        self._client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.last_meta: Dict[str, Any] = {}

    def complete(
        self,
        system: str,
        user: str,
        schema: Optional[Dict[str, Any]] = None,
        *,
        model: Optional[str] = None,
    ) -> Reply:  # pragma: no cover - needs the SDK and network
        request: Dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        output_config: Dict[str, Any] = {}
        if self.effort:
            output_config["effort"] = self.effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            request["output_config"] = output_config
        try:
            response = self._client.messages.create(**request)
        except self._anthropic.APIError as e:
            raise LLMError(f"Anthropic API call failed: {e}") from e
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(
                "Anthropic API refused the request"
                + (f" (category={getattr(details, 'category', None)!r}: "
                   f"{getattr(details, 'explanation', None)})" if details else "")
            )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        usage = getattr(response, "usage", None)
        self.last_meta = {
            "model": getattr(response, "model", None),
            "stop_reason": getattr(response, "stop_reason", None),
            "usage": usage.to_dict() if hasattr(usage, "to_dict") else None,
        }
        if schema is None:
            return text
        obj = extract_json_object(text)
        if obj is None:
            raise LLMError("Anthropic API reply contained no JSON object", stdout=text[:4000])
        return obj


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

_FACTORIES: Dict[str, Callable[..., Any]] = {
    "claude-cli": ClaudeCLIClient,
    "claude": ClaudeCLIClient,
    "cli": ClaudeCLIClient,
    "fake": FakeLLMClient,
    "anthropic": AnthropicAPIClient,
    "api": AnthropicAPIClient,
}


def register_client(name: str, factory: Callable[..., Any]) -> None:
    """Add a client under a short name so ``make_client`` can build it."""
    _FACTORIES[name.strip().lower()] = factory


def available_clients() -> list:
    return sorted(_FACTORIES)


def make_client(name: str, **kwargs: Any):
    """Build a client from a short name: ``"claude-cli"``, ``"fake"``,
    ``"anthropic"``. A ``:model`` suffix sets the model (``"claude-cli:opus"``)
    unless ``model=`` is passed explicitly. Extra kwargs go to the constructor.
    """
    base, _, model = (name or "").partition(":")
    key = base.strip().lower()
    if key not in _FACTORIES:
        raise LookupError(f"Unknown LLM client {name!r}. Available: {available_clients()}")
    if model and "model" not in kwargs:
        kwargs["model"] = model
    return _FACTORIES[key](**kwargs)


__all__ = [
    "LLMClient",
    "LLMError",
    "Reply",
    "ClaudeCLIClient",
    "FakeLLMClient",
    "AnthropicAPIClient",
    "NESTING_ENV_VARS",
    "extract_json_object",
    "role_of",
    "make_client",
    "register_client",
    "available_clients",
]

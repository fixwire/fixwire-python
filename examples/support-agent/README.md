# Support agent (Claude)

A customer-support agent with two tools, `lookup_order` and `refund_order`,
in a plain tool-use loop on Claude.

```sh
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt        # or, from this repo: uv pip install -e ../.. anthropic
ANTHROPIC_API_KEY=... FIXWIRE_DSN=https://<key>@<host> python agent.py "Refund ord_1, it arrived broken"
```

What Fixwire shows for each question:

| Code | In Fixwire |
|---|---|
| `fixwire.ai.agent("support-agent", …)` | One agent run: duration, steps, total tokens and cost |
| `fixwire.ai.wrap_anthropic(Anthropic())` | A chat span per model call: model, input/output and cache tokens, stop reason |
| `fixwire.ai.tool(block.name, call_id=…, arguments=…)` | A tool span per call, with a hash of its arguments (repeated identical calls show up as a loop) |
| `raise AlreadyShipped(...)` in a tool | A failed tool span with its error class; the model gets the error and answers anyway |
| `RECORD_AI_CONTENT=1` | Prompts, answers and tool arguments recorded too, redacted on this machine first |

Ask about `ord_1` (shipped, so not refundable) or `ord_2` (still processing).
The model is a setting (`ANTHROPIC_MODEL`); the tracing works the same with any
provider through `fixwire.ai.chat(provider=…, model=…)`.

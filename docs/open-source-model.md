# Running on an open model

The demo line for this phase is "run offline on an open model". This build takes that
further than the plan intended: **there is no other kind of run.** The only provider it
ships is `openai_compat`, and every number in `docs/numbers.md` was produced by an open or
free model behind it.

So this document is not about a fallback path. It is about the path.

---

## 1. Three ways to serve a model

| | For | Cost | Tool calls |
|---|---|---|---|
| **Ollama** | a laptop, development, CI | free | usable on 7B+; expect retries |
| **vLLM** | a GPU box, the scale demo | GPU-hours | good, with the right parser |
| **A free hosted endpoint** | no hardware at all | free, rate-limited | varies by provider |

### Ollama

```bash
ollama serve
ollama pull qwen2.5-coder:7b
```

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=unused
LLM_MODEL=qwen2.5-coder:7b
```

This is what `tests/integration` and the `live` marker use. No key, no quota, no network.

#### Raise the context window first, or the Coder loop will not finish

**Ollama serves 4 096 tokens regardless of what the model supports.** Measured on Ollama
0.34.0: `qwen2.5:7b` advertises `qwen2.context_length: 32768` and is loaded at
`context_length: 4096`. §6 already says a 4 096-token window will not finish a Coder loop;
what it did not say is that 4 096 is what you get by default.

The OpenAI-compatible endpoint gives you no way to ask for more — `num_ctx` is an Ollama
option and `/v1/chat/completions` does not carry it. Bake it into a derived model instead:

```bash
mkdir -p ~/ollama-ctx && cd ~/ollama-ctx
printf 'FROM qwen2.5-coder:7b\nPARAMETER num_ctx 32768\n' > Modelfile
ollama create qwen2.5-coder-32k -f ~/ollama-ctx/Modelfile
```

then set `LLM_MODEL=qwen2.5-coder-32k`. No root, no service restart. Confirm it took:

```bash
curl -s localhost:11434/api/ps | jq '.models[] | {name, context_length}'
# { "name": "qwen2.5-coder-32k:latest", "context_length": 32768 }
```

`/api/ps` reports nothing until a model is loaded, so send one request first.

**Keep the Modelfile under `$HOME` if Ollama came from snap.** A snap-confined `ollama`
cannot read `/tmp`, and `ollama create -f /tmp/Modelfile` fails with *"no Modelfile or
safetensors files found"* — which names neither the real cause nor the fix. The same
confinement applies to Docker; see `scripts/bringup.sh`, which diagnoses that case.

**The KV cache is not free.** 32 768 tokens costs real memory on top of the weights, and
on a CPU-only box that is the difference between a model that fits and one that swaps.
Raise it as far as you need and no further.

### vLLM

```bash
docker compose --profile gpu up -d vllm
```

```bash
LLM_BASE_URL=http://localhost:8001/v1
LLM_API_KEY=unused
LLM_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct
```

A quantized Qwen3-Coder-30B-A3B fits in 24 GB of VRAM; smaller Qwen3-Coder and
DeepSeek-Coder variants fit in less. `VLLM_MODEL`, `VLLM_TOOL_PARSER`, `VLLM_MAX_MODEL_LEN`
and `VLLM_GPUS` override the compose defaults.

**Check the tool-call parser against the release you pull.** `--tool-call-parser=hermes`
matches the Qwen3-Coder instruct models. The wrong parser does not error — it silently
yields no tool calls, so every agent loop runs to `max_iterations` having done nothing, and
the run looks like a model that would not cooperate rather than a flag that was wrong.

### A free hosted endpoint

Any OpenAI-compatible endpoint works: Groq, Cerebras, Gemini's compatibility layer,
OpenRouter's free tier. `make scale-preflight` checks one in about a second and lists the
models a key can actually see, which is faster than finding out after a sandbox and a
3 000-file index. See the `.env.*` files the Makefile reads.

---

## 2. Choosing the provider per run

```bash
curl -XPOST localhost:8000/runs -H "X-API-Key: $AUTOSWE_API_KEY" \
  -d '{"repo_url":"...","goal":"...","provider":"openai_compat"}'
```

The run records it, and the worker builds from the row rather than from `LLM_PROVIDER`.
Before step 6.3 the column was written and ignored, which made `llm_calls.provider`
evidence of the process's configuration rather than of the run's.

A provider this build cannot construct is a **422 at the API**, naming what does work,
rather than a run that is accepted and dies in a worker the caller cannot see. A row that
names one anyway — written before a deployment changed — fails the run with that message
instead of burning three arq retries and leaving it `queued`.

**`anthropic` is a name this build knows and refuses.** The Phase 6 plan is written for a
system whose default is the Anthropic SDK; this one runs on whatever is cheap or free, and
Phase 5 rewrote its own compaction criterion rather than take the dependency. See
`gateway/providers.py`.

---

## 3. The preamble

`agents/prompts/_open_model_preamble.md` is prepended to every role prompt. It is five
rules about tool protocol — one call per turn, arguments matching the schema, finish with
the submit tool, read before you write — and nothing about the work.

That split is deliberate and the plan insists on it: *"Do not tune Anthropic prompts to fix
open-model behaviour; use the preamble."* The eleven `.md` files are the roles. A rule
copied into all eleven is a rule that will be true in nine of them by the time anyone
checks.

The evidence for it is Phase 5's: on a free model the Debugger needed six attempts across
two escalations and a replan, and four of the nine defects that phase found were malformed
tool calls or a forgotten `submit_result`. Those are protocol failures, not reasoning ones.

It rides in the cached prefix, in the same block as the role prompt — not a block of its
own, which would spend one of the four cache breakpoints on text that never changes
independently of the prompt it is glued to. `OPEN_MODEL_PREAMBLE=false` turns it off for a
deployment on a model that follows tool schemas without being told.

---

## 4. What open models actually cost you

From `docs/numbers.md`, all measured rather than estimated:

| | |
|---|---|
| Prompt caching, local model | **0.7745** run-level over 11 steps |
| Prompt caching, free hosted model | **0.617** on a 2 093-file repository |
| A complete scale run | 12 steps, 9.4 minutes, draft PR with 3 commits, **$0.00** |
| Debugger attempts in that run | 6 of 12 steps, across two escalations and a replan |

Two things worth taking from that.

**The Debugger taking half the steps is the honest shape of an open-model run.** The state
machine bounded each attempt and kept going rather than letting it loop, which is the
system working — but it is six attempts where a frontier model might need one.

**Not every endpoint implements prompt caching.** One free hosted model returned
`cached_tokens: 0` on an identical 3 322-token prefix, measured directly. The caching
figures above rest on servers that do cache; on one that does not, the prefix is re-sent
in full on every call and the only thing that changes is how long it takes.

---

## 5. Cost accounting when the API is free

`llm_calls.cost_usd` is 0 for an unpriced model, and `gateway/pricing.py` says so rather
than guessing. A run on a local model reports `$0.00` because that is what the API charged
— the GPU-hours are real and are not in this number. Compare open-model and hosted runs on
**wall-clock and attempts**, not on the dollar column, which is measuring two different
things.

---

## 6. Pitfalls

- **Strict JSON schemas.** Guided-decoding backends reject some schema features
  (`format`, `pattern`, long `enum`s). Phase 0 kept the contracts flat for this reason. If
  a schema is rejected, simplify the contract rather than special-casing the provider.
- **Context windows.** `--max-model-len=131072` is the compose default; a smaller model
  may not have it. `gateway/context.py` clears older tool results at 40 000 tokens, so the
  harness copes, but a 4 096-token window will not finish a Coder loop.
- **Trailing slashes.** `https://host/v1beta/openai/` plus `chat/completions` becomes
  `openai//chat/completions` and 404s. The Makefile strips it; if you are configuring by
  hand, do not leave one.
- **Model ids are not guessable.** Every provider names the same weights differently.
  `make scale-preflight` lists what a key can see.

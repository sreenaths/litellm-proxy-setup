# LLM Proxy using LiteLLM

**Prerequisite: uv**

Collating the best practices to start LiteLLM in proxy mode to server local requests.
- Prompt caching enabled
- Fixed model version for consistent result and better caching
- Allow only local connections
- ... more to be added

## Starting with Anthropic

Set the environment variables
```shell
export ANTHROPIC_API_KEY="<ANTHROPIC_API_KEY>"
export LITELLM_MASTER_KEY="<ANY_STRING>" # Auth bearer token expected in the requests coming into LiteLLM
```

And start LiteLLM
```shell
uv run litellm -c ./configs/anthropic.yaml --host 127.0.0.1
```
You must see a message that the startup completed and is running on http://127.0.0.1:4000.

For testing use the following curl command

```shell
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ANY_STRING>" \
  -d '{
    "model": "claude-haiku-4-5",
    "messages": [
      {"role": "user", "content": "Hi"}
    ]
  }'
```

Configurations are in [configs.yaml](./configs.yaml).

> Special thanks to Krrish & Ishaan for building LiteLLM.

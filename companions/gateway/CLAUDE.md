# Instructions for Claude

Project: ai-gateway
GUID: ea907125

## Context

AI Gateway - unified API for AI model access. FastAPI service providing:
- CLI sessions with persistent Claude processes
- Cloud provider APIs (Anthropic, Gemini, OpenRouter)
- Session management with context preservation

## Key Files

- `main.py` - the door: FastAPI application, cloud providers, /anthropic/* relay
- `model_door.py` - what every agent model call passes, whatever its format: token, forward, record; `anthropic_relay.py` and `openai_relay.py` are its entrances
- `runtime_main.py` - the runtime: owns the agent CLI processes (own unit, loopback)
- `runtime_proxy.py` - the door forwards /api/v1/sessions/* and CLI chats to the runtime
- `model_tokens.py` - tokens of agent processes, digests in sn_core.model_tokens
- `session_manager.py` - CLI session lifecycle management
- `config.py` - Environment configuration
- `models.py` - Pydantic request/response models
- `providers/` - Cloud provider implementations

## Commands

```bash
# Development
cd /opt/smart-node/ai-gateway
source venv/bin/activate
python main.py

# Service management
sudo systemctl start ai-gateway
sudo systemctl status ai-gateway
sudo journalctl -u ai-gateway -f

# Testing
curl http://172.17.0.1:19850/api/v1/health
curl http://172.17.0.1:19850/api/v1/providers
```

## Architecture

```
Client -> door (main.py :8000) -> CloudProviders -> External APIs
                                -> /anthropic/*   -> the provider's upstream (agents' own calls;
                                                     api.anthropic.com, or the cli_providers row's
                                                     ANTHROPIC_BASE_URL)
                                -> /openai/v1/*   -> OpenRouter or OpenAI (Flint's calls, openai_relay.py)
                                -> runtime_proxy  -> runtime (runtime_main.py :8002)
                                                       -> SessionManager / sessions v2
                                                       -> Claude CLI (subprocess, as the agent's user)
                                                            -> door /anthropic/* (token)
                                                       -> Flint (subprocess, as the agent's user)
                                                            -> door /openai/v1/* (token)
```

## Notes

- CLI sessions spawn long-running Claude processes
- Each session maintains conversation context
- Sessions auto-cleanup after timeout (default 1h)
- Max 10 concurrent sessions

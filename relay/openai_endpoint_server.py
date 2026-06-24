"""Standalone runner for the Copilot-backed OpenAI-compatible endpoint -- on its OWN port, so it does
NOT touch the live :8000 /mcp server (and its supervisor) at all.

This serves POST /v1/chat/completions + GET /v1/models backed by the M365-Copilot relay, letting any
OpenAI/LiteLLM harness (OpenHands, TheAgentCompany, ...) use the Copilot-routed Opus 4.8 as its LLM --
no Anthropic API cost. Secrets are read from .env at RUNTIME (MCP_API_KEY for auth, MCP_CDP_URL /
MCP_IMPL_AGENT_URL for the relay); this file contains NONE of them, so it is safe to commit.

    python -m relay.openai_endpoint_server            # serves on 127.0.0.1:8011
    OPENAI_ENDPOINT_PORT=8011 OPENAI_ENDPOINT_HOST=0.0.0.0 python -m relay.openai_endpoint_server
"""
import os

from dotenv import load_dotenv


def main():
    # Load .env (MCP_API_KEY / MCP_CDP_URL / MCP_IMPL_AGENT_URL) and force the compat flag ON for THIS
    # process only -- the live :8000 server's environment is untouched.
    load_dotenv()
    os.environ["OPENAI_COMPAT"] = "1"
    os.environ.setdefault("MCP_CDP_URL", "http://localhost:9222")

    from starlette.applications import Starlette
    from relay.openai_adapter import register_openai_routes
    import uvicorn

    host = os.environ.get("OPENAI_ENDPOINT_HOST", "127.0.0.1")
    port = int(os.environ.get("OPENAI_ENDPOINT_PORT", "8011"))
    app = Starlette()
    if not register_openai_routes(app):
        raise SystemExit("register_openai_routes returned False (OPENAI_COMPAT not honored)")
    print("[openai-endpoint] serving POST /v1/chat/completions + GET /v1/models on %s:%d "
          "(Copilot-backed, separate from :8000/mcp)" % (host, port), flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

"""支持 python -m policy_mcp 启动。"""
import argparse
import os

from dotenv import load_dotenv
import uvicorn

from .server import create_http_app, create_server


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="制度检索与问答 MCP 服务")
    parser.add_argument("transport", nargs="?", choices=["streamable-http", "stdio"], default="streamable-http")
    parser.add_argument("--host", default=os.getenv("POLICY_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("POLICY_MCP_PORT", "8001")))
    args = parser.parse_args()
    try:
        if args.transport == "stdio":
            create_server().run(transport="stdio")
        else:
            app = create_http_app()
            uvicorn.run(app, host=args.host, port=args.port)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()


"""Servidor MCP autônomo: expõe read_url e research_web via FastMCP."""

import argparse
from datetime import datetime

import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from . import config
from .tools.read_url import read_url
from .tools.research import research_web

mcp = FastMCP(
    config.MCP_NAME,
    instructions=(
        "Use research_web para qualquer fato que você não saiba com "
        "certeza absoluta, passando a pergunta inteira em linguagem natural "
        "e UMA VEZ SÓ: ela já busca vários ângulos por dentro, então repetir "
        "a mesma pergunta reescrita relê as mesmas páginas e dobra o tempo "
        "de espera sem trazer material novo. "
        "Use read_url quando o usuário fornecer um link "
        "específico e pedir para lê-lo, resumi-lo ou analisá-lo."
    ),
)
mcp.tool(read_url)
mcp.tool(research_web)


@mcp.prompt
def pesquisador() -> str:
    """Política de pesquisa: sempre pesquisar, nunca inventar, citar a fonte."""
    now = datetime.now().astimezone().strftime("%d/%m/%Y %H:%M")
    return (
        f"Data e hora atual: {now}. "
        "Você é um assistente que responde em português do Brasil. "
        "SEMPRE chame a ferramenta research_web com a pergunta completa "
        "antes de responder qualquer coisa que você não saiba com certeza "
        "absoluta, e responda EXCLUSIVAMENTE com base no resumo que ela "
        "devolver, citando a URL da fonte. Uma chamada por pergunta: a "
        "ferramenta já cobre vários ângulos de busca por dentro, e repetir a "
        "mesma pergunta reescrita só faz o usuário esperar de novo pelo mesmo "
        "material. Nunca invente placares, datas "
        "ou fatos, nem deduza. Se a pesquisa não trouxer a informação, "
        "diga educadamente que não conseguiu encontrar."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="Sobe em streamable-http em vez de stdio")
    args = parser.parse_args()

    if args.http:
        # mcp.run(transport="http") não expõe middleware ASGI (CORS inclusive):
        # precisa montar o app via http_app() e servir com uvicorn na mão.
        app = mcp.http_app(
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=config.MCP_CORS_ALLOW_ORIGINS,
                    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                    allow_headers=[
                        "mcp-protocol-version",
                        "mcp-session-id",
                        "Authorization",
                        "Content-Type",
                    ],
                    expose_headers=["mcp-session-id"],
                )
            ]
        )
        uvicorn.run(app, host=config.MCP_HOST, port=config.MCP_PORT)
    else:
        mcp.run(transport=config.MCP_TRANSPORT)


if __name__ == "__main__":
    main()

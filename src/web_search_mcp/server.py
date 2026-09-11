"""Servidor MCP autônomo: expõe read_url e research_web via FastMCP."""

import argparse
import logging
import time
from datetime import datetime

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware as MCPMiddleware
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from . import config
from .tools.analyze import analyze_urls
from .tools.read_url import read_url
from .tools.research import chain_questions, research_web

mcp = FastMCP(
    config.MCP_NAME,
    instructions=(
        "Use research_web para qualquer fato que você não saiba com "
        "certeza absoluta, passando a pergunta inteira em linguagem natural "
        "e UMA VEZ SÓ: ela já busca vários ângulos por dentro, então repetir "
        "a mesma pergunta reescrita relê as mesmas páginas e dobra o tempo "
        "de espera sem trazer material novo. Passe os nomes e termos do "
        "usuário exatamente como ele escreveu, sem traduzir, trocar ou "
        "explicar entre parênteses: um palpite errado seu vira busca pela "
        "coisa errada. Se a pesquisa não encontrar, diga isso ao usuário e "
        "peça o nome exato em vez de pesquisar de novo com palpites. "
        "Use analyze_urls quando o usuário fornecer link(s) e pedir resumo, "
        "parecer, opinião ou comparação: a leitura e a análise acontecem lá "
        "dentro e só o resultado volta. "
        "Use read_url apenas quando o texto integral de UMA página for "
        "necessário no seu contexto: para várias páginas, analyze_urls faz "
        "a leitura toda de uma vez e devolve só o resultado, enquanto "
        "encadear read_url enche seu contexto à toa. "
        "Ao apresentar qualquer resultado ao usuário, preserve a atribuição "
        "por item: o link markdown que veio junto do fato (research_web e "
        "analyze_urls) ou o link do cabeçalho \"Fonte desta página\" "
        "(read_url), mais as datas. Nunca troque um link por nome de página "
        "ou de veículo escrito por você, nem condense as fontes numa lista "
        "genérica no rodapé: nome sem endereço é fonte que o usuário não "
        "consegue conferir."
    ),
)
mcp.tool(read_url)
mcp.tool(research_web)
mcp.tool(analyze_urls)


# Freio de cadeia: o agente que chama às vezes pesquisa em círculo, trocando
# a pergunta a cada volta — escapa do cache anti-repetição do research_web,
# que só pega a MESMA pergunta. Observado em 10/09/2026 no Open WebUI: 5
# research_web + 3 analyze_urls encadeados, cada um trocando o nome do que
# o usuário perguntou por um palpite novo, 5,3 min até uma resposta errada.
#
# O sinal é estrutural, nunca o assunto: chamadas que começam logo depois
# que a anterior devolveu, na mesma sessão, são o mesmo turno do agente. Nos
# logs desse caso o intervalo entre devolver e chamar de novo foi 0-10 s;
# uma pergunta nova do usuário exige ler a resposta e digitar, bem mais que
# _CHAIN_GAP_SECONDS. Da _CHAIN_SOFT-ésima chamada em diante o resultado vem
# com um aviso; a partir da _CHAIN_HARD-ésima a chamada nem roda — devolve
# na hora a ordem de responder com o que já tem.
_CHAINED_TOOLS = {"research_web", "analyze_urls"}
_CHAIN_GAP_SECONDS = 45.0
_CHAIN_SOFT = 3
_CHAIN_HARD = 5


logger = logging.getLogger(__name__)


class _ChainGuard(MCPMiddleware):
    def __init__(self) -> None:
        # sessão → (quando a última chamada devolveu, tamanho da cadeia)
        self._chains: dict[str, tuple[float, int]] = {}
        # sessão → perguntas de research_web desta cadeia, em ordem. Vão para
        # research.chain_questions: quando o agente reescreve a pergunta e
        # troca o nome do usuário por um palpite, a pesquisa ainda enxerga o
        # nome que ele tinha passado antes (research._carried_from_chain).
        self._asked: dict[str, list[str]] = {}

    def _session(self, context: MiddlewareContext) -> str:
        try:
            return context.fastmcp_context.session_id
        except Exception:
            return "_"

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if context.message.name not in _CHAINED_TOOLS:
            return await call_next(context)

        sid = self._session(context)
        now = time.monotonic()
        last_end, length = self._chains.get(sid, (0.0, 0))
        # O instante é gravado já na entrada, não só na saída: chamadas em
        # paralelo chegam antes de a anterior devolver e também entram na
        # cadeia.
        length = length + 1 if now - last_end <= _CHAIN_GAP_SECONDS else 1
        self._chains[sid] = (now, length)
        if length == 1:
            self._asked[sid] = []
        if len(self._chains) > 200:
            oldest = min(self._chains, key=lambda k: self._chains[k][0])
            del self._chains[oldest]
            self._asked.pop(oldest, None)

        if length >= _CHAIN_HARD:
            logger.warning("_ChainGuard: sessão %s, chamada %d encadeada de %s barrada",
                           sid, length, context.message.name)
            self._chains[sid] = (time.monotonic(), length)
            return ToolResult(content=[TextContent(type="text", text=_chain_stop_note(length))])

        asked = self._asked.setdefault(sid, [])
        token = chain_questions.set(tuple(asked))
        try:
            result = await call_next(context)
        finally:
            chain_questions.reset(token)
        query = (getattr(context.message, "arguments", None) or {}).get("query")
        if context.message.name == "research_web" and isinstance(query, str):
            asked.append(query)
        self._chains[sid] = (time.monotonic(), length)
        if length >= _CHAIN_SOFT:
            logger.warning("_ChainGuard: sessão %s, chamada %d encadeada de %s",
                           sid, length, context.message.name)
            note = _chain_soft_note(length)
            result.content = [TextContent(type="text", text=note), *result.content]
            if isinstance(result.structured_content, dict) and isinstance(
                result.structured_content.get("result"), str
            ):
                result.structured_content["result"] = note + result.structured_content["result"]
        return result


def _chain_soft_note(n: int) -> str:
    return (
        f"AVISO: esta é a {n}ª pesquisa seguida nesta mesma resposta. Se as "
        "anteriores não encontraram o que o usuário pediu, trocar a pergunta "
        "por outro palpite não vai encontrar: responda agora com o que já "
        "tem, diga o que não foi encontrado e peça ao usuário o nome exato ou "
        f"mais contexto. A partir da {_CHAIN_HARD}ª pesquisa seguida, ela "
        "não será mais executada.\n\n"
    )


def _chain_stop_note(n: int) -> str:
    return (
        f"PESQUISA NÃO EXECUTADA: seria a {n}ª seguida nesta mesma resposta. "
        "Responda ao usuário agora com o que as pesquisas anteriores "
        "trouxeram, diga claramente o que não foi encontrado e peça o nome "
        "exato ou mais contexto. Se ele quiser outra pesquisa, ele pede."
    )


mcp.add_middleware(_ChainGuard())


@mcp.prompt
def pesquisador() -> str:
    """Política de pesquisa: sempre pesquisar, nunca inventar, citar a fonte."""
    now = datetime.now().astimezone().strftime("%d/%m/%Y %H:%M")
    return (
        f"Data e hora atual: {now}. "
        "Você é um assistente que responde em português do Brasil. "
        "SEMPRE chame a ferramenta research_web com a pergunta completa, "
        "com os nomes e termos exatamente como o usuário escreveu, "
        "antes de responder qualquer coisa que você não saiba com certeza "
        "absoluta, e responda EXCLUSIVAMENTE com base no resumo que ela "
        "devolver, mantendo a atribuição por item que vier nele (marcações "
        "[n] e a lista de URLs) e as datas, sem substituí-la por nomes de "
        "veículos escritos por você. Uma chamada por pergunta: a "
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

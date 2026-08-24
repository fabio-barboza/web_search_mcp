"""Faithfulness e relevância em 2 passos, via llm.chat().

O juiz costuma ser o mesmo modelo que escreveu o resumo (EVAL_JUDGE_MODEL
vazio cai no MODEL), então ele tende a se dar razão — a nota absoluta é
otimista. Serve para comparar rodadas e pegar alucinação escancarada, não
como avaliação independente.
"""

import logging

from web_search_mcp.llm import chat

logger = logging.getLogger(__name__)

_CLAIMS_INSTRUCTION = (
    "Quebre o texto a seguir em afirmações atômicas (fatos isolados e "
    "verificáveis). Uma afirmação por linha, sem numerar, sem explicar."
)

_JUDGE_INSTRUCTION = (
    "Você julga se uma afirmação é suportada pelo material de referência. "
    "Responda apenas SUPORTADA ou NÃO SUPORTADA, nada mais."
)

_RELEVANCE_INSTRUCTION = (
    "Avalie de 0 a 2 o quanto o resumo responde à pergunta: 0 = não "
    "responde, 1 = responde parcialmente, 2 = responde bem. Responda "
    "apenas o número."
)


def _extract_claims(summary: str) -> list[str]:
    content = chat(system=_CLAIMS_INSTRUCTION, user=summary)
    return [line.strip(" -*\"'") for line in content.splitlines() if line.strip()]


def _judge_claim(claim: str, dossier: str) -> bool | None:
    """True/False, ou None quando o próprio julgamento falhou.

    None em vez de False porque as duas coisas são diferentes: afirmação não
    suportada é resultado, chamada que deu timeout é ausência de resultado.
    Contar uma como a outra rebaixaria a nota por problema de infraestrutura.
    """
    try:
        verdict = chat(
            system=_JUDGE_INSTRUCTION,
            user=f"Material de referência:\n{dossier}\n\nAfirmação: {claim}",
        )
    except Exception as e:
        # Uma execução do eval leva minutos e faz uma chamada por afirmação;
        # deixar uma falha derrubar tudo joga fora todo o trabalho anterior.
        logger.error("_judge_claim: julgamento falhou, afirmação ignorada: %s", e)
        return None
    return "NÃO" not in verdict.upper() and "SUPORTADA" in verdict.upper()


def faithfulness(summary: str, dossier: str, max_claims: int | None = None) -> tuple[float, int, int]:
    """Devolve (nota, suportadas, julgadas). Nota = suportadas / julgadas.

    Afirmações cujo julgamento falhou saem do denominador — a nota fala do
    que foi efetivamente medido.

    max_claims limita quantas afirmações são julgadas (uma chamada de LLM
    por afirmação): amostra as primeiras N em vez de julgar todas. Para
    comparar rodadas basta a amostra; julgar tudo só muda o custo.
    """
    try:
        claims = _extract_claims(summary)
    except Exception as e:
        logger.error("faithfulness: extração de afirmações falhou: %s", e)
        return 0.0, 0, 0
    if not claims:
        return 0.0, 0, 0
    if max_claims:
        claims = claims[:max_claims]

    verdicts = [v for c in claims if (v := _judge_claim(c, dossier)) is not None]
    if not verdicts:
        return 0.0, 0, 0
    supported = sum(verdicts)
    return supported / len(verdicts), supported, len(verdicts)


def relevance(query: str, summary: str) -> int:
    """Nota 0-2 de relevância do resumo à pergunta. -1 quando o juiz falhou."""
    try:
        content = _relevance_call(query, summary)
    except Exception as e:
        logger.error("relevance: julgamento falhou: %s", e)
        return -1
    digits = "".join(c for c in content if c.isdigit())
    return int(digits[0]) if digits else 0


def _relevance_call(query: str, summary: str) -> str:
    return chat(
        system=_RELEVANCE_INSTRUCTION,
        user=f"Pergunta: {query}\n\nResumo: {summary}",
    )

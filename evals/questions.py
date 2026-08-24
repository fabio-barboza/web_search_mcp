"""Conjunto fixo de perguntas para o eval, misturando recent=True e recent=False
para exercitar os dois ramos de instrução em `tools/research.py`."""

QUESTIONS = [
    {"query": "Qual a cotação atual do dólar em reais?", "recent": True},
    {"query": "Quais as principais notícias de tecnologia hoje?", "recent": True},
    {"query": "Quem foi Santos Dumont?", "recent": False},
    {"query": "O que é o protocolo MCP (Model Context Protocol)?", "recent": False},
    # Atemporais técnicas: o MCP não é só notícia — medir apenas pauta do
    # dia otimizaria o pipeline para o caso que menos representa o uso.
    {"query": "Como funciona o fluxo de autorização OAuth2 com PKCE?", "recent": False},
    {"query": "O que é o padrão circuit breaker e quando usar?", "recent": False},
]

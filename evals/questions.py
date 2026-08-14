"""Conjunto fixo de perguntas para o eval, misturando recent=True e recent=False
para exercitar os dois ramos de instrução em `tools/research.py`."""

QUESTIONS = [
    {"query": "Qual a cotação atual do dólar em reais?", "recent": True},
    {"query": "Quais as principais notícias de tecnologia hoje?", "recent": True},
    {"query": "Quem foi Santos Dumont?", "recent": False},
    {"query": "O que é o protocolo MCP (Model Context Protocol)?", "recent": False},
]

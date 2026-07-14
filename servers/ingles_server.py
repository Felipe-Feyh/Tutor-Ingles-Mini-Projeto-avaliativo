"""
Servidor MCP "Ingles" — aprendizado de ingles com flashcards e repeticao espacada.

Problema real: memorizar vocabulario de forma eficiente. A tecnica usada e a
REPETICAO ESPACADA (Spaced Repetition / SRS), o mesmo principio do Anki:
- Cada card tem uma data de "proxima revisao".
- Se voce ACERTA, o intervalo ate a proxima revisao aumenta (1 -> 6 -> dias*facilidade).
- Se voce ERRA, o card volta a aparecer no dia seguinte.
Assim voce revisa muito o que e dificil e pouco o que ja domina.

O algoritmo aqui e uma versao simplificada do SM-2.

Os dados ficam num banco SQLite real em `dados/ingles.db` (persistem entre usos).

Divisao de papeis (importante!):
- ESTE SERVIDOR guarda os cards e faz o AGENDAMENTO (o que o LLM nao faz bem).
- O AGENTE (LLM) gera traducoes/exemplos e AVALIA suas respostas pelo significado.
"""

from mcp.server.fastmcp import FastMCP
from datetime import date, timedelta
import sqlite3
import json
import os

mcp = FastMCP("Ingles")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_DIR = os.path.join(_BASE, "dados")
os.makedirs(_DB_DIR, exist_ok=True)
_DB = os.path.join(_DB_DIR, "ingles.db")

_HOJE = lambda: date.today().isoformat()


def _conectar():
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _conectar() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cards (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   frente TEXT NOT NULL,           -- palavra/expressao em ingles
                   verso TEXT NOT NULL,            -- significado/traducao
                   exemplo TEXT DEFAULT '',        -- frase de exemplo em ingles
                   ipa TEXT DEFAULT '',            -- transcricao fonetica
                   audio TEXT DEFAULT '',          -- url do audio da pronuncia
                   intervalo INTEGER DEFAULT 0,    -- dias ate a proxima revisao
                   facilidade REAL DEFAULT 2.5,    -- fator de facilidade (SM-2)
                   repeticoes INTEGER DEFAULT 0,   -- acertos seguidos
                   proxima_revisao TEXT,           -- data 'YYYY-MM-DD'
                   acertos INTEGER DEFAULT 0,
                   erros INTEGER DEFAULT 0,
                   criado_em TEXT NOT NULL
               )"""
        )
        # Migracao leve: adiciona colunas em bancos antigos, se faltarem.
        for coluna in ("ipa", "audio", "tema"):
            try:
                conn.execute(f"ALTER TABLE cards ADD COLUMN {coluna} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # coluna ja existe


_init_db()


@mcp.tool()
def adicionar_card(word: str, meaning: str, example: str = "",
                   ipa: str = "", audio: str = "", tema: str = "") -> str:
    """Cria um flashcard novo para estudar.

    Use quando o usuario quiser aprender/salvar uma palavra ou expressao em ingles.
    - word: a palavra ou expressao EM INGLES (ex: 'reliable')
    - meaning: o significado/traducao em portugues (ex: 'confiavel')
    - example: uma frase curta em ingles usando a palavra (opcional, mas recomendado)
    - ipa: transcricao fonetica, se souber (opcional, ex: '/rɪˈlaɪəbəl/')
    - audio: url do audio da pronuncia (opcional)
    - tema: assunto do card, para permitir revisao por tema (ex: 'saudacoes')
    O card ja fica agendado para revisao hoje.
    """
    if not word.strip() or not meaning.strip():
        return "Erro: word e meaning sao obrigatorios."
    with _conectar() as conn:
        cur = conn.execute(
            "INSERT INTO cards (frente, verso, exemplo, ipa, audio, tema, proxima_revisao, criado_em) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (word.strip(), meaning.strip(), example.strip(), ipa.strip(),
             audio.strip(), tema.strip(), _HOJE(), _HOJE()),
        )
        novo_id = cur.lastrowid
    return json.dumps(
        {"id": novo_id, "frente": word.strip(), "verso": meaning.strip(),
         "exemplo": example.strip(), "ipa": ipa.strip(), "audio": audio.strip(),
         "tema": tema.strip(), "status": "criado, agendado para hoje"},
        ensure_ascii=False,
    )


@mcp.tool()
def listar_cards() -> str:
    """Lista todos os flashcards do usuario (para revisar o vocabulario ou criar textos)."""
    with _conectar() as conn:
        linhas = conn.execute(
            "SELECT id, frente, verso, exemplo, ipa, audio, tema, proxima_revisao, acertos, erros "
            "FROM cards ORDER BY id"
        ).fetchall()
    if not linhas:
        return "Nenhum card cadastrado ainda."
    return json.dumps([dict(r) for r in linhas], ensure_ascii=False, indent=2)


@mcp.tool()
def cards_para_revisar(limite: int = 10) -> str:
    """Retorna os cards que estao VENCIDOS para revisao hoje (ate 'limite' cards).

    Use no inicio de uma sessao de revisao para saber o que estudar agora.
    Devolve frente, verso e exemplo de cada card devido.
    """
    with _conectar() as conn:
        linhas = conn.execute(
            "SELECT id, frente, verso, exemplo, ipa, audio, tema FROM cards "
            "WHERE proxima_revisao IS NULL OR proxima_revisao <= ? "
            "ORDER BY proxima_revisao LIMIT ?",
            (_HOJE(), max(1, limite)),
        ).fetchall()
    if not linhas:
        return "Nenhum card para revisar agora. Volte mais tarde!"
    return json.dumps([dict(r) for r in linhas], ensure_ascii=False, indent=2)


@mcp.tool()
def registrar_revisao(card_id: int, acertou: bool) -> str:
    """Registra o resultado da revisao de um card e reagenda pela repeticao espacada.

    Chame DEPOIS de o usuario responder um card:
    - acertou=True  -> intervalo aumenta (o card volta a aparecer mais tarde)
    - acertou=False -> card volta a aparecer amanha
    Retorna quando sera a proxima revisao.
    """
    with _conectar() as conn:
        card = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not card:
            return f"Card {card_id} nao encontrado."

        facilidade = card["facilidade"]
        repeticoes = card["repeticoes"]
        intervalo = card["intervalo"]

        if acertou:
            repeticoes += 1
            if repeticoes == 1:
                intervalo = 1
            elif repeticoes == 2:
                intervalo = 6
            else:
                intervalo = round(intervalo * facilidade)
            facilidade = min(3.0, facilidade + 0.1)
            acertos = card["acertos"] + 1
            erros = card["erros"]
        else:
            repeticoes = 0
            intervalo = 1
            facilidade = max(1.3, facilidade - 0.2)
            acertos = card["acertos"]
            erros = card["erros"] + 1

        proxima = (date.today() + timedelta(days=intervalo)).isoformat()
        conn.execute(
            "UPDATE cards SET intervalo=?, facilidade=?, repeticoes=?, "
            "proxima_revisao=?, acertos=?, erros=? WHERE id=?",
            (intervalo, round(facilidade, 2), repeticoes, proxima, acertos, erros, card_id),
        )
    return json.dumps(
        {"card_id": card_id, "acertou": acertou, "proxima_revisao": proxima,
         "intervalo_dias": intervalo},
        ensure_ascii=False,
    )


@mcp.tool()
def estatisticas() -> str:
    """Mostra estatisticas de estudo: total de cards, quantos vencem hoje e progresso."""
    with _conectar() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
        devidos = conn.execute(
            "SELECT COUNT(*) AS n FROM cards "
            "WHERE proxima_revisao IS NULL OR proxima_revisao <= ?",
            (_HOJE(),),
        ).fetchone()["n"]
        dominados = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE intervalo >= 21"
        ).fetchone()["n"]
        ac = conn.execute("SELECT COALESCE(SUM(acertos),0) AS a, "
                          "COALESCE(SUM(erros),0) AS e FROM cards").fetchone()
    total_rev = ac["a"] + ac["e"]
    taxa = f"{(ac['a'] / total_rev * 100):.0f}%" if total_rev else "sem revisoes"
    return json.dumps(
        {"total_cards": total, "para_revisar_hoje": devidos,
         "dominados_21dias": dominados, "revisoes_feitas": total_rev,
         "taxa_de_acerto": taxa},
        ensure_ascii=False, indent=2,
    )


@mcp.tool()
def remover_card(card_id: int) -> str:
    """Remove um flashcard pelo ID."""
    with _conectar() as conn:
        cur = conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
    return "Card removido." if cur.rowcount else f"Card {card_id} nao encontrado."


@mcp.tool()
def remover_todos_cards() -> str:
    """Remove TODOS os flashcards do usuario. Operacao irreversivel."""
    with _conectar() as conn:
        cur = conn.execute("DELETE FROM cards")
    return f"Removidos {cur.rowcount} card(s). Sua colecao esta vazia."


if __name__ == "__main__":
    mcp.run(transport="stdio")

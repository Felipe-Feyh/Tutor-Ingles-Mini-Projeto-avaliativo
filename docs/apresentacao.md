# Apresentação — Tutor de Inglês (Agente LangGraph)

Autor: Felipe Feyh · IA para Desenvolvedores · Módulo 2

> Conteúdo pronto para 2 slides (copie para PowerPoint/Google Slides e exporte em PDF).

---

## SLIDE 1 — Problema e Proposta

**Problema**
Memorizar vocabulário em inglês exige método (repetição espaçada), mas apps de
flashcards tradicionais não geram conteúdo nem avaliam respostas de forma inteligente.

**Proposta do agente**
Um tutor de inglês que conversa em português e automatiza o ciclo de estudo:
aprende palavras (com dados reais de dicionário), gera vocabulário por tema, cria
textos de leitura, revisa com repetição espaçada e acompanha o progresso.

**Entrada esperada**
Texto livre em português — ex: `quero aprender reliable`, `termos sobre java`,
`texto sobre viagem`, `revisar`, `progresso`, `apague os cards de java`.

**Saída esperada**
Card salvo (definição, exemplo, sinônimos, IPA, áudio), lista de termos, texto com
perguntas, feedback de revisão, estatísticas ou confirmação de remoção.

---

## SLIDE 2 — Arquitetura e Fluxo (LangGraph)

**Fluxo (StateGraph)**
```
START → validar_entrada → identificar_intencao → [roteamento por intenção]
  ├─ aprender     → dicionário → montar card → traduzir → salvar (SQLite)
  ├─ gerar_termos → gerar lista → salvar vários
  ├─ leitura      → gerar texto  |  responder → avaliar → salvar vocabulário
  ├─ revisar      → buscar cards (SRS)  |  responder_card → avaliar
  ├─ remover      → contar → confirmar → executar
  └─ progresso    → estatísticas
→ gerar_resposta_final → END
```

**Ferramentas**
- Free Dictionary API (definições, sinônimos, IPA, áudio reais)
- SQLite (persistência de flashcards + agendamento por repetição espaçada)

**Memória**
- Curto prazo: estado do grafo + sessões entre turnos (fila de revisão, leitura)
- Longo prazo: banco SQLite (persiste entre execuções)

**Segurança e validação**
Chave de API no `.env` (com `.gitignore` e `.env.example`), validação de entrada,
confirmação em operações destrutivas.

**Stack:** Python · LangGraph · LangChain · Groq (LLM) · SQLite · Free Dictionary API

---

*Observação: transformar em 2 slides visuais. Sugestão — Slide 1 com o problema/proposta*
*e um exemplo de diálogo; Slide 2 com o diagrama do grafo e a tabela de ferramentas.*

# Modelos disponíveis no Groq (free tier)

Troque o `GROQ_MODEL` no `.env` conforme necessidade:

| Modelo | Qualidade | Cota diária |
|---|---|---|
| `openai/gpt-oss-120b` | Melhor em seguir instruções e tool calling | ~200k tokens/dia |
| `openai/gpt-oss-20b` | Boa, mas pode errar em classificações | Cota separada |
| `llama-3.3-70b-versatile` | Bom para texto livre, pior em JSON | ~200k tokens/dia |
| `llama-3.1-8b-instant` | Rápido, mas fraco em tool calling com muitas tools | Alta cota |

Se aparecer `RateLimitError 429`, troque para um modelo com cota separada ou aguarde o reset.

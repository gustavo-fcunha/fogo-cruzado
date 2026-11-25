# Fogo Cruzado — base de ocorrências

Job semanal no Railway que baixa o **CSV** de ocorrências de todas as cidades em
https://api.fogocruzado.org.br/search (estado → cidade → tipo "Completo" → BUSCAR → CSV),
confere cada cidade com o total da API e grava tudo na tabela `ocorrencias` do Postgres.

## Arquivos

| Arquivo | O que faz |
|---|---|
| `atualizar_base.py` | Fluxo completo que o Railway roda: baixa → confere → compila → substitui a tabela numa transação → registra em `execucoes`. Se alguma cidade não conferir, a base anterior é mantida e o job sai com erro. |
| `rpa_fogo_cruzado.py` | RPA com Playwright. Cidades com mais de 8 mil ocorrências são baixadas ano a ano (a API limita 60 requisições/min). |
| `gerar_relatorio.py` | Conferência de cada cidade com a API; gera `relatorio.csv`. |
| `Dockerfile` | Python 3.12 + Playwright/Chromium. |

## Tabelas

- `ocorrencias`: uma linha por ocorrência (`id` é a chave), 32 colunas do CSV do site com tipos
  (`data` em horário de Brasília, booleanos, inteiros) + `atualizado_em`.
- `execucoes`: histórico de cada rodada (`status` sucesso/falha, totais, `relatorio` jsonb por cidade).

## Railway

- Variável: `DATABASE_URL = ${{Postgres.DATABASE_URL}}`
- Cron: `0 9 * * 1` (UTC) = toda segunda às 06:00 em Brasília
- Restart policy: `NEVER` (o job termina sozinho; falha fica visível no deploy)

## Rodar localmente

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
DATABASE_URL=postgresql://... .venv/bin/python atualizar_base.py            # fluxo completo
DATABASE_URL=postgresql://... .venv/bin/python atualizar_base.py --carregar downloads_csv   # só carrega uma pasta
.venv/bin/python rpa_fogo_cruzado.py --estado Bahia                          # só o RPA
```

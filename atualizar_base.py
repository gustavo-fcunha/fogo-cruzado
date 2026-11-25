"""
Atualização semanal da base Fogo Cruzado no Postgres (job agendado no Railway).

Passos:
  1. Baixa o CSV de todas as cidades pelo site (rpa_fogo_cruzado), numa pasta nova.
  2. Confere cada cidade com o total da API (gerar_relatorio).
  3. Se tudo bater, junta os CSVs e substitui a tabela `ocorrencias` numa única
     transação. Se algo falhar, a base anterior fica intacta e o job termina com
     erro (o Railway marca a execução como falha).
  4. Registra a execução na tabela `execucoes`.

Uso:
  python atualizar_base.py                       # fluxo completo (o que o Railway roda)
  python atualizar_base.py --carregar downloads_csv
                                                 # só valida e carrega uma pasta já baixada
Variável obrigatória: DATABASE_URL (string de conexão do Postgres).
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

import psycopg

import gerar_relatorio
import rpa_fogo_cruzado

COLUNAS = [
    ("id", "text PRIMARY KEY"),
    ("address", "text"), ("region", "text"), ("state", "text"), ("city", "text"),
    ("neighborhood", "text"), ("sub_neighborhood", "text"), ("locality", "text"),
    ("latitude", "double precision"), ("longitude", "double precision"),
    ("data", "timestamp"),  # horário de Brasília, como o site exibe
    ("police_action", "boolean"), ("agent_presence", "boolean"),
    ("police_unit", "text"), ("main_reason", "text"), ("complementary_reason", "text"),
    ("clippings", "text"), ("massacre", "boolean"),
    *[(f"{grupo}_{tipo}", "integer")
      for grupo in ("civilians", "agents", "men", "women", "kids", "teenagers", "elderly")
      for tipo in ("dead", "wounded")],
]
NOMES = [nome for nome, _ in COLUNAS]

DDL = f"""
CREATE TABLE IF NOT EXISTS ocorrencias (
    {", ".join(f"{nome} {tipo}" for nome, tipo in COLUNAS)},
    atualizado_em timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ocorrencias_state_city ON ocorrencias (state, city);
CREATE INDEX IF NOT EXISTS ocorrencias_data ON ocorrencias (data);
CREATE TABLE IF NOT EXISTS execucoes (
    id serial PRIMARY KEY,
    inicio timestamptz NOT NULL,
    fim timestamptz NOT NULL,
    status text NOT NULL,              -- sucesso | falha
    total_ocorrencias integer,
    cidades integer,
    detalhe text,
    relatorio jsonb                    -- conferência por cidade
);
"""


def converter(campo: str, valor: str):
    tipo = dict(COLUNAS)[campo]
    if valor == "" and not tipo.startswith("text"):
        return None
    if tipo == "double precision":
        return float(valor)
    if tipo == "integer":
        return int(valor)
    if tipo == "boolean":
        return valor == "true"
    if tipo == "timestamp":
        return datetime.strptime(valor, "%d/%m/%Y, %H:%M:%S")
    return valor


def compilar(pasta: Path, relatorio: list[list]) -> Path:
    """Junta os CSVs das cidades (na ordem do relatório) em pasta/fogo_cruzado_compilado.csv."""
    destino = pasta / "fogo_cruzado_compilado.csv"
    cabecalho = None
    with destino.open("w", newline="", encoding="utf-8") as saida:
        w = csv.writer(saida, lineterminator="\r\n")
        for linha in relatorio:
            arquivo = pasta / linha[2]
            if not linha[2] or not arquivo.exists():
                continue  # cidade sem ocorrências
            with arquivo.open(newline="", encoding="utf-8") as f:
                leitor = csv.reader(f)
                cab = next(leitor)
                if cabecalho is None:
                    cabecalho = cab
                    w.writerow(cab)
                elif cab != cabecalho:
                    raise RuntimeError(f"cabeçalho diferente em {linha[2]}")
                w.writerows(leitor)
    if cabecalho != NOMES:
        raise RuntimeError(f"colunas do CSV mudaram no site: {cabecalho}")
    return destino


def carregar(conn, compilado: Path, agora: datetime) -> int:
    """Substitui o conteúdo de `ocorrencias` pelo CSV compilado, numa transação:
    quem consulta a tabela vê a base antiga até o commit."""
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE carga (LIKE ocorrencias) ON COMMIT DROP")
        with compilado.open(newline="", encoding="utf-8") as f, \
                cur.copy(f"COPY carga ({', '.join(NOMES)}, atualizado_em) FROM STDIN") as copy:
            for registro in csv.DictReader(f):
                copy.write_row([converter(c, registro[c]) for c in NOMES] + [agora])
        cur.execute("SELECT count(*) FROM carga")
        total = cur.fetchone()[0]
        cur.execute("DELETE FROM ocorrencias")
        cur.execute("INSERT INTO ocorrencias SELECT * FROM carga")
    return total


def registrar(conn, inicio, status, total=None, cidades=None, detalhe=None, relatorio=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO execucoes (inicio, fim, status, total_ocorrencias, cidades, detalhe, relatorio)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (inicio, datetime.now(timezone.utc), status, total, cidades, detalhe,
             json.dumps(relatorio, ensure_ascii=False) if relatorio is not None else None),
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carregar", type=Path, help="pula o download e carrega esta pasta")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Defina DATABASE_URL com a conexão do Postgres.")

    inicio = datetime.now(timezone.utc)
    print(f"Atualização iniciada em {inicio:%Y-%m-%d %H:%M} UTC", flush=True)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(DDL)
        relatorio = None
        try:
            if args.carregar:
                pasta = args.carregar
            else:
                pasta = Path(tempfile.mkdtemp(prefix="fogo_cruzado_"))
                print(f"Baixando para {pasta}", flush=True)
                rpa_fogo_cruzado.executar(pasta, force=True)

            print("\nConferindo com a API...", flush=True)
            relatorio = gerar_relatorio.gerar(pasta)
            divergentes = [f"{l[0]}/{l[1]}" for l in relatorio if l[5] != "SIM"]
            if divergentes:
                raise RuntimeError(f"{len(divergentes)} cidade(s) não conferem: {', '.join(divergentes)}")

            compilado = compilar(pasta, relatorio)
            with conn.transaction():
                total = carregar(conn, compilado, inicio)
                registrar(conn, inicio, "sucesso", total, len(relatorio), relatorio=relatorio)
            print(f"\nBase atualizada: {total} ocorrências de {len(relatorio)} cidades.", flush=True)
        except Exception as e:
            detalhe = f"{type(e).__name__}: {e}"
            registrar(conn, inicio, "falha", detalhe=detalhe + "\n" + traceback.format_exc(),
                      relatorio=relatorio)
            print(f"\nFALHA — base anterior mantida. {detalhe}", flush=True)
            sys.exit(1)


if __name__ == "__main__":
    main()

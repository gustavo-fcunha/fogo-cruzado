"""
Gera downloads_csv/relatorio.csv a partir dos CSVs baixados pelo RPA, conferindo
a quantidade de ocorrências de cada cidade com o total informado pela API do site.

Uso: python gerar_relatorio.py [pasta]   (padrão: downloads_csv)
"""

import csv
import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from rpa_fogo_cruzado import OUT_DIR, slug

API = "https://api-service.fogocruzado.org.br/api/v2"
# Chave pública que o próprio site usa (está no JavaScript da página de busca).
API_SECRET = os.environ.get("FC_API_SECRET", "3x4Aur@ircW4Pg@bOi55w3QOx6BjrLC*f47xV5aGGFtMEfn0^I")
CONTAGENS = ["civilians_dead", "civilians_wounded", "agents_dead", "agents_wounded"]


def api(caminho: str, **params):
    url = f"{API}{caminho}?{urllib.parse.urlencode(params)}"
    for _ in range(6):
        saida = subprocess.run(
            ["curl", "-s", "-H", f"x-api-secret: {API_SECRET}", url],
            capture_output=True, text=True,
        ).stdout
        try:
            return json.loads(saida)
        except json.JSONDecodeError:
            time.sleep(20)  # limite de 60 requisições/min
    raise RuntimeError(f"API não respondeu: {url}")


def data_ocorrencia(texto: str):
    try:
        return datetime.strptime(texto, "%d/%m/%Y, %H:%M:%S")
    except ValueError:
        return None


def gerar(out_dir: Path = OUT_DIR) -> list[list]:
    """Confere cada cidade de out_dir com a API, grava out_dir/relatorio.csv e
    devolve as linhas (sem a de TOTAL). Coluna 5 = "SIM"/"NAO"."""
    linhas = []
    for estado in api("/states")["data"]:
        for cidade in api("/cities", stateId=estado["id"])["data"]:
            arquivo = Path(slug(estado["name"])) / f"{slug(cidade['name'])}.csv"
            total_api = api(
                "/occurrences", idState=estado["id"], idCities=cidade["id"],
                typeOccurrence="all", page=1, take=1,
            )["pageMeta"]["itemCount"]
            time.sleep(1.1)

            caminho = out_dir / arquivo
            if not caminho.exists():
                # sem ocorrências o site não gera arquivo: só é erro se a API tiver dados
                linhas.append([estado["name"], cidade["name"], "", 0, total_api,
                               "SIM" if total_api == 0 else "NAO", "", "", *[0] * len(CONTAGENS),
                               "sem ocorrências" if total_api == 0 else "arquivo não encontrado"])
                continue
            with caminho.open(newline="", encoding="utf-8") as f:
                registros = list(csv.DictReader(f))
            ids = {r["id"] for r in registros}
            datas = [d for d in (data_ocorrencia(r["data"]) for r in registros) if d]
            somas = [sum(int(r[c] or 0) for r in registros) for c in CONTAGENS]
            confere = len(ids) == total_api and len(registros) == len(ids)
            linhas.append([
                estado["name"], cidade["name"], str(arquivo), len(registros), total_api,
                "SIM" if confere else "NAO",
                min(datas).strftime("%d/%m/%Y") if datas else "",
                max(datas).strftime("%d/%m/%Y") if datas else "",
                *somas, "",
            ])
            print(f"{estado['name']} / {cidade['name']}: {len(registros)} registros "
                  f"(API {total_api}) {'ok' if confere else 'DIVERGENTE'}", flush=True)

    destino = out_dir / "relatorio.csv"
    with destino.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["estado", "cidade", "arquivo", "registros_no_arquivo",
                    "ocorrencias_na_api", "confere", "primeira_ocorrencia",
                    "ultima_ocorrencia", *CONTAGENS, "observacao"])
        w.writerows(linhas)
        w.writerow(["TOTAL", f"{len(linhas)} cidades", "",
                    sum(l[3] for l in linhas), sum(l[4] for l in linhas),
                    "SIM" if all(l[5] == "SIM" for l in linhas) else "NAO", "", "",
                    *[sum(l[8 + i] or 0 for l in linhas) for i in range(len(CONTAGENS))], ""])
    print(f"\nRelatório: {destino}")
    return linhas


if __name__ == "__main__":
    gerar(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR)

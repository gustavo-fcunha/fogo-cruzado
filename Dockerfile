FROM python:3.12-slim

# curl: usado pelo gerar_relatorio para conferir os totais na API do site
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY rpa_fogo_cruzado.py gerar_relatorio.py atualizar_base.py ./

ENV PYTHONUNBUFFERED=1 TZ=America/Sao_Paulo
CMD ["python", "atualizar_base.py"]

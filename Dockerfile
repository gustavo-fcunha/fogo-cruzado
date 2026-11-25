FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY rpa_fogo_cruzado.py atualizar_base.py ./
COPY base_inicial/ ./base_inicial/

ENV PYTHONUNBUFFERED=1
CMD ["python", "atualizar_base.py"]

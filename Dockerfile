# Imagen de E.D.I.T.H. para servidores (ver docs/despliegue.md).
#
#   docker compose up -d --build      # forma recomendada
#
# Los secretos NO van en la imagen: llegan por variables de entorno (.env).
# Todo lo que cambia mientras el bot corre (cuentas, datos de cada estudiante,
# clave maestra) vive en /datos, que debe ser un volumen para no perderse.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EDITH_DATOS=/datos \
    PORT=8080

# Usuario sin privilegios: si alguien explota el bot, no es root dentro del contenedor.
RUN groupadd --system --gid 1000 edith \
    && useradd --system --uid 1000 --gid edith --create-home --home-dir /home/edith edith

WORKDIR /app

# Primero solo las dependencias: así Docker reutiliza esta capa mientras
# requirements.txt no cambie y las reconstrucciones tardan segundos.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /app/data (semillas: pensum.json, CSV) y /app/ml/modelos deben poder escribirse:
# la app regenera los CSV si faltan y reentrena los modelos cada semana.
# /datos se crea con dueño edith para que el volumen nuevo herede ese dueño.
RUN mkdir -p /datos \
    && chown -R edith:edith /datos /app/data /app/ml/modelos

USER edith

VOLUME ["/datos"]

# Solo se usa en modo webhook (cuando EDITH_WEBHOOK_URL está definida).
EXPOSE 8080

# En modo webhook la app responde GET /salud; en modo polling no abre ningún
# puerto, así que el chequeo se da por bueno (no hay nada que consultar).
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os, sys, urllib.request; \
sys.exit(0) if not os.environ.get('EDITH_WEBHOOK_URL') else \
urllib.request.urlopen('http://127.0.0.1:%s/salud' % os.environ.get('PORT', '8080'), timeout=8)" || exit 1

CMD ["python", "main.py"]

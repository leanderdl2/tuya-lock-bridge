# BUILD_FROM is supplied by the Home Assistant builder from build.yaml, so the
# right base image is picked per architecture. The fallback keeps a plain
# "docker build" working for local testing.
ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

# pycryptodome has no musl wheels and is compiled during install.
RUN apk add --no-cache gcc musl-dev libffi-dev

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

FROM python:3.12-slim
WORKDIR /work
COPY . .
RUN python -m pip install --no-cache-dir .
ENTRYPOINT ["effectfence"]

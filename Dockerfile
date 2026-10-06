FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install '.[server]' && rm -rf /app/src
COPY examples ./examples

RUN useradd --system --create-home bgexplorer
USER bgexplorer

# override any of these with environment variables
ENV FLASK_MONGODB_URI=mongodb://mongo:27017/bgexplorer \
    GUNICORN_CMD_ARGS="--workers 2 --threads 4 --timeout 120 --access-logfile -"
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "bgexplorer:create_app()"]

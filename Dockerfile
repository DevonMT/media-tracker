FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . /app

EXPOSE 8501
CMD ["streamlit", "run", "dashboard.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]

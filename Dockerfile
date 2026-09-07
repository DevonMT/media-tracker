FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . /app

EXPOSE 8501
# Off Streamlit, for the reason Runway went: it shipped a megabyte of
# JavaScript to draw a table, and a shared review corpus has to be
# server-authoritative anyway. dashboard.py stays in the image so the old app
# can still be run by hand if this one needs backing out.
CMD ["uvicorn", "matinee.app:app", "--host", "0.0.0.0", "--port", "8501"]

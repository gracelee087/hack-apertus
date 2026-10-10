FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY data ./data
# the study (2,000 invoices, run once) is shown in the app; the reference run itself is redone at start
COPY results/study ./results/study

ENV PYTHONPATH=/app/src APP_ROOT=/app PYTHONUNBUFFERED=1
EXPOSE 8000
# evaluate (replays recorded Apertus responses unless LLM_API_KEY is set), then serve the web app
CMD ["python", "-m", "agent", "all"]

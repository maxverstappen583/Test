FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy everything
COPY . .

# create data folder
RUN mkdir -p data

ENV PORT=8080

CMD ["python", "bot.py"]

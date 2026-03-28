FROM python:3.11-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh && \
    mkdir -p /tmp/armorcode/log

ENTRYPOINT ["./entrypoint.sh"]
CMD []

FROM alpine:latest

WORKDIR /app

# Install dependencies
RUN apk update && \
    apk upgrade && \
    apk add --no-cache python3 py3-pip && \
    mkdir -p /tmp/armorcode/log

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --break-system-packages

COPY . .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
CMD []

FROM alpine:3.22.4

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

RUN addgroup -g 1001 appgroup && adduser -D -u 1001 -G appgroup 1001 && \
    chown -R 1001:1001 /app /tmp/armorcode

USER 1001

ENTRYPOINT ["./entrypoint.sh"]
CMD []

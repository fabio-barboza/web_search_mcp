# Daemon tor (não o Tor Browser): é só o proxy SOCKS que o research_web usa
# para buscar no Google CSE. Versão fixada; a Tor Project não publica imagem
# oficial do cliente, e o pacote do Alpine community é o mesmo 0.4.9.
FROM alpine:3.23

RUN apk add --no-cache tor=0.4.9.12-r0 curl \
 && install -d -o tor -g tor -m 700 /var/lib/tor

COPY torrc /etc/tor/torrc
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

USER tor
ENTRYPOINT ["/bin/sh", "/usr/local/bin/entrypoint.sh"]

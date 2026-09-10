#!/bin/sh
# O tor recusa ControlPort sem autenticação fora de localhost. O hash sai da
# senha do ambiente a cada boot e entra pela linha de comando, então nada
# precisa ser gravado no container.
set -eu
: "${TOR_CONTROL_PASSWORD:?TOR_CONTROL_PASSWORD não definida}"
HASH=$(tor --quiet --hash-password "$TOR_CONTROL_PASSWORD" | tail -n 1)
exec tor -f /etc/tor/torrc --HashedControlPassword "$HASH"

#!/usr/bin/env bash
set -euo pipefail

OPTIONS=/data/options.json
GENERATED=/data/pomerium.yaml
USER_CONFIG=/config/pomerium.yaml
COOKIE_SECRET_FILE=/data/cookie_secret

log() { echo "[$1] ${*:2}"; }

echo "========================================"
echo "  Pomerium Add-on Starting"
echo "========================================"

# --- Cookie secret: generate once, persist across restarts and updates ---
if [ ! -s "$COOKIE_SECRET_FILE" ]; then
    log INFO "Generating new cookie_secret (persisted in /data)"
    head -c 32 /dev/urandom | base64 | tr -d '\n' > "$COOKIE_SECRET_FILE"
    chmod 600 "$COOKIE_SECRET_FILE"
fi
COOKIE_SECRET=$(cat "$COOKIE_SECRET_FILE")

CONFIG_MODE=$(jq -r '.config_mode // "options"' "$OPTIONS")

# --- File mode: user-supplied pomerium.yaml, passed through verbatim ---
if [ "$CONFIG_MODE" = "file" ]; then
    if [ ! -f "$USER_CONFIG" ]; then
        log ERROR "config_mode is 'file' but $USER_CONFIG does not exist."
        log ERROR "Create pomerium.yaml in this add-on's config directory"
        log ERROR "(/addon_configs/<repo>_pomerium/ on the host) and restart."
        exit 1
    fi
    log INFO "Using user-supplied config: $USER_CONFIG"
    if ! grep -Eq '^[[:space:]]*cookie_secret[[:space:]]*:' "$USER_CONFIG"; then
        log INFO "No cookie_secret in file - injecting the add-on managed one via environment"
        export COOKIE_SECRET
    fi
    exec pomerium -config "$USER_CONFIG"
fi

# --- Options mode: generate pomerium.yaml from add-on options ---
AUTOCERT=$(jq -r '.autocert' "$OPTIONS")
if [ "$AUTOCERT" != "true" ]; then
    CERTFILE=$(jq -r '.certfile // ""' "$OPTIONS")
    KEYFILE=$(jq -r '.keyfile // ""' "$OPTIONS")
    if [ -z "$CERTFILE" ] || [ -z "$KEYFILE" ]; then
        log ERROR "autocert is disabled but certfile/keyfile are not set."
        log ERROR "Either enable autocert or point certfile/keyfile at files under /ssl."
        exit 1
    fi
    for f in "/ssl/$CERTFILE" "/ssl/$KEYFILE"; do
        if [ ! -f "$f" ]; then
            log ERROR "Certificate file not found: $f"
            exit 1
        fi
    done
fi

mkdir -p /data/autocert

jq --arg cookie "$COOKIE_SECRET" '
  def nonempty: (. // "") != "";

  {
    address: ":443",
    cookie_secret: $cookie,
    log_level: (.log_level // "info"),
    routes: ((.routes // []) | map(
      {
        from: .from,
        to: .to,
        allow_websockets: (if .allow_websockets == null then true else .allow_websockets end),
        preserve_host_header: (.preserve_host_header // false)
      }
      + (if (.pass_identity_headers // false) then {pass_identity_headers: true} else {} end)
      + (
        (
          (if (.allow_any_authenticated_user // false) then [{authenticated_user: true}] else [] end)
          + ((.allowed_users // [])   | map(select(nonempty)) | map({email:  {is: .}}))
          + ((.allowed_domains // []) | map(select(nonempty)) | map({domain: {is: .}}))
        ) as $rules
        | if ($rules | length) > 0 then {policy: {allow: {or: $rules}}} else {} end
      )
    ))
  }
  + (if .autocert then
       {autocert: true, autocert_dir: "/data/autocert"}
       + (if (.autocert_staging // false) then {autocert_use_staging: true} else {} end)
     else
       {certificate_file: ("/ssl/" + .certfile), certificate_key_file: ("/ssl/" + .keyfile)}
     end)
  + (if (.http_redirect == null or .http_redirect) then {http_redirect_addr: ":80"} else {} end)
  + (if (.authenticate_service_url | nonempty) then {authenticate_service_url: .authenticate_service_url} else {} end)
  + (if (.idp_provider       | nonempty) then {idp_provider:       .idp_provider}       else {} end)
  + (if (.idp_provider_url   | nonempty) then {idp_provider_url:   .idp_provider_url}   else {} end)
  + (if (.idp_client_id      | nonempty) then {idp_client_id:      .idp_client_id}      else {} end)
  + (if (.idp_client_secret  | nonempty) then {idp_client_secret:  .idp_client_secret}  else {} end)
' "$OPTIONS" > /tmp/pomerium-config.json

ROUTE_COUNT=$(jq '.routes | length' /tmp/pomerium-config.json)
if [ "$ROUTE_COUNT" -eq 0 ]; then
    log WARN "No routes configured - Pomerium will start but proxy nothing."
fi

log INFO "Routes:"
jq -r '.routes[] |
    "  " + .from + "  ->  " + .to +
    (if .policy then "" else "  [WARN: no access policy - all requests DENIED. Set allowed_users, allowed_domains, or allow_any_authenticated_user]" end)' \
    /tmp/pomerium-config.json

if jq -e '.routes[] | select(.from | test("example\\.com"))' /tmp/pomerium-config.json > /dev/null; then
    log WARN "A route still uses the placeholder domain example.com - edit the add-on options."
fi

# JSON is valid YAML, so a plain copy works if yq is unavailable or is not
# the Go flavor; yq just makes the generated file pleasant to read.
if ! { command -v yq > /dev/null 2>&1 \
        && yq -P '.' /tmp/pomerium-config.json > "$GENERATED" 2> /dev/null \
        && [ -s "$GENERATED" ]; }; then
    cp /tmp/pomerium-config.json "$GENERATED"
fi
chmod 600 "$GENERATED"
rm -f /tmp/pomerium-config.json

if [ "$AUTOCERT" = "true" ]; then
    log INFO "TLS: autocert (Let's Encrypt) - ports 443 and 80 must be reachable from the internet"
else
    log INFO "TLS: certificates from /ssl"
fi
log INFO "Generated config: $GENERATED (secrets inside - not logged)"
log INFO "Starting Pomerium..."

exec pomerium -config "$GENERATED"

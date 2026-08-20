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

# Everything written below (cookie secret, generated config) embeds secrets.
umask 077

# Persistent autocert cache for both modes (AUTOCERT_DIR defaults here, see
# Dockerfile).
mkdir -p /data/autocert

# --- Cookie secret: generate once, persist across restarts and updates ---
if [ ! -s "$COOKIE_SECRET_FILE" ]; then
    log INFO "Generating new cookie_secret (persisted in /data)"
    head -c 32 /dev/urandom | base64 | tr -d '\n' > "$COOKIE_SECRET_FILE"
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
    # Environment variables take precedence over the config file in Pomerium,
    # so only inject COOKIE_SECRET when the file does not set a non-empty
    # cookie_secret / cookie_secret_file itself. Accepts block style
    # (`cookie_secret: x`), quoted keys and JSON/flow style
    # (`"cookie_secret": "x"`, `{cookie_secret: x}`); an empty value
    # (`cookie_secret: ""` or a bare `cookie_secret:`) counts as unset so the
    # managed secret is used instead of Pomerium's random per-start fallback.
    if grep -Eq "^[[:space:]{]*[\"']?cookie_secret(_file)?[\"']?[[:space:]]*:[[:space:]]*(\"[^\"]|'[^']|[^\"'#[:space:]])" "$USER_CONFIG"; then
        log INFO "cookie_secret is set in $USER_CONFIG - not injecting the add-on managed one"
    else
        log INFO "No cookie_secret in file - injecting the add-on managed one via environment"
        export COOKIE_SECRET
    fi
    exec pomerium --config "$USER_CONFIG"
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

# Pomerium parses the config file as YAML and JSON is valid YAML, so the jq
# output is written directly as the config (no YAML conversion step needed).
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
  # Optional strings: emit only the non-empty ones so Pomerium defaults apply
  # (no authenticate/idp settings at all = hosted authenticate service).
  + ({authenticate_service_url, idp_provider, idp_provider_url, idp_client_id, idp_client_secret}
     | with_entries(select(.value | nonempty)))
' "$OPTIONS" > "$GENERATED"

ROUTE_COUNT=$(jq '.routes | length' "$GENERATED")
if [ "$ROUTE_COUNT" -eq 0 ]; then
    log WARN "No routes configured - Pomerium will start but proxy nothing."
fi

log INFO "Routes:"
jq -r '.routes[] |
    "  " + .from + "  ->  " + .to +
    (if .policy then "" else "  [WARN: no access policy - all requests DENIED. Set allowed_users, allowed_domains, or allow_any_authenticated_user]" end)' \
    "$GENERATED"

# Refuse to start on the shipped placeholder: with autocert enabled Pomerium
# would otherwise place real Let's Encrypt orders for example.com, fail, and
# retry forever.
if jq -e '[.routes[].from | select(test("[./]example\\.com([:/]|$)"))] | length > 0' "$GENERATED" > /dev/null; then
    log ERROR "A route still uses the placeholder domain example.com."
    log ERROR "Set every route's 'from' to a domain you own (Configuration tab) and start the add-on again."
    exit 1
fi

if [ "$AUTOCERT" = "true" ]; then
    log INFO "TLS: autocert (Let's Encrypt) - ports 443 and 80 must be reachable from the internet"
else
    log INFO "TLS: certificates from /ssl"
fi
log INFO "Generated config: $GENERATED (secrets inside - not logged)"
log INFO "Starting Pomerium..."

exec pomerium --config "$GENERATED"

#!/usr/bin/env bash

WITH_ADMIN_PANEL=false
ADMIN_DIR="$SCRIPT_DIR/../librechat-admin"
ADMIN_SESSION_SECRET=""
ADMIN_SECRET_EXISTS=""

admin_validate() {
  if [[ "$WITH_ADMIN_PANEL" == true ]]; then
    [[ -n "$AUTH_DIR" ]] || { err '--with-admin-panel requires --auth-dir'; return 1; }
    local name
    for name in values config route; do
      [[ -f "$AUTH_DIR/librechat-admin-$name.yaml" ]] || {
        err 'Admin configuration missing; set CHAT_ADMIN_URL and CHAT_ADMIN_GROUP and rerun install-auth.sh'
        return 1
      }
    done
    auth_args+=(-f "$AUTH_DIR/librechat-admin-values.yaml")
  elif [[ -n "$AUTH_DIR" && -f "$AUTH_DIR/librechat-admin-values.yaml" ]]; then
    err 'Admin configuration is enabled; pass --with-admin-panel to preserve the admin policy'
    return 1
  fi
}

admin_preflight() {
  if [[ "$WITH_ADMIN_PANEL" != true ]]; then
    local existing
    existing="$(kubectl -n "$NAMESPACE" get configmap librechat-admin-panel --ignore-not-found -o name)" || return 1
    [[ -z "$existing" ]] || { err 'Admin panel is already configured; pass --with-admin-panel and its --auth-dir to preserve the admin policy'; return 1; }
    existing="$(kubectl -n "$NAMESPACE" get deployment librechat-admin-panel --ignore-not-found -o name)" || return 1
    [[ -z "$existing" ]] || { err 'Admin panel is already deployed; pass --with-admin-panel and its --auth-dir'; return 1; }
    return 0
  fi
  kubectl kustomize "$ADMIN_DIR" >/dev/null
  ADMIN_SECRET_EXISTS="$(kubectl -n "$NAMESPACE" get secret librechat-admin-panel-session --ignore-not-found -o name)" || return 1
  if [[ -n "$ADMIN_SECRET_EXISTS" ]]; then
    ADMIN_SESSION_SECRET="$(kubectl -n "$NAMESPACE" get secret librechat-admin-panel-session -o 'jsonpath={.data.SESSION_SECRET}' | base64 -d)" || return 1
    [[ ${#ADMIN_SESSION_SECRET} -ge 32 && "$ADMIN_SESSION_SECRET" != *$'\n'* && "$ADMIN_SESSION_SECRET" != *$'\r'* ]] || {
      err 'Existing admin SESSION_SECRET is missing, malformed or shorter than 32 characters; restore it explicitly'
      return 1
    }
  else
    ADMIN_SESSION_SECRET="$(openssl rand -hex 32)"
  fi
}

render_admin() {
  kubectl kustomize "$ADMIN_DIR"
  printf '\n---\n'
  command cat "$AUTH_DIR/librechat-admin-config.yaml"
  printf '\n---\n'
  command cat "$AUTH_DIR/librechat-admin-route.yaml"
}

install_admin() {
  if [[ -z "$ADMIN_SECRET_EXISTS" ]]; then
    printf 'SESSION_SECRET=%s\n' "$ADMIN_SESSION_SECRET" |
      kubectl -n "$NAMESPACE" create secret generic librechat-admin-panel-session --from-env-file=/dev/stdin -o yaml --dry-run=client |
      kubectl -n "$NAMESPACE" apply -f - >/dev/null
  fi
  unset ADMIN_SESSION_SECRET
  kubectl -n "$NAMESPACE" apply -f "$AUTH_DIR/librechat-admin-config.yaml"
  kubectl -n "$NAMESPACE" apply -k "$ADMIN_DIR"
  kubectl -n "$NAMESPACE" rollout restart deployment/librechat-admin-panel
  kubectl -n "$NAMESPACE" rollout status deployment/librechat-admin-panel --timeout=5m
  kubectl -n "$NAMESPACE" apply -f "$AUTH_DIR/librechat-admin-route.yaml"
}
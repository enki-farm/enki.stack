#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$HUB_TEST_LOG"
[[ "$1" != --context ]] || shift 2
case "$*" in
  *'rollout status '*|*' wait '* )
    [[ "${HUB_TEST_ROLLOUT_ERROR:-false}" != true ]] || exit 1
    if [[ "$*" == *ResolvedRefs* && "${HUB_TEST_ROUTE_ERROR:-false}" == true ]]; then exit 1; fi
    ;;
  *'get secret '* )
    [[ "${HUB_TEST_SECRET_ERROR:-false}" != true ]] || exit 1
    if [[ "$*" == *'get secret zot-auth '* ]]; then
      [[ "${HUB_TEST_ZOT_SECRET_MISSING:-false}" != true ]] || exit 0
      if [[ "$*" == *'jsonpath='* && "${HUB_TEST_ZOT_KEY_MISSING:-false}" == true ]]; then exit 0; fi
    fi
    if [[ "$*" == *'-o name'* ]]; then
      [[ "${HUB_TEST_EXISTING:-false}" != true ]] || printf 'secret/existing\n'
    elif [[ "$*" == *'.data.MYSQL_USER_NAME'* ]]; then
      printf 'cm9vdA=='
    elif [[ "$*" == *'.data.POSTGRES_USER'* ]]; then
      printf 'aHVi'
    elif [[ "$*" == *'jsonpath='* ]]; then
      printf 'cHJlc2VydmVkLXRlc3QtdmFsdWU='
    fi
    ;;
  *'get clusterstoragecontainer '* )
    [[ -z "${HUB_TEST_INITIALIZER:-}" ]] || printf '%s' "$HUB_TEST_INITIALIZER"
    [[ "${HUB_TEST_CONFLICT:-false}" != true ]] || printf 'another-registry:8080'
    ;;
  *'get pvc '* )
    if [[ "$*" == *'get pvc zot-data '* && "${HUB_TEST_ZOT_DATA_EXISTS:-false}" == true ]]; then
      printf 'persistentvolumeclaim/zot-data\n'
    fi
    [[ "${HUB_TEST_DATA_EXISTS:-false}" != true ]] || printf 'persistentvolumeclaim/existing\n'
    ;;
  *'create secret generic '* )
    if [[ "$*" == *'model-catalog-hf-api-key '* && -n "${HUB_TEST_HF_PAYLOAD:-}" ]]; then
      command cat > "$HUB_TEST_HF_PAYLOAD"
    elif [[ "$*" == *'--from-env-file=/dev/stdin'* ]]; then
      command cat > "$HUB_TEST_PAYLOAD_DIR/${6}"
    else
      for argument in "$@"; do
        case "$argument" in
          --from-file=.dockerconfigjson=*)
            cp "${argument#--from-file=.dockerconfigjson=}" "$HUB_TEST_PAYLOAD_DIR/${6}.json" ;;
        esac
      done
    fi
    printf '{}\n'
    ;;
  'apply -f -') command cat >/dev/null ;;
esac
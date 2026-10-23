kubectl get secret --namespace observability grafana-admin -o jsonpath="{.data.admin-password}" | base64 --decode ; echo

The Grafana chart shadows the image's bundled-plugin directory with a writable
volume because the container root filesystem is read-only. Keep the bundled
plugin IDs required by this deployment explicitly listed in
`values-grafana.yaml`; unlisted bundled plugins are not available after shadowing.
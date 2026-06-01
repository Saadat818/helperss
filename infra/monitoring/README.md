# Monitoring stack

Единый стек для Helper и остальных сервисов:

```text
service /metrics -> Prometheus -> Grafana
server metrics -> node_exporter -> Prometheus -> Grafana
```

## Запуск

```bash
cd infra/monitoring
cp .env.example .env
docker compose up -d
```

Grafana слушает `127.0.0.1:3000`. Наружу ее нужно отдавать только через nginx и HTTPS:

```nginx
include /path/to/infra/monitoring/nginx/grafana.conf;
```

Для публичного адреса задайте:

```env
GRAFANA_ROOT_URL=https://DOMAIN/grafana/
GRAFANA_SERVE_FROM_SUB_PATH=true
```

Prometheus и node_exporter слушают только localhost.

## Helper /metrics

Helper отдает `/metrics` через `prometheus_client`.

Нужные env в Helper:

```env
PROMETHEUS_METRICS_ENABLED=true
PROMETHEUS_SERVICE_NAME=helper
PROMETHEUS_ENV=prod
PROMETHEUS_METRICS_ALLOWED_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
PROMETHEUS_METRICS_TOKEN=
```

Если scrape идет не с localhost/внутренней сети, задайте `PROMETHEUS_METRICS_TOKEN` и добавьте header:

```text
Authorization: Bearer <token>
```

## Подключение других сервисов

В `prometheus/prometheus.yml` раскомментируйте нужный job и замените порт:

```yaml
- job_name: mychat
  metrics_path: /metrics
  static_configs:
    - targets: ["127.0.0.1:PORT"]
      labels:
        service: mychat
        env: prod
```

## Проверка

1. `curl http://127.0.0.1:5003/metrics`
2. Grafana: `https://DOMAIN/grafana/`
3. Grafana datasource `Prometheus` -> `Save & test`
4. Explore: `up`
5. Dashboard: `Helper Overview`

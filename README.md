# Helios dummy workloads

These workloads are intentionally **example/test resources only**. They are not part
of the reusable Helios module and must not be deployed automatically to consumer
clusters.

They validate two different ingestion paths:

1. `helios-dummy-stdout` writes to stdout/stderr every 15 seconds. No OTLP endpoint
   is required. The Helios daemon collector's `filelog` receiver should discover
   these records from `/var/log/pods` automatically.
2. `helios-dummy-otlp` emits synthetic OTLP/HTTP logs, metrics and traces every 15
   seconds. It sets `OTEL_EXPORTER_OTLP_ENDPOINT` to the in-cluster daemon collector
   Service, matching the endpoint used by the module's Instrumentation CR.

## Why the OTLP pod points to the daemon collector

The dummy OTLP pod represents an instrumented application. Application telemetry
should enter the Helios stack through the daemon collector, not bypass it and send
straight to Elasticsearch.

The module exposes the daemon receiver at:

```text
http://opentelemetry-kube-stack-daemon-collector.opentelemetry-operator-system.svc.cluster.local:4418
```

The pieces of that address are intentional:

- `opentelemetry-kube-stack-daemon-collector` is the daemon collector Service.
- `opentelemetry-operator-system` is the Helios namespace fixed by this module.
- `svc.cluster.local` is Kubernetes service discovery inside the cluster.
- `4418` is the module's OTLP/HTTP port. It was moved from the standard `4318`
  because Datadog already owns hostPort `4318` on the target clusters.

The gateway is the egress tier: daemon/cluster collectors forward telemetry to it,
and the gateway exports to Elastic. Pointing a test application directly at Elastic
would bypass the exact in-cluster path this example is intended to prove.

The stdout pod is different: it does **not** need this endpoint because its output is
collected from the Kubernetes node's pod-log files by `filelog`.

## Apply on the dev-8 validation cluster

After Terraform has created the EKS cluster and Helios is healthy:

```bash
kubectl apply -k examples/helios-dummy-pods
kubectl -n helios-test get pods -o wide
```

Watch the synthetic OTLP sender:

```bash
kubectl -n helios-test logs -f deploy/helios-dummy-otlp
```

Expected messages include HTTP `200` responses for `logs`, `metrics` and `traces`.
The response may be `200` with an empty body; that is normal for OTLP/HTTP success.

Watch the plain stdout workload:

```bash
kubectl -n helios-test logs -f deploy/helios-dummy-stdout
```

Also verify the Helios components remain healthy:

```bash
kubectl -n opentelemetry-operator-system get pods -o wide
kubectl -n opentelemetry-operator-system get svc
```

Then search Elastic/Kibana for:

```text
service.name: "helios-dummy-otlp"
```

and/or Kubernetes metadata for the `helios-test` namespace. For the stdout workload,
search for:

```text
helios dummy stdout
```

## Clean up

```bash
kubectl delete -k examples/helios-dummy-pods
```

This deletes only the dummy validation namespace/workloads. It does not touch the
Helios installation itself.

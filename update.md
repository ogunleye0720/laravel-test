## Work completed so far

* **Vendored Elastic's `values.yaml` into the Terraform module**

  * The Helm deployment referenced a local `values.yaml` from `main.tf`, but the file was not present in the repository.
  * This meant that any Terraform `plan` or `apply` attempting to evaluate the Helm release would eventually fail because Terraform could not read the expected values file.
  * The corresponding `values.yaml` for Elastic Helm chart version `0.3.3` has now been fetched and committed into the repository.
  * The downloaded file was validated against the upstream Elastic version using SHA256 to confirm that the vendored copy is byte-for-byte identical.
  * Keeping the upstream file unchanged gives us a clean baseline and makes future upgrades easier to review.

* **Validated the Elastic chart version and required configuration dependencies**

  * Confirmed that Elastic chart version `0.3.3` exists upstream and is available for deployment.
  * Confirmed that the chart version also matches Elastic's expected compatibility marker for the implementation being used.
  * Reviewed the Kubernetes Secret configuration referenced by Terraform and compared it against the `secretKeyRef` blocks used by the Helm chart.
  * The Secret name and expected Secret keys match correctly, so the collectors and gateway should be able to resolve the Elastic endpoint/API key when the required SSM parameters are present.
  * This validation was important because a mismatch here would result in workloads being deployed but unable to authenticate with Elastic.

* **Hardened the Terraform module and removed several hard-coded values**

  * Exposed the Elastic Helm `chart_version` as a Terraform variable instead of keeping it fixed inside the module.
  * Exposed the SSM Parameter Store paths used for the Elastic endpoint and API key as variables.
  * Exposed Helm deployment timeout values as variables so consumers can adjust them where required.
  * Enabled `wait = true` on the Helm release.
  * Set the Helm timeout to `900` seconds.
  * With `wait = true`, Terraform waits for the Helm resources to become ready instead of reporting the release as successfully created immediately after submitting the resources to Kubernetes.
  * The longer timeout gives components such as the operator, gateway, cluster-stats collector and daemon collectors enough time to schedule and initialise before Terraform considers the deployment unsuccessful.
  * Added `outputs.tf` so useful module information can be exposed to consuming Terraform configurations.
  * Added `.gitignore` entries to prevent local Terraform artefacts and other generated files from being accidentally committed.

* **Implemented the requested Kubernetes scheduling behaviour**

  * Applied the team lead's `nodeSelector` and toleration requirements to the Elastic operator.
  * Applied the same scheduling configuration to the `cluster-stats` collector.
  * Applied the same scheduling configuration to the gateway.
  * These workloads are therefore scheduled onto the designated system node group.
  * The scheduling configuration was deliberately not applied to the daemon collector.
  * The daemon runs as a Kubernetes `DaemonSet`, meaning its responsibility is to run a collector on every applicable worker node rather than only on the system node group.
  * Restricting the daemon using the same `nodeSelector` would prevent collection from application nodes.
  * The daemon remains OS-constrained where necessary and tolerates node taints so that it can reach as many cluster nodes as possible.
  * This is required for cluster-wide container logs, host metrics and kubelet statistics.

* **Implemented the Elastic PoC data stream namespace**

  * Added the `resource/data_stream_namespace` processor to all four gateway pipelines.
  * The processor forces telemetry flowing through the gateway into the `ansys_namespace` data stream namespace.
  * This was specifically requested by the Elastic team for the PoC so that Helios telemetry can be clearly isolated and queried separately from other Elastic telemetry.
  * The configuration applies consistently across the gateway pipelines rather than relying on individual workloads to set the namespace.
  * Once the PoC is complete, the override can be removed by setting:

    * `data_stream_namespace = null`
  * After that change, Elastic can remove the temporary `*-ansys_namespace` data streams created for the PoC.

* **Implemented a layered Helm values/overlay model**

  * The upstream Elastic `values.yaml` remains unchanged and byte-identical to Elastic's source.
  * Ansys-specific and environment-specific customisations are applied as additional Helm values documents on top of the upstream file.
  * This avoids directly editing hundreds of lines of upstream Helm configuration.
  * It also means future chart refreshes will produce clean and understandable Git diffs.
  * When Elastic changes its defaults, we can compare the new upstream file separately from our local customisations.
  * This significantly reduces the risk of accidentally carrying undocumented upstream modifications between chart versions.

* **Added drift protection to `fetch-values.sh`**

  * Added two fingerprints to the values refresh process.
  * One fingerprint protects the gateway pipeline processor sections that our overlays depend on.
  * The second protects the collector environment-variable blocks that our overlays also restate.
  * When `fetch-values.sh` downloads a new upstream `values.yaml`, the script checks these sections before replacing the existing file.
  * If Elastic changes either of those upstream structures, the script intentionally fails instead of silently accepting the new file.
  * This prevents our overlay logic from becoming invalid without us noticing.
  * Both drift guards were tested by intentionally modifying the expected sections in both directions and confirming that the refresh fails when the upstream structure no longer matches what the module expects.

---

## Issues encountered and resolutions

* **Issue 1 — `values.yaml` was missing**

  * The Terraform Helm release expected to read a local Elastic `values.yaml`.
  * That file had never been vendored into the repository.
  * As a result, the Terraform configuration was incomplete and a real plan/apply would fail when it attempted to load the file.
  * Resolution:

    * Retrieved the correct upstream file.
    * Confirmed it corresponds to chart version `0.3.3`.
    * Verified its SHA256 against the upstream version.
    * Committed the file into the module.

* **Issue 2 — the original fetch script could destroy a known-good `values.yaml`**

  * The initial refresh process downloaded the upstream file directly over the existing repository copy.
  * Validation happened only after the destination file had already been overwritten.
  * If validation failed, the script then removed the downloaded file.
  * This meant that a failed refresh could leave the repository without the previously working `values.yaml`.
  * Resolution:

    * Changed the script to download into a temporary file first.
    * All version and fingerprint validation now runs against the temporary file.
    * The existing file is left untouched if validation fails.
    * Only after every validation check succeeds is the temporary file moved into place.
  * This makes the refresh process atomic from the module's perspective.

* **Issue 3 — Helm apply failed because an environment variable became a boolean**

  * The Kubernetes API rejected one of the generated environment variable definitions with:

    * `env[6].value must be of type string: "boolean"`
  * The affected value was:

    * `ELASTIC_AGENT_OTEL: "true"`
  * The value was intended to be the string `"true"`.
  * Chart version `0.3.3`, however, uses a `renderenvs` Helm template that renders the value without quoting it.
  * YAML therefore interpreted `true` as the boolean value `true` rather than the string `"true"`.
  * Kubernetes environment variable `value` fields require strings, so the generated resource was invalid.
  * This was confirmed to be a chart behaviour rather than a Terraform issue.
  * Resolution:

    * Wrapped the value in literal quotes through the overlay so the final Helm rendering produces a string.
  * Elastic later corrected this upstream by applying Helm's `| quote` function, but that fix is not available in chart `0.3.3`.
  * The fix is also absent in chart `0.8.0`, so relying on an immediate chart upgrade would not resolve the issue for this implementation.

* **Issue 4 — all collectors entered `ImagePullBackOff`**

  * After resolving the Helm rendering issue, the Kubernetes resources were created but the collector containers could not start.
  * Kubernetes reported `ImagePullBackOff` for the Elastic Agent image.
  * The chart was attempting to pull:

    * `elastic-agent:9.6.0`
  * That image tag does not currently exist in Elastic's published registry and returns a 404.
  * The underlying reason is that the values refresh process retrieves configuration from Elastic's `main` branch.
  * Elastic's `main` branch tracks development toward the next unreleased Elastic Agent version rather than necessarily matching the currently published container image.
  * This created a mismatch between the chart configuration and available container images.
  * Resolution:

    * Pinned the Elastic Agent version to the verified published version:

      * `9.5.0`
    * Applied this through the `image_overlay`.
    * The image value from upstream `main` is therefore intentionally ignored.
  * This allowed the operator, gateway and collectors to pull a valid image.

* **Issue 5 — daemon collectors entered `CrashLoopBackOff`**

  * After the image issue was resolved, the daemon collectors were able to pull and start their containers, but the daemon process repeatedly crashed.
  * The health probes showed `connection refused` because the OpenTelemetry Collector process was terminating before the probe port became available.
  * Investigation showed that this was a collector configuration error.
  * Chart `0.3.3` enables the `kubernetesAttributes` preset by default.
  * That preset configures:

    * `node_from_env_var: K8S_NODE_NAME`
  * However, chart `0.3.3` only injects:

    * `OTEL_K8S_NODE_NAME`
  * As a result, the collector configuration referenced an environment variable that did not exist.
  * The collector therefore failed during startup.
  * Resolution implemented:

    * Injected `K8S_NODE_NAME` using Kubernetes `fieldRef` so it resolves from the node on which the daemon pod is running.
  * This matches the behaviour used by Elastic in newer chart version `0.20.1`.
  * The change has been implemented in Terraform/Helm configuration.
  * Cluster-side confirmation of the fix is still pending.

* **Correction to the initial daemon diagnosis**

  * The daemon restart was initially suspected to be caused by an OOM kill.
  * The pod logs later showed that the collector was failing during configuration loading rather than being terminated by the kernel for memory consumption.
  * The diagnosis was therefore corrected to the missing `K8S_NODE_NAME` configuration described above.

* **Local development environment issue encountered during troubleshooting**

  * During troubleshooting, `.terraform` was briefly deleted.
  * This caused the IDE Terraform language server to become stale because provider/module metadata it expected locally had disappeared.
  * This was a local development issue only and was not related to the Helios Kubernetes deployment itself.

---

## Current cluster status

* **Elastic operator — Running**

  * The operator is deployed successfully.
  * It is scheduled according to the required system-node scheduling rules.

* **Gateway — Running**

  * Both gateway replicas are currently running.
  * The gateway is responsible for receiving and forwarding the telemetry pipelines configured for the PoC.
  * The `ansys_namespace` data stream processor is applied at this layer.

* **Cluster-stats collector — Running**

  * The cluster-level collector is running successfully.
  * Kubernetes events are being collected across the cluster.
  * Kubernetes cluster-state metrics are also being collected.
  * These data types already have full-cluster visibility because they do not depend on one daemon pod existing on every node.

* **Daemon collector — partially operational**

  * Currently only 1 of the 3 expected daemon pods is running successfully.
  * The daemon collector is responsible for:

    * Container/pod logs.
    * Host/node metrics.
    * Kubelet/pod/container metrics.
  * Because only one daemon instance is currently running, those telemetry sources currently cover only one worker node rather than the entire cluster.
  * Full telemetry coverage requires a successfully scheduled and running daemon pod on all three nodes.

* **Traces/APM — currently empty by design**

  * No application traces are expected at this stage.
  * Workloads must carry the appropriate Elastic injection annotation before automatic instrumentation/tracing will begin.
  * Therefore the absence of APM/traces is not currently considered a deployment failure.

---

## Infrastructure-side issue blocking full log and node-metric coverage

* **Two daemon pods are currently Pending due to node memory pressure**

  * Kubernetes reports:

    * `1 Insufficient memory`
  * This is a scheduling problem rather than a collector application failure.
  * The affected nodes cannot currently satisfy even the daemon's `64Mi` memory request.

* **The Kubernetes Cluster Autoscaler will not solve this specific problem**

  * The daemon is deployed as a `DaemonSet`.
  * A DaemonSet is expected to place one pod on each existing eligible node.
  * Adding another node does not solve the fact that the DaemonSet pod intended for an already existing constrained node cannot fit on that node.
  * Cluster Autoscaler therefore correctly does not treat this as a normal scale-up opportunity.
  * Without infrastructure or workload changes, those daemon pods can remain Pending indefinitely.

* **Recommended first option — reclaim memory from existing nodes**

  * The cluster is currently running Datadog alongside EDOT/Helios.
  * Both systems collect overlapping telemetry.
  * If Helios is intended to replace Datadog for this cluster, removing the overlapping Datadog agents would reclaim node resources.
  * This is the preferred first option because it avoids increasing infrastructure size purely to run two telemetry stacks covering similar data.

* **Second option — use Kubernetes priority/preemption**

  * A `PriorityClass` could be assigned to the Helios daemon.
  * This would allow Kubernetes to preempt lower-priority workloads if required to make space for the node-level telemetry agent.
  * This should be considered carefully because preemption deliberately removes another workload to make capacity available.

* **Third option — increase worker node capacity**

  * The worker node instance type can be increased to provide additional memory.
  * This would allow the daemon collector to fit without removing existing workloads.
  * This has a direct infrastructure cost and should therefore be evaluated after determining whether duplicate monitoring agents can be removed.

* **Recommended diagnostic command**

  * To understand exactly which workloads are reserving memory on the affected node, use:

    * `kubectl describe node ip-10-0-16-161 | sed -n '/Non-terminated Pods/,/Events/p'`
  * The important values to inspect are Kubernetes resource **requests**, not only live usage reported by `kubectl top`.
  * Kubernetes schedules pods based primarily on requested resources, so a node can appear to have free live memory while still being unable to accept another requested reservation.

---

## Decisions still outstanding

* **Decision required on the default value of `enable_helios`**

  * The original ticket specifies:

    * `enable_helios = false`
  * A later review comment suggests:

    * `enable_helios = true`
  * There was also a thread stating that this should be confirmed with Adam, but no final decision has been recorded.
  * The module currently remains at:

    * `enable_helios = false`
  * This is the safer default because changing it to `true` would automatically opt every existing module consumer into the Helios deployment during their next Terraform apply.

* **Impact of setting `enable_helios = true` by default**

  * Every existing environment consuming the module would attempt to deploy Helios.
  * Each account and region must already contain the required SSM parameters.
  * If those parameters do not exist, Terraform will fail while evaluating the data sources.
  * This can happen during `terraform plan`, before Helm deployment is even attempted.
  * Enabling the feature globally should therefore only happen after the required configuration exists everywhere that consumes the module.

* **Required SSM parameters must be created before enabling Helios in a new environment**

  * The expected parameters are:

    * `/global/helios/elastic_endpoint`
    * `/global/helios/elastic_api_key`
  * These must exist in the relevant AWS account and region.
  * The Terraform module reads these values before it builds the Kubernetes Secret used by the Helm deployment.
  * Any cluster where these parameters are missing cannot successfully plan/apply with Helios enabled.

---

## Recommended follow-up tickets

* **Replace the self-signed webhook certificate strategy**

  * The current chart uses `autoGenerateCert` for the webhook.
  * This results in chart-generated/self-signed certificates.
  * Elastic does not recommend this approach for a production deployment.
  * A follow-up should evaluate using `cert-manager` or the organisation's standard certificate-management mechanism for the operator webhook.

* **Review deprecated Kubernetes Terraform resources**

  * The module currently follows the existing repository convention of using:

    * `kubernetes_secret`
    * `kubernetes_namespace`
  * In Kubernetes provider `3.2.1`, these are deprecated aliases.
  * They still work today, so this does not block the PoC.
  * A future major provider release could remove them.
  * A follow-up should either migrate to the currently recommended resources or explicitly pin the provider to an appropriate `~> 3.0` range until the migration is performed.

* **Monitor cluster-stats collector memory usage**

  * The `cluster-stats` collector is still using the chart's default `128Mi` memory configuration.
  * It is currently stable and running.
  * No immediate change is required.
  * However, cluster-state telemetry volume generally increases as more Kubernetes objects and workloads are added.
  * The pod should therefore be monitored for memory-related restarts as the PoC continues.

* **Review the Helm `pre-delete` hook scheduling behaviour**

  * Chart `0.3.3` creates a `pre-delete` hook Job during Helm uninstall.
  * That Job does not expose toleration configuration.
  * If a cluster were configured so that every node required a taint toleration, the cleanup Job might be unable to schedule.
  * In that situation, `helm uninstall` could remain blocked waiting for the hook.
  * This does not affect the current deployment but should be considered before production adoption.

* **Create a PoC cleanup activity**

  * At the end of the PoC, remove the temporary Elastic namespace override by setting:

    * `data_stream_namespace = null`
  * Once new telemetry is no longer being written into the PoC namespace, coordinate with the Elastic team to remove the `*-ansys_namespace` data streams.
  * This should be tracked explicitly so temporary PoC data-stream configuration does not unintentionally become permanent production configuration.

---

## Overall status

* **The Helios deployment foundation is now working.**

  * The missing upstream Helm configuration has been restored.
  * Terraform module configuration has been hardened.
  * Elastic credentials/configuration references have been validated.
  * Required scheduling rules have been implemented.
  * The PoC namespace requirement has been implemented.
  * Gateway, operator and cluster-level collection are operational.
  * Kubernetes events and cluster-state metrics currently have full-cluster coverage.

* **The remaining blocker for full telemetry coverage is the daemon collector.**

  * The configuration issue affecting `K8S_NODE_NAME` has been fixed in the implementation and still needs cluster-side confirmation.
  * Separately, two daemon pods cannot currently schedule because their target nodes do not have enough allocatable memory.
  * Until node capacity is reclaimed, workloads are preempted, or the worker nodes are resized, container logs, host metrics and kubelet metrics will remain limited to the node where the daemon is currently running.

* **The main decision required from the team is whether Helios should remain opt-in or become enabled by default.**

  * Current implementation keeps `enable_helios = false`.
  * This avoids breaking existing environments that do not yet have the required SSM parameters.
  * The default should only be changed once the team confirms the desired rollout behaviour and the required parameters have been provisioned across all affected accounts and regions.
